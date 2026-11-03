#!/usr/bin/env python3
"""fast_rpc.py — persistent keep-alive RPC client with local nonce tracking,
gas limit cache, and background fee refresh. For SuperAgentic Wallet daemon.

RPC FAILOVER (2026-10-30, perintah owner): a client may be constructed with
a LIST of endpoint URLs for its chain. call() walks the endpoints in health
order (last-good first, endpoints in transport-failure cooldown last) with
the IDENTICAL JSON-RPC payload and fails over on transport failures
(connection closed/timeout/HTTP 5xx/HTTP 429/non-JSON response) AND on
rate-limit rejections (JSON-RPC -32005 / 'rate limit' / 'too many
requests' — fix 2026-10-30, temuan SEDANG uji keamanan BrainD id 720: a
rate-limited node refused the request before processing it, so it is the
same class of trigger as transport). Any OTHER JSON-RPC error returned by
a node is semantic (revert, policy, nonce, ...) and is raised as-is —
never a failover trigger. For eth_sendRawTransaction the resubmission is
the same raw tx (idempotent, same hash): a node answering
"already known"/"already imported" counts as success, and "nonce too low"
counts as success ONLY when an earlier attempt in the same call failed
over (transport or rate limit).
"""
import asyncio, json, time, ssl
import aiohttp

RPC_COOLDOWN_SEC = 60

def _raw_tx_hash(raw_hex):
    """Local keccak256 hash of a signed raw transaction (0x-prefixed)."""
    try:
        from eth_utils import keccak
        raw = bytes.fromhex(raw_hex[2:] if raw_hex.startswith("0x") else raw_hex)
        return "0x" + keccak(raw).hex()
    except Exception:
        return None

class _TransportFail(Exception):
    pass

def _is_rate_limit_error(err):
    """True when a JSON-RPC error is a rate-limit rejection: code -32005
    or 429, or a message carrying 'rate limit' / 'too many requests'.
    Mirrors walletd._is_rate_limit_error (fix 2026-10-30)."""
    if isinstance(err, dict):
        code = err.get("code")
        msg = str(err.get("message", ""))
    else:
        code, msg = None, str(err)
    if code in (-32005, 429, "-32005", "429"):
        return True
    low = msg.lower()
    return ("rate limit" in low or "rate_limit" in low
            or "too many requests" in low)

class FastRPC:
    def __init__(self, url, chain_id, log_fn=None):
        # url: a single URL string, or a list/tuple of endpoint URLs
        # (first = primary). Failover walks the list in health order.
        self.urls = list(url) if isinstance(url, (list, tuple)) else [url]
        self.url = self.urls[0]
        self.chain_id = chain_id
        self._log = log_fn
        self._cooldown = {}         # url -> unix ts until which it is deprioritized
        self._last_good = None      # url of the last endpoint that answered
        self.session = None
        self._nonce = None          # local nonce tracking
        self._nonce_lock = asyncio.Lock()
        self._gas_cache = {}        # (to, data_prefix) -> gas_limit
        self._fee = {}              # cached fee params
        self._fee_ts = 0
        self._id = 0

    async def _ensure(self):
        if self.session is None or self.session.closed:
            # Use system CA bundle (includes Hatch egress CA for TLS interception)
            ssl_ctx = ssl.create_default_context()
            conn = aiohttp.TCPConnector(limit=20, keepalive_timeout=60, ssl=ssl_ctx)
            self.session = aiohttp.ClientSession(
                connector=conn,
                trust_env=True,  # use https_proxy env var
                headers={"Content-Type": "application/json",
                         "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"})

    def _ordered_urls(self):
        now = time.time()
        fresh = [u for u in self.urls if self._cooldown.get(u, 0) <= now]
        cooled = [u for u in self.urls if self._cooldown.get(u, 0) > now]
        ordered = []
        if self._last_good in fresh:
            ordered.append(self._last_good)
        ordered += [u for u in fresh if u != self._last_good]
        ordered += cooled
        return ordered

    async def _post_once(self, url, method, params):
        """One attempt against one endpoint. Returns the parsed JSON dict.
        Raises _TransportFail on transport problems (incl. HTTP 5xx with
        no JSON-RPC error body) and on ANY HTTP 429 (rate limit — a
        failover trigger even when the body carries a JSON-RPC error);
        any other parsed dict carrying a JSON-RPC 'error' is RETURNED so
        call() can treat it semantically."""
        self._id += 1
        try:
            async with self.session.post(url, json={
                    "jsonrpc": "2.0", "id": self._id, "method": method, "params": params},
                    timeout=aiohttp.ClientTimeout(total=15)) as r:
                status = r.status
                try:
                    data = await r.json(content_type=None)
                except Exception:
                    raise _TransportFail(f"HTTP {status} non-JSON response")
                if not isinstance(data, dict) or ("result" not in data and "error" not in data):
                    raise _TransportFail(f"HTTP {status} malformed JSON-RPC response")
                if status == 429:
                    raise _TransportFail("HTTP 429 rate limit exceeded")
                if status >= 500:
                    if "error" in data:
                        return data  # node answered semantically despite status
                    raise _TransportFail(f"HTTP {status}")
                return data
        except _TransportFail:
            raise
        except Exception as e:
            raise _TransportFail(f"{type(e).__name__}: {e}")

    async def call(self, method, params=[]):
        await self._ensure()
        is_send = (method == "eth_sendRawTransaction")
        tx_hash = _raw_tx_hash(params[0]) if is_send and params else None
        transport_failed_before = False
        last_err = None
        last_rl_err = None
        ordered = self._ordered_urls()
        for i, url in enumerate(ordered):
            try:
                data = await self._post_once(url, method, params)
            except _TransportFail as e:
                last_err = e
                transport_failed_before = True
                self._cooldown[url] = time.time() + RPC_COOLDOWN_SEC
                if self._log:
                    nxt = ordered[i + 1] if i + 1 < len(ordered) else None
                    self._log(f"RPC-FAILOVER chain={hex(self.chain_id)} method={method} "
                              f"from={url} to={nxt or 'NONE'} err={e}")
                continue
            if "error" in data:
                err = data["error"]
                msg = str(err.get("message", err)) if isinstance(err, dict) else str(err)
                low = msg.lower()
                if _is_rate_limit_error(err):
                    # Rate-limit rejection (fix 2026-10-30): the node did
                    # NOT process the request — fail over with the
                    # identical payload + cooldown, like transport.
                    last_rl_err = err
                    transport_failed_before = True
                    self._cooldown[url] = time.time() + RPC_COOLDOWN_SEC
                    if self._log:
                        nxt = ordered[i + 1] if i + 1 < len(ordered) else None
                        self._log(f"RPC-FAILOVER chain={hex(self.chain_id)} method={method} "
                                  f"from={url} to={nxt or 'NONE'} err=rate-limit: {msg}")
                    continue
                if is_send and tx_hash and ("already known" in low or "already imported" in low):
                    self._last_good = url
                    return tx_hash
                if is_send and tx_hash and transport_failed_before and "nonce too low" in low:
                    self._last_good = url
                    return tx_hash
                raise RuntimeError(err)
            self._last_good = url
            return data["result"]
        if last_err is None and last_rl_err is not None:
            raise RuntimeError(last_rl_err)
        raise RuntimeError(
            f"rpc transport failure on all endpoints for chain {hex(self.chain_id)} "
            f"method {method}: {last_err}")

    async def get_nonce(self, address):
        """Local nonce tracking: fetch once, increment locally, resync on error."""
        async with self._nonce_lock:
            if self._nonce is None:
                self._nonce = int(await self.call("eth_getTransactionCount",
                                                  [address, "pending"]), 16)
            n = self._nonce
            self._nonce += 1
            return n

    def nonce_resync(self):
        """Call this when 'nonce too low' error occurs."""
        self._nonce = None

    def gas_limit(self, to, data="0x"):
        """Cached gas limit per contract+function selector."""
        key = (to.lower() if to else "", (data[:10] if data else "0x"))
        return self._gas_cache.get(key)

    def set_gas_limit(self, to, data, limit):
        key = (to.lower() if to else "", (data[:10] if data else "0x"))
        self._gas_cache[key] = int(limit * 1.3)  # 30% buffer

    async def refresh_fees(self):
        """Background fee refresh. Call periodically, not per-tx."""
        try:
            # Try EIP-1559 first
            block = await self.call("eth_getBlockByNumber", ["latest", False])
            base = int(block.get("baseFeePerGas", "0x0"), 16)
            if base > 0:
                # Fee ikut chain (revisi owner 2 Nov 2026 22:18 WIB):
                # priority = eth_maxPriorityFeePerGas chain apa adanya;
                # maxFee = baseFee berjalan + priority — mekanik
                # penerimaan chain itu sendiri, TANPA pengali markup
                # (2,2x / 1,5x / 1,8-2,5x semuanya dicabut).
                try:
                    prio = int(await self.call("eth_maxPriorityFeePerGas"), 16)
                except Exception:
                    gp = int(await self.call("eth_gasPrice"), 16)
                    prio = max(gp - base, 0)
                self._fee = {
                    "type": 2,
                    "maxFeePerGas": base + prio,
                    "maxPriorityFeePerGas": prio,
                }
            else:
                # Legacy chain (BSC): gasPrice chain apa adanya.
                gp = int(await self.call("eth_gasPrice"), 16)
                self._fee = {"type": 0, "gasPrice": gp}
            self._fee_ts = time.time()
        except Exception:
            pass  # keep old fees on error

    def fees(self):
        """Return cached fees, refresh if stale (>12s)."""
        return dict(self._fee)

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
