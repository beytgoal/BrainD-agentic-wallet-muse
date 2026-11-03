#!/usr/bin/env python3
"""SuperAgentic Wallet Daemon — the agent IS the wallet.
WebSocket server (127.0.0.1:18923) speaking EIP-1193 to the bridge extension.
Policy engine auto-approves per owner rules; signs via local key; broadcasts via fast RPC.
"""
import asyncio, hashlib, hmac, json, os, random, sys, time, urllib.request
import base64, glob
VENV = os.path.expanduser("~/workspace/.wallet-venv/lib")
for p in glob.glob(VENV + "/python*/site-packages"):
    sys.path.insert(0, p)
import websockets
from eth_account import Account
from eth_account.messages import encode_defunct
from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins
from solders.keypair import Keypair as SolKeypair
from fast_rpc import FastRPC, RPC_COOLDOWN_SEC, _raw_tx_hash

# Fast RPC clients (keep-alive, local nonce, cached fees) — one per chain.
# Constructed with the chain's full endpoint list (primary + backups) so
# the WS path gets the same transport failover as rpc_call_chain (HTTP).
_fast_clients = {}
def fast_client(chain_hex):
    if chain_hex not in RPCS:
        # No silent fallback to the default chain (fix 2026-10-30).
        raise RuntimeError(f"4902: chain not supported by this wallet: {chain_hex!r}")
    if chain_hex not in _fast_clients:
        _, cid = RPCS[chain_hex]
        _fast_clients[chain_hex] = FastRPC(rpc_endpoints_filtered(chain_hex), cid,
                                           log_fn=log)
    return _fast_clients[chain_hex]

async def fee_refresher():
    """Background task: refresh fee params every 12s for all chains."""
    while True:
        for ch in list(RPCS.keys()):
            try:
                await fast_client(ch).refresh_fees()
            except Exception:
                pass
        await asyncio.sleep(12)

SEED_FILE = os.path.expanduser("~/.burner/seed")
PORT = 18923
LOG = os.path.expanduser("~/workspace/superagentic-wallet/daemon/walletd.log")
# SOL_RPC is defined per-network in the Solana networking section below
# (migrated 2026-10-30, fix datahub id 685 — it used to be a single
# hardcoded MAINNET URL here, so every sol_* read silently hit mainnet).

RPCS = {
    "0x38":  ("https://bsc-rpc.publicnode.com", 56),
    "0x1":   ("https://ethereum-rpc.publicnode.com", 1),
    "0x2105":("https://base-rpc.publicnode.com", 8453),
    "0x89":  ("https://polygon-rpc.publicnode.com", 137),
    "0x185d9": ("https://testnet-rpc.elysium.kinetiq.xyz", 99801),
    # --- testnets (added 2026-10-28 for Uniswap testnet experiment) ---
    "0xaa36a7": ("https://ethereum-sepolia-rpc.publicnode.com", 11155111),
    "0x61":  ("https://bsc-testnet-rpc.publicnode.com", 97),
    "0x14a34": ("https://base-sepolia-rpc.publicnode.com", 84532),
    "0x13882": ("https://polygon-amoy-bor-rpc.publicnode.com", 80002),
}
DEFAULT_CHAIN = "0x38"
UA = {"Content-Type": "application/json",
      "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}

# ---- RPC backup endpoints + transport failover (2026-10-30, perintah owner) ----
# walletd used to have exactly ONE public RPC per chain; the Sepolia
# publicnode endpoint intermittently closes connections ("Remote end
# closed connection without response"), failing reads and losing send
# responses. Every backup below was verified live on 2026-10-30 to answer
# eth_chainId with the correct chain id and no API key. The primary in
# RPCS stays FIRST; failover order is health-based (see _rpc_order).
RPC_BACKUPS = {
    "0x38":  ["https://bsc-dataseed.bnbchain.org",
              "https://1rpc.io/bnb",
              "https://bsc.drpc.org"],
    "0x1":   ["https://1rpc.io/eth",
              "https://eth.drpc.org"],
    "0x2105": ["https://mainnet.base.org",
               "https://base.drpc.org"],
    "0x89":  ["https://1rpc.io/matic",
              "https://polygon.drpc.org"],
    # Elysium (99801): NO verified public alternative exists (probed
    # kinetiq host variants 2026-10-30 — none answered) -> single endpoint.
    "0x185d9": [],
    "0xaa36a7": ["https://gateway.tenderly.co/public/sepolia",
                 "https://api.zan.top/eth-sepolia",
                 "https://1rpc.io/sepolia"],
    "0x61":  ["https://data-seed-prebsc-1-s1.bnbchain.org:8545",
              "https://bsc-testnet.drpc.org"],
    "0x14a34": ["https://sepolia.base.org",
                "https://base-sepolia.drpc.org"],
    "0x13882": ["https://polygon-amoy.drpc.org",
                "https://polygon-amoy.gateway.tenderly.co"],
}
RPC_ENDPOINTS = {ch: [RPCS[ch][0]] + RPC_BACKUPS.get(ch, []) for ch in RPCS}
# Test hook: comma-separated substrings; endpoints containing any of them
# are excluded from failover order (used once to prove failover works).
RPC_BLACKLIST = [s.strip() for s in
                 os.environ.get("WALLETD_RPC_BLACKLIST", "").split(",") if s.strip()]
_rpc_cooldown = {}    # endpoint url -> unix ts until which it is deprioritized
_rpc_last_good = {}   # chain_hex -> url of last endpoint that answered

def rpc_endpoints_filtered(chain_hex):
    """Endpoint list for a chain with the WALLETD_RPC_BLACKLIST test hook
    applied; if filtering would empty the list, keep the full list."""
    if chain_hex not in RPCS:
        # No silent fallback to the default chain (fix 2026-10-30).
        raise RuntimeError(f"4902: chain not supported by this wallet: {chain_hex!r}")
    eps = RPC_ENDPOINTS[chain_hex]
    if RPC_BLACKLIST:
        kept = [u for u in eps if not any(b in u for b in RPC_BLACKLIST)]
        if kept:
            return kept
    return list(eps)

def _rpc_order(chain_hex):
    """Failover order: last-good endpoint first, then healthy endpoints in
    config order, then endpoints still in transport-failure cooldown."""
    eps = rpc_endpoints_filtered(chain_hex)
    now = time.time()
    fresh = [u for u in eps if _rpc_cooldown.get(u, 0) <= now]
    cooled = [u for u in eps if _rpc_cooldown.get(u, 0) > now]
    good = _rpc_last_good.get(chain_hex)
    ordered = []
    if good in fresh:
        ordered.append(good)
    ordered += [u for u in fresh if u != good]
    ordered += cooled
    return ordered

# ---- policy (owner standing auth 2026-10-24; REVISI 2 Nov 2026 22:18 WIB) ----
# Semua cap nilai DICABUT atas perintah owner ("bebaskan semuanya — ini
# decentralized, bukan perbankan"): tidak ada lagi MAX_AUTO_VALUE_WEI /
# ELYSIUM_VALUE_CAP_WEI. Satu-satunya penolakan policy yang tersisa adalah
# denylist alamat di bawah (bukan cap nominal).
BLOCKED = set()                          # denylisted addresses (lowercase)

ELYSIUM_CHAIN_HEX = "0x185d9"          # 99801 — NOTE: division inpage.js wrote 0x185f1 (= 99825), wrong
# Clamp gas Elysium (lantai 0,1 / cap 0,2 gwei) juga DICABUT 2 Nov 2026:
# fee ikut eth_gasPrice chain apa adanya di semua jalur (WS maupun HTTP).

def norm_chain_hex(v):
    """Normalize a chain id (hex str, decimal str, or int) to lowercase '0x..' hex, or None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return "0x%x" % v
    if isinstance(v, str):
        s = v.strip().lower()
        try:
            return "0x%x" % (int(s, 16) if s.startswith("0x") else int(s))
        except ValueError:
            return None
    return None

def _is_rate_limit_error(err):
    """True when a JSON-RPC error answered by a node is a RATE-LIMIT
    rejection (fix 2026-10-30, temuan SEDANG uji keamanan BrainD id 720):
    code -32005 or 429, or a message carrying 'rate limit' / 'too many
    requests'. A rate-limit rejection means the node refused the request
    BEFORE processing it, so it is a failover trigger just like a
    transport failure — the identical payload may go to the next
    endpoint. Every other JSON-RPC error (revert, insufficient funds,
    invalid params, nonce, 'already known', ...) is semantic and final:
    NEVER a failover trigger."""
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

def resolve_chain_hint(raw):
    """Resolve an EXPLICIT per-request chain value to a supported chain
    hex (fix 2026-10-30, temuan SEDANG uji keamanan BrainD id 720:
    unknown chains used to be silently mapped to BSC).

    - Absent / None / empty string -> DEFAULT_CHAIN. Legit callers rely
      on this: wd.py/brain.py omit 'chain' for chain-agnostic methods
      (accounts, personal_sign, sol/sui/cosmos), wc-wallet sends 'chain'
      only when a chainHex is given, and older extension providers send
      no hint at all.
    - Any other explicit value that does not normalize to a chain in
      RPCS is REJECTED (4902 chain not supported) — never remapped."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return DEFAULT_CHAIN
    h = norm_chain_hex(raw)
    if h is None or h not in RPCS:
        raise RuntimeError(f"4902: chain not supported by this wallet: {raw!r}")
    return h

def error_code(msg):
    """Split a 'NNNN: text' error prefix (EIP-1193 code) from the message.
    Returns (code, message); code is -32000 when there is no numeric prefix."""
    if len(msg) > 5 and msg[4] == ":" and msg[:4].isdigit():
        return int(msg[:4]), msg[5:].lstrip()
    return -32000, msg

def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")

# ---- Caller token for the local HTTP bridge (2026-10-30, perintah owner:
# tutup temuan TINGGI uji keamanan BrainD — bypass Brain via POST langsung
# ke 18924). EVERY HTTP POST must carry header X-Walletd-Token matching the
# secret in CALLER_TOKEN_FILE (0600, alongside the seed). The token is read
# fresh on every request so rotation needs no daemon restart. Fail-closed:
# a missing/short token file denies ALL HTTP POSTs. The WS path (18923) is
# NOT covered by this token — it is gated by Brain (brain_gate_ws) and the
# relay token upstream. The token value is NEVER logged.
CALLER_TOKEN_FILE = os.path.expanduser("~/.burner/walletd.token")
CALLER_TOKEN_HEADER = "X-Walletd-Token"

def _load_caller_token():
    try:
        with open(CALLER_TOKEN_FILE) as f:
            tok = f.read().strip()
    except Exception:
        return None
    return tok if len(tok) >= 32 else None

def caller_authorized(header_value):
    expected = _load_caller_token()
    if expected is None or not header_value:
        return False
    return hmac.compare_digest(str(header_value).strip(), expected)

def seed():
    with open(SEED_FILE) as f:
        return f.read().strip()

def acct():
    s = Bip39SeedGenerator(seed()).Generate()
    return Account.from_key(
        Bip44.FromSeed(s, Bip44Coins.ETHEREUM).DeriveDefaultPath().PrivateKey().Raw().ToHex())

def sol_kp():
    s = Bip39SeedGenerator(seed()).Generate()
    raw32 = Bip44.FromSeed(s, Bip44Coins.SOLANA).DeriveDefaultPath().PrivateKey().Raw().ToBytes()
    return SolKeypair.from_seed(raw32)

# ---- Solana networking (migrated 2026-10-30, fix datahub id 685) ----
# The old hardcoded SOL_RPC = https://solana-rpc.publicnode.com was
# MAINNET for every sol_* call: the burner's DEVNET balance read 0 (the
# 5 SOL were on devnet) and sol_signAndSendTransaction would have
# broadcast to mainnet. Endpoints are now per-network, mirroring the Sui
# migration (id 671). All company Solana work is devnet, so the default
# network is DEVNET — a read can never silently resolve against mainnet.
# Callers may pass {"network": "devnet"|"testnet"|"mainnet"} in any dict
# inside params for an explicit choice; WALLETD_SOL_NETWORK overrides
# the default.
SOL_RPC = {
    "devnet": "https://api.devnet.solana.com",
    "testnet": "https://api.testnet.solana.com",
    "mainnet": "https://solana-rpc.publicnode.com",
}
SOL_DEFAULT_NETWORK = os.environ.get("WALLETD_SOL_NETWORK", "devnet").lower()
if SOL_DEFAULT_NETWORK not in SOL_RPC:
    SOL_DEFAULT_NETWORK = "devnet"

def _sol_param_dicts(params):
    if isinstance(params, dict):
        return [params]
    if isinstance(params, (list, tuple)):
        return [x for x in params if isinstance(x, dict)]
    return []

def sol_network(params=None):
    """Resolve the Solana network for a call: an explicit {"network": ...}
    in any dict inside params wins; default SOL_DEFAULT_NETWORK."""
    for p in _sol_param_dicts(params):
        n = str(p.get("network") or "").lower()
        if n in SOL_RPC:
            return n
    return SOL_DEFAULT_NETWORK

def sol_mainnet_confirmed(params=None):
    """True only when a caller EXPLICITLY opted into a mainnet broadcast
    with {"confirmMainnet": true} in a params dict. Anything else —
    including a default that merely resolves to mainnet — does not
    authorize a mainnet broadcast (see the guard in
    sol_signAndSendTransaction)."""
    return any(p.get("confirmMainnet") is True for p in _sol_param_dicts(params))

def sol_rpc(method, params=[], network=None):
    # Legacy sync version (for HTTP handler). WS handler should use fast path.
    net = network or sol_network(params)
    req = urllib.request.Request(SOL_RPC[net],
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        headers=UA)
    r = json.loads(urllib.request.urlopen(req, timeout=15).read())
    if "error" in r:
        raise RuntimeError(r["error"])
    return r["result"]

_sol_session = None
async def sol_rpc_fast(method, params=[], network=None):
    """Fast async Solana RPC with keep-alive."""
    global _sol_session
    import aiohttp, ssl
    net = network or sol_network(params)
    if _sol_session is None or _sol_session.closed:
        ssl_ctx = ssl.create_default_context()
        conn = aiohttp.TCPConnector(limit=10, keepalive_timeout=60, ssl=ssl_ctx)
        _sol_session = aiohttp.ClientSession(connector=conn, trust_env=True,
            headers={"Content-Type": "application/json",
                     "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"})
    async with _sol_session.post(SOL_RPC[net], json={
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=aiohttp.ClientTimeout(total=15)) as r:
        data = await r.json()
    if "error" in data:
        raise RuntimeError(data["error"])
    return data["result"]

def sui_address():
    s = Bip39SeedGenerator(seed()).Generate()
    return Bip44.FromSeed(s, Bip44Coins.SUI).DeriveDefaultPath().PublicKey().ToAddress()

def sui_privkey():
    """Sui Ed25519 private key bytes (32)."""
    s = Bip39SeedGenerator(seed()).Generate()
    return Bip44.FromSeed(s, Bip44Coins.SUI).DeriveDefaultPath().PrivateKey().Raw().ToBytes()

def sui_pubkey():
    """Sui Ed25519 public key bytes (32) via pysui."""
    from pysui.sui.sui_crypto import SuiKeyPair
    import bech32 as b32
    priv = sui_privkey()
    data = b'\x00' + priv
    conv = b32.convertbits(data, 8, 5)
    encoded = b32.bech32_encode('suiprivkey', conv)
    kp = SuiKeyPair.from_bech32(encoded)
    # public_key may be SuiPublicKey object or base64 string
    pk = kp.public_key
    if isinstance(pk, bytes):
        return pk
    if isinstance(pk, str):
        return base64.b64decode(pk)
    # SuiPublicKey object — try to_bytes or value
    if hasattr(pk, 'key_bytes'):
        return pk.key_bytes
    if hasattr(pk, 'to_bytes'):
        return pk.to_bytes()
    if hasattr(pk, 'value'):
        v = pk.value
        return v if isinstance(v, bytes) else base64.b64decode(v)
    return bytes(pk)

def sui_sign_intent(intent_msg):
    """Ed25519-sign a Sui intent message per the Sui Intent Signing standard.

    The signature is over the Blake2b-256 digest (digest_size=32) of the
    intent message, NOT over the raw intent bytes. (Fix 2026-10-30: the old
    code signed the raw intent, which Sui validators reject.)
    Returns (sig_bytes, pubkey_bytes).
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv = Ed25519PrivateKey.from_private_bytes(sui_privkey())
    digest = hashlib.blake2b(intent_msg, digest_size=32).digest()
    sig = priv.sign(digest)
    return sig, sui_pubkey()

def sui_sign_tx_bytes(tx_bytes):
    """Sign Sui transaction bytes. Returns base64 signature (flag+sig+pubkey)."""
    # Intent: 0x00 (TransactionData) + 0x00 (version) + 0x00 (app_id)
    intent_msg = b'\x00\x00\x00' + tx_bytes
    sig, pub = sui_sign_intent(intent_msg)
    # signature = flag(0x00) + sig(64) + pub(32), base64
    return base64.b64encode(b'\x00' + sig + pub).decode()

# ---- Sui networking (migrated 2026-10-30, Aliran 3 / Divisi Infrastruktur) ----
# The old hardcoded SUI_RPC = https://sui-rpc.publicnode.com was MAINNET
# JSON-RPC, which Sui has deprecated globally: every call now fails with
# -32000 "Sui JSON-RPC endpoint has been deprecated. Please use the gRPC
# endpoint instead". READS therefore go through the Sui GraphQL API,
# which is per-network. Company Sui work is testnet-only, so the default
# network is TESTNET — a read can never silently resolve against mainnet.
# Callers may pass {"network": "mainnet"|"testnet"} in params for an
# explicit choice; WALLETD_SUI_NETWORK overrides the default.
SUI_GRAPHQL = {
    "testnet": "https://graphql.testnet.sui.io/graphql",
    "mainnet": "https://graphql.mainnet.sui.io/graphql",
}
SUI_DEFAULT_NETWORK = os.environ.get("WALLETD_SUI_NETWORK", "testnet").lower()
if SUI_DEFAULT_NETWORK not in SUI_GRAPHQL:
    SUI_DEFAULT_NETWORK = "testnet"
# Legacy JSON-RPC fullnodes per network — ONLY still used by sui_transfer's
# tx building (unsafe_*). Kept network-aware so a transfer can never be
# built against mainnet by accident (see the mainnet guard in sui_transfer).
SUI_RPC = {
    "testnet": "https://fullnode.testnet.sui.io:443",
    "mainnet": "https://fullnode.mainnet.sui.io:443",
}

def sui_network(params=None):
    """Resolve the Sui network for a call from its params (dict or
    list whose first element is a dict); default SUI_DEFAULT_NETWORK."""
    p = params[0] if isinstance(params, list) and params else params
    if isinstance(p, dict):
        n = str(p.get("network") or "").lower()
        if n in SUI_GRAPHQL:
            return n
    return SUI_DEFAULT_NETWORK

def sui_gql(query, variables, network):
    req = urllib.request.Request(SUI_GRAPHQL[network],
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers=UA)
    r = json.loads(urllib.request.urlopen(req, timeout=20).read())
    if r.get("errors"):
        raise RuntimeError(f"sui graphql[{network}]: {r['errors']}")
    return r.get("data") or {}

_SUI_BALANCE_Q = ('query ($addr: SuiAddress!) { address(address: $addr) '
                  '{ balance(coinType: "0x2::sui::SUI") { totalBalance } } }')

def sui_get_balance(address, network):
    """SUI balance via GraphQL (replaces deprecated suix_getBalance).

    Returns a dict shaped like the old JSON-RPC result (totalBalance as
    string) plus explicit network/source, so callers can see which
    network answered."""
    data = sui_gql(_SUI_BALANCE_Q, {"addr": address}, network)
    bal = ((data.get("address") or {}).get("balance") or {})
    tb = bal.get("totalBalance")
    return {"coinType": "0x2::sui::SUI",
            "totalBalance": str(int(tb)) if tb is not None else "0",
            "network": network, "source": "graphql"}

def sui_rpc(method, params=[], network=None):
    """Legacy JSON-RPC helper — used ONLY by sui_transfer's tx building
    (unsafe_*). Network-aware: targets the resolved network's official
    fullnode, never the old hardcoded mainnet publicnode URL."""
    net = network or sui_network(params)
    req = urllib.request.Request(SUI_RPC[net],
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        headers=UA)
    r = json.loads(urllib.request.urlopen(req, timeout=15).read())
    if "error" in r:
        raise RuntimeError(r["error"])
    return r["result"]

def cosmos_address():
    s = Bip39SeedGenerator(seed()).Generate()
    return Bip44.FromSeed(s, Bip44Coins.COSMOS).DeriveDefaultPath().PublicKey().ToAddress()

def cosmos_privkey():
    """Cosmos secp256k1 private key bytes (32)."""
    s = Bip39SeedGenerator(seed()).Generate()
    return Bip44.FromSeed(s, Bip44Coins.COSMOS).DeriveDefaultPath().PrivateKey().Raw().ToBytes()

COSMOS_REST = "https://cosmos-rest.publicnode.com"
COSMOS_REST_FALLBACKS = [
    "https://cosmos-rest.publicnode.com",
    "https://rest.cosmos.directory/cosmoshub",
]
COSMOS_RPC = "https://cosmos-rpc.publicnode.com"

def cosmos_rest(path):
    """Cosmos REST with fallback endpoints (publicnode flaky)."""
    last_err = None
    for base in COSMOS_REST_FALLBACKS:
        try:
            req = urllib.request.Request(base + path, headers=dict(UA))
            return json.loads(urllib.request.urlopen(req, timeout=15).read())
        except Exception as e:
            last_err = e
            continue
    raise last_err

def cosmos_account_info(address):
    """Get account number + sequence from REST API."""
    data = cosmos_rest(f"/cosmos/auth/v1beta1/accounts/{address}")
    acc = data["account"]
    # Could be BaseAccount or wrapped
    if "account" in acc:
        acc = acc["account"]
    return int(acc["account_number"]), int(acc["sequence"])

def rpc_call(url, method, params=[]):
    req = urllib.request.Request(url,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        headers=UA)
    r = json.loads(urllib.request.urlopen(req, timeout=15).read())
    if "error" in r:
        raise RuntimeError(r["error"])
    return r["result"]

class _RpcTransport(Exception):
    """Transport-level RPC failure (failover trigger) — as opposed to a
    JSON-RPC error answered by a node, which is semantic and final."""

def _rpc_post_once(url, method, params, timeout=12):
    """One JSON-RPC attempt against ONE endpoint with the identical
    payload. Returns the parsed response dict (which may carry a semantic
    'error'). Raises _RpcTransport on connection close/timeout/HTTP 5xx
    without a JSON-RPC body/non-JSON or malformed response, and on ANY
    HTTP 429 (rate limit — a failover trigger since the 2026-10-30 fix,
    even when the 429 body carries a JSON-RPC error)."""
    req = urllib.request.Request(url,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            body = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 429:
            # HTTP 429 IS a rate-limit rejection (fix 2026-10-30): fail
            # over to the next endpoint even when the body carries a
            # JSON-RPC error — that error IS the rate-limit signal.
            raise _RpcTransport("HTTP 429 rate limit exceeded")
        try:
            body = e.read()
            data = json.loads(body)
            if isinstance(data, dict) and "error" in data:
                return data  # node answered semantically despite HTTP status
        except Exception:
            pass
        if e.code >= 500:
            raise _RpcTransport(f"HTTP {e.code}")
        raise _RpcTransport(f"HTTP {e.code} non-JSON response")
    except Exception as e:
        raise _RpcTransport(f"{type(e).__name__}: {e}")
    try:
        data = json.loads(body)
    except Exception:
        raise _RpcTransport(f"HTTP {status} non-JSON response")
    if not isinstance(data, dict) or ("result" not in data and "error" not in data):
        raise _RpcTransport(f"HTTP {status} malformed JSON-RPC response")
    if status == 429:
        raise _RpcTransport("HTTP 429 rate limit exceeded")
    if status >= 500:
        if "error" in data:
            return data
        raise _RpcTransport(f"HTTP {status}")
    return data

def rpc_call_chain(chain_hex, method, params=[]):
    """Chain-aware RPC with automatic failover across the chain's
    endpoints (primary + verified backups), identical payload on every
    attempt. Failover triggers: transport failures AND rate-limit
    rejections (JSON-RPC -32005 / HTTP 429 / 'rate limit' / 'too many
    requests' — fix 2026-10-30, temuan SEDANG id 720; a rate-limited
    node refused the request before processing it). All other semantic
    JSON-RPC errors (revert, policy, nonce, ...) are returned exactly as
    before (RuntimeError) and NEVER trigger failover. For
    eth_sendRawTransaction only the SAME signed raw tx is ever resent
    (idempotent — identical hash): a node answering 'already
    known'/'already imported' is success, and 'nonce too low' is success
    ONLY after an earlier attempt in this call failed over (transport or
    rate limit). If every endpoint fails, raises a clear error so
    callers reconcile via nonce instead of resending blindly. An
    explicit chain outside RPCS is rejected (4902), never remapped to
    the default chain."""
    if chain_hex not in RPCS:
        raise RuntimeError(f"4902: chain not supported by this wallet: {chain_hex!r}")
    is_send = (method == "eth_sendRawTransaction")
    tx_hash = _raw_tx_hash(params[0]) if is_send and params else None
    transport_failed_before = False
    last_err = None
    last_rl_err = None
    ordered = _rpc_order(chain_hex)
    for i, url in enumerate(ordered):
        try:
            data = _rpc_post_once(url, method, params)
        except _RpcTransport as e:
            last_err = e
            transport_failed_before = True
            _rpc_cooldown[url] = time.time() + RPC_COOLDOWN_SEC
            nxt = ordered[i + 1] if i + 1 < len(ordered) else None
            log(f"RPC-FAILOVER chain={chain_hex} method={method} from={url} "
                f"to={nxt or 'NONE'} err={e}")
            continue
        if "error" in data:
            err = data["error"]
            msg = str(err.get("message", err)) if isinstance(err, dict) else str(err)
            low = msg.lower()
            if _is_rate_limit_error(err):
                # Rate-limit rejection: the node did NOT process the
                # request — fail over with the identical payload and put
                # the endpoint in cooldown, exactly like transport.
                last_rl_err = err
                transport_failed_before = True
                _rpc_cooldown[url] = time.time() + RPC_COOLDOWN_SEC
                nxt = ordered[i + 1] if i + 1 < len(ordered) else None
                log(f"RPC-FAILOVER chain={chain_hex} method={method} from={url} "
                    f"to={nxt or 'NONE'} err=rate-limit: {msg}")
                continue
            if is_send and tx_hash and ("already known" in low or "already imported" in low):
                _rpc_last_good[chain_hex] = url
                return tx_hash
            if is_send and tx_hash and transport_failed_before and "nonce too low" in low:
                _rpc_last_good[chain_hex] = url
                return tx_hash
            raise RuntimeError(err)
        _rpc_last_good[chain_hex] = url
        return data["result"]
    if last_err is None and last_rl_err is not None:
        raise RuntimeError(last_rl_err)
    raise RuntimeError(
        f"rpc transport failure on all endpoints for chain {chain_hex} "
        f"method {method}: {last_err}")

def policy_check(tx, chain_hex):
    """Returns (approved: bool, reason: str).

    Sejak revisi owner 2 Nov 2026 22:18 WIB tidak ada lagi cap nilai di
    lapis walletd — satu-satunya penolakan adalah denylist alamat
    (BLOCKED). Jalur WS tetap digerbang intent Brain di handler-nya;
    jalur HTTP adalah jalur lokal termediasi (brain.py/wd.py)."""
    to = (tx.get("to") or "").lower()
    if to in BLOCKED:
        return False, "address denylisted"
    return True, "within policy"

# ---- Brain single gate for the WS / extension path (2026-10-30, perintah owner) ----
# Brain (custom-connectors/custom.walletd/bin/brain.py) is now the SINGLE
# gate for signing. The local HTTP path (18924) is NOT gated here: brain.py
# deliver / wd.py calls over HTTP are the mediated local path itself.
# Everything arriving over WS (127.0.0.1:18923) is extension-originated
# (bridge extension directly, or via forwarder 18888 -> saw-relay 18925),
# and on that path:
#   - eth_sendTransaction requires an approved, not-yet-consumed Brain
#     intent whose intent_hash matches the canonical hash of the request
#     (sha256 of "chain|method|to_lower|value_wei|data" — brain.py's exact
#     formula, chain = Brain chain NAME). A match is consumed BEFORE
#     signing/broadcast (fail-closed: replay of the identical request is
#     rejected; a failed broadcast still burns the intent, mirroring
#     brain.py deliver marking the intent executed before receipt checks).
#   - personal_sign / eth_signTypedData_v4 (Brain v2, keputusan owner
#     2026-11-02 22:14 WIB) require an approved, not-yet-consumed Brain
#     SIGN intent whose canonical hash covers the exact payload
#     (sha256 of "chain|method|sign|payload" — brain.py's exact formula;
#     payload = the message verbatim for personal_sign, or canonical-JSON
#     typed data for eth_signTypedData_v4). A match is consumed BEFORE
#     signing (fail-closed, same rule as transactions). No intent -> the
#     request is rejected; signing is NEVER free/auto-approved here.
#   - Reads (accounts/chainId/call/balance/...) stay free, and
#     wallet_switchEthereumChain stays allowed: it is not a signature and
#     moves no funds (this daemon is stateless per-chain-hint anyway).
# walletd only READS intents.jsonl / brain_policy.json. Consumption is
# recorded by appending to ws_consumed.jsonl + audit.jsonl in the Brain
# state dir (brain.py stays the sole writer of intents.jsonl; its
# load_intents overlays ws_consumed.jsonl so a consumed intent shows as
# executed and cannot be delivered again over HTTP).
BRAIN_STATE_DIR = os.path.expanduser("~/workspace/custom-connectors/custom.walletd/state")
BRAIN_INTENTS_PATH = os.path.join(BRAIN_STATE_DIR, "intents.jsonl")
BRAIN_AUDIT_PATH = os.path.join(BRAIN_STATE_DIR, "audit.jsonl")
BRAIN_CONSUMED_PATH = os.path.join(BRAIN_STATE_DIR, "ws_consumed.jsonl")
BRAIN_POLICY_PATH = os.path.join(BRAIN_STATE_DIR, "brain_policy.json")

def _brain_now_iso():
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=7))).isoformat()

def _brain_chain_name(chain_hex):
    """Brain chain NAME for a chainId hex, read from brain_policy.json
    (single source of truth). None when the chain is not in Brain."""
    try:
        with open(BRAIN_POLICY_PATH) as f:
            pol = json.load(f)
        for name, info in (pol.get("chains") or {}).items():
            if str(info.get("chain_id", "")).lower() == chain_hex.lower():
                return name
    except Exception:
        pass
    return None

def _brain_intent_hash(chain, method, to, value_wei, data):
    """Exact copy of brain.py's intent_hash_of (canonical string)."""
    canonical = "|".join([chain, method, to.lower(), str(value_wei), data])
    return hashlib.sha256(canonical.encode()).hexdigest()

def _brain_sign_hash(chain, method, payload):
    """Canonical hash for Brain SIGN intents (v2) — exact copy of
    brain.py's intent_hash_of branch for SIGN_METHODS."""
    canonical = "|".join([chain, method, "sign", payload or ""])
    return hashlib.sha256(canonical.encode()).hexdigest()

def _brain_canonical_json(obj):
    """Canonical JSON for typed-data payloads — exact copy of brain.py's
    canonical_json (sorted keys, no spaces)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)

def _brain_intent_hash_any(rec):
    """Recompute an intent record's hash from its own fields — sign
    intents and transaction intents alike (exact-match re-verification,
    same discipline as brain.py deliver)."""
    if rec.get("method") == "personal_sign":
        return _brain_sign_hash(rec["chain"], rec["method"],
                                rec.get("message") or "")
    if rec.get("method") == "eth_signTypedData_v4":
        return _brain_sign_hash(rec["chain"], rec["method"],
                                rec.get("typed_data") or "")
    return _brain_intent_hash(rec["chain"], rec["method"], rec["to"],
                              rec["value_wei"], rec["data"])

def _brain_latest_intents():
    latest = {}
    try:
        with open(BRAIN_INTENTS_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                latest[rec["intent_id"]] = rec
    except FileNotFoundError:
        pass
    return latest

def _brain_consumed_ids():
    ids = set()
    try:
        with open(BRAIN_CONSUMED_PATH) as f:
            for line in f:
                line = line.strip()
                if line:
                    ids.add(json.loads(line)["intent_id"])
    except FileNotFoundError:
        pass
    return ids

def _brain_append_jsonl(path, obj):
    with open(path, "a") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())

def brain_audit_ws(event, rec, tx_hash=None, detail=None):
    """Append a Brain audit line for a WS-path consumption event (same
    field shape as brain.py's audit()). Never includes secrets — intents
    carry only public tx parameters."""
    _brain_append_jsonl(BRAIN_AUDIT_PATH, {
        "ts": _brain_now_iso(), "event": event,
        "intent_id": rec.get("intent_id"), "caller": rec.get("caller"),
        "chain": rec.get("chain"), "method": rec.get("method"),
        "to": rec.get("to"), "value_wei": rec.get("value_wei"),
        "reason": rec.get("reason"), "actor": "walletd-ws-gate",
        "tx_hash": tx_hash, "receipt_status": None, "detail": detail,
    })

def _ws_value_wei(tx):
    v = tx.get("value", 0)
    if isinstance(v, str):
        return int(v, 16) if v.startswith(("0x", "0X")) else int(v)
    return int(v)

def brain_gate_ws(chain_hex, tx):
    """Brain gate for an eth_sendTransaction arriving via WS/extension.
    Returns the consumed intent record on success. Raises RuntimeError
    'brain approval required: ...' when no approved, unconsumed intent
    matches the request exactly. Callers run inside the asyncio loop and
    this check + marker append contain no awaits, so check-and-consume
    cannot interleave with another WS request in this process."""
    chain_name = _brain_chain_name(chain_hex)
    if chain_name is None:
        log(f"BRAIN-GATE DENY method=eth_sendTransaction chain={chain_hex} "
            f"alasan=chain tidak terdaftar di Brain")
        raise RuntimeError(
            f"brain approval required: chain {chain_hex} is not registered in Brain")
    to_norm = "CREATE" if not tx.get("to") else str(tx.get("to"))
    data = tx.get("data") or "0x"
    value_wei = str(_ws_value_wei(tx))
    want = _brain_intent_hash(chain_name, "eth_sendTransaction",
                              to_norm, value_wei, data)
    consumed = _brain_consumed_ids()
    match = None
    for rec in _brain_latest_intents().values():
        if rec.get("status") != "approved":
            continue
        if rec.get("intent_id") in consumed:
            continue
        if rec.get("intent_hash") != want:
            continue
        # Re-verify the stored hash from the record's own fields
        # (exact-match discipline, same as brain.py deliver).
        if _brain_intent_hash_any(rec) != rec["intent_hash"]:
            continue
        match = rec
        break
    if match is None:
        log(f"BRAIN-GATE DENY method=eth_sendTransaction chain={chain_name} "
            f"to={to_norm} value_wei={value_wei} hash={want[:12]} "
            f"alasan=tidak ada intent Brain approved yang cocok / sudah terpakai")
        raise RuntimeError(
            "brain approval required: no approved Brain intent matches this "
            "transaction (or it was already used)")
    # Consume BEFORE signing/broadcast (fail-closed against replay).
    _brain_append_jsonl(BRAIN_CONSUMED_PATH, {
        "ts": _brain_now_iso(), "intent_id": match["intent_id"],
        "intent_hash": match["intent_hash"], "chain": chain_name,
        "method": "eth_sendTransaction", "via": "ws",
    })
    brain_audit_ws("ws_consumed", match,
                   detail="jalur=ws intent dikonsumsi gerbang walletd sebelum "
                          "broadcast; hasil dicatat pada ws_delivered/ws_consume_failed")
    log(f"BRAIN-GATE ALLOW intent={match['intent_id']} chain={chain_name} "
        f"to={to_norm} value_wei={value_wei}")
    return match

def brain_gate_ws_sign(chain_hex, method, payload):
    """Brain gate for a message-signing request arriving via WS/extension
    (Brain v2, keputusan owner 2026-11-02 22:14 WIB). `payload` is the
    CANONICAL payload: the message verbatim for personal_sign, canonical
    JSON for eth_signTypedData_v4. Returns the consumed intent record on
    success. Raises RuntimeError 'brain approval required: ...' when no
    approved, unconsumed sign intent covers this exact payload. The
    intent is consumed BEFORE signing (fail-closed against replay) —
    signing on this path is never free or auto-approved."""
    chain_name = _brain_chain_name(chain_hex)
    if chain_name is None:
        log(f"BRAIN-GATE DENY method={method} chain={chain_hex} "
            f"alasan=chain tidak terdaftar di Brain")
        raise RuntimeError(
            f"brain approval required: chain {chain_hex} is not registered in Brain")
    want = _brain_sign_hash(chain_name, method, payload)
    consumed = _brain_consumed_ids()
    match = None
    for rec in _brain_latest_intents().values():
        if rec.get("status") != "approved":
            continue
        if rec.get("intent_id") in consumed:
            continue
        if rec.get("method") != method:
            continue
        if rec.get("intent_hash") != want:
            continue
        if _brain_intent_hash_any(rec) != rec["intent_hash"]:
            continue
        match = rec
        break
    if match is None:
        log(f"BRAIN-GATE DENY method={method} chain={chain_name} "
            f"hash={want[:12]} alasan=tidak ada intent Brain approved "
            f"yang cocok / sudah terpakai")
        raise RuntimeError(
            "brain approval required: no approved Brain intent matches this "
            "signing payload (or it was already used)")
    # Consume BEFORE signing (fail-closed against replay).
    _brain_append_jsonl(BRAIN_CONSUMED_PATH, {
        "ts": _brain_now_iso(), "intent_id": match["intent_id"],
        "intent_hash": match["intent_hash"], "chain": chain_name,
        "method": method, "via": "ws",
    })
    brain_audit_ws("ws_consumed", match,
                   detail="jalur=ws intent sign dikonsumsi gerbang walletd "
                          "sebelum signing; hasil dicatat pada ws_delivered")
    log(f"BRAIN-GATE ALLOW intent={match['intent_id']} chain={chain_name} "
        f"method={method}")
    return match

async def elysium_gas_price(fc):
    """Elysium gas price = eth_gasPrice chain APA ADANYA (revisi owner
    2 Nov 2026 22:18 WIB: fee ikut gwei chain). Clamp 0,1-0,2 gwei dan
    lift baseFee BERPENGALI (elysium_fee_with_basefee, fix 2026-10-29)
    DICABUT. Satu-satunya penyesuaian di titik panggil: lantai mekanik
    penerimaan chain — gasPrice diangkat ke TEPAT baseFee blok pending
    bila usulan chain berada di bawahnya (uji 2 Nov 2026: endpoint fast
    mengusulkan 0,1 gwei persis sementara baseFee 0,10005 gwei, dan
    chain menolaknya 'max fee per gas less than block base fee')."""
    return int(await fc.call("eth_gasPrice"), 16)

def encode_addr(a):
    return a[2:].lower().zfill(64)

def encode_uint(n):
    return hex(n)[2:].zfill(64)

async def dex_swap_prepare(fc, address, router, token_in, token_out, amount_in_wei,
                            slippage_bps=50, deadline_sec=300):
    """Prepare DEX swap with slippage protection.
    Returns tx params dict with amountOutMin calculated.
    slippage_bps: 50 = 0.5%, 100 = 1%."""
    import time as _t
    # getAmountsOut(uint amountIn, address[] path) -> uint[]
    # selector: 0xd06ca61f
    path = token_in[2:].lower().zfill(64) + token_out[2:].lower().zfill(64)
    # encode: offset(0x40) + length(2) + token_in + token_out
    data = ("0xd06ca61f"
            + encode_uint(amount_in_wei)
            + encode_uint(0x40)  # offset to path
            + encode_uint(2)     # path length
            + encode_addr(token_in)
            + encode_addr(token_out))
    out_hex = await fc.call("eth_call", [{"to": router, "data": data}, "latest"])
    # amounts[1] is at offset 64+32=96 bytes = 192 hex chars after 0x
    amounts_out = int(out_hex[2+64:2+128], 16) if len(out_hex) > 130 else int(out_hex, 16)
    # Actually getAmountsOut returns array; second element is output
    # out_hex = 0x + [offset][length][amount0][amount1] — amount1 at chars 2+192:2+256
    if len(out_hex) >= 2 + 256:
        amounts_out = int(out_hex[2+192:2+256], 16)
    amount_out_min = amounts_out * (10000 - slippage_bps) // 10000
    deadline = int(_t.time()) + deadline_sec
    # swapExactTokensForTokens(uint amountIn, uint amountOutMin, address[] path, address to, uint deadline)
    # selector: 0x38ed1739
    swap_data = ("0x38ed1739"
                 + encode_uint(amount_in_wei)
                 + encode_uint(amount_out_min)
                 + encode_uint(0xa0)  # offset path
                 + encode_addr(address)  # to
                 + encode_uint(deadline)
                 + encode_uint(2)
                 + encode_addr(token_in)
                 + encode_addr(token_out))
    return {
        "to": router,
        "data": swap_data,
        "value": "0x0",
        "quote": {"amountOut": str(amounts_out), "amountOutMin": str(amount_out_min),
                  "slippageBps": slippage_bps},
    }

async def handle(ws):
    ac = acct()
    address = ac.address
    log(f"extension connected from {ws.remote_address}")
    async for raw in ws:
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params", [])
        # Per-request chain hint from the bridge (elysium provider sends
        # "0x185d9"; older providers send nothing -> default chain).
        # Resolved INSIDE the try below via resolve_chain_hint (fix
        # 2026-10-30): an explicit unknown chain is rejected with a 4902
        # JSON-RPC error instead of being silently mapped to BSC; only
        # an absent/empty hint keeps the default chain.
        chain_hint = DEFAULT_CHAIN
        log(f"REQ {method} id={mid}")
        gate_rec = None  # Brain intent consumed by this WS request, if any
        try:
            chain_hint = resolve_chain_hint(msg.get("chain"))
            if method in ("eth_requestAccounts", "eth_accounts"):
                res = [address]
            elif method == "eth_chainId":
                res = chain_hint
            elif method == "net_version":
                res = str(int(chain_hint, 16))
            elif method == "personal_sign":
                # Brain gate v2 (2026-11-02, keputusan owner 22:14 WIB):
                # message signing via WS is allowed ONLY against an
                # approved, unconsumed Brain sign intent covering this
                # exact message; the intent is consumed before signing.
                # params: [message, address]
                message = params[0]
                if not isinstance(message, str):
                    raise RuntimeError("personal_sign: message param must be a string")
                gate_rec = brain_gate_ws_sign(chain_hint, "personal_sign",
                                              message)
                sig = ac.sign_message(encode_defunct(hexstr=message) if message.startswith("0x")
                                                     else encode_defunct(text=message))
                res = sig.signature.hex()
                brain_audit_ws("ws_delivered", gate_rec,
                               detail="jalur=ws personal_sign ditandatangani "
                                      "via walletd WS (gerbang Brain v2); "
                                      "signature=" + res)
                log(f"signed message id={mid}")
            elif method == "eth_sendTransaction":
                tx = dict(params[0] if isinstance(params, list) else params)
                _raw_chain = tx.pop("chainId", None)
                # Explicit tx chainId must be supported (fix 2026-10-30):
                # an unknown chainId is rejected, never remapped to the
                # hint/default chain. Absent/empty chainId -> the hint.
                _no_chain = (_raw_chain is None or
                             (isinstance(_raw_chain, str) and not _raw_chain.strip()))
                chain_hex = chain_hint if _no_chain else resolve_chain_hint(_raw_chain)
                url, cid = RPCS[chain_hex]
                # Brain single gate (2026-10-30): extension/WS sends need a
                # matching approved Brain intent, consumed before signing.
                # Raises RuntimeError 'brain approval required: ...' when
                # there is none. walletd policy_check below stays layer 2.
                gate_rec = brain_gate_ws(chain_hex, tx)
                fc = fast_client(chain_hex)
                ok, reason = policy_check(tx, chain_hex)
                if not ok:
                    raise RuntimeError(f"policy denied: {reason}")
                t0 = time.time()
                # Fast path: local nonce, cached fees, cached gas limit — 1 RTT total
                if "nonce" not in tx:
                    tx["nonce"] = await fc.get_nonce(address)
                if chain_hex == ELYSIUM_CHAIN_HEX:
                    # Elysium: fee ikut chain (revisi owner 2 Nov 2026
                    # 22:18 WIB) — gasPrice = eth_gasPrice chain apa
                    # adanya; clamp 0,1/0,2 gwei + lift baseFee DICABUT.
                    # Field EIP-1559 tetap dibuang: Elysium chain legacy,
                    # itu mekanik penerimaan chain-nya sendiri.
                    tx.pop("maxFeePerGas", None)
                    tx.pop("maxPriorityFeePerGas", None)
                    tx.pop("type", None)
                    if "gasPrice" in tx:
                        _gp = tx["gasPrice"]
                        tx["gasPrice"] = int(_gp, 16) if isinstance(_gp, str) else int(_gp)
                    else:
                        tx["gasPrice"] = await elysium_gas_price(fc)
                    # Mekanik penerimaan chain (pengecualian yang diizinkan
                    # revisi 22:18): legacy gasPrice DI BAWAH baseFee blok
                    # inklusi tidak akan pernah diterima chain — naikkan
                    # ke TEPAT baseFee blok "pending" (baseFee yang akan
                    # berlaku di blok inklusi; baseFee Elysium merayap naik
                    # ~0,01% per blok, jadi baseFee "latest" sudah basi
                    # saat tx mengantre satu blok). Ini bukan markup
                    # (tanpa pengali/margin di atas baseFee chain) dan
                    # bukan cap (tidak ada batas atas).
                    _bf = 0
                    for _tag in ("pending", "latest"):
                        try:
                            _blk = await fc.call("eth_getBlockByNumber", [_tag, False])
                            _bf = int(_blk["baseFeePerGas"], 16) if isinstance(_blk, dict) and _blk.get("baseFeePerGas") else 0
                        except Exception:
                            _bf = 0
                        if _bf:
                            break
                    if _bf and tx["gasPrice"] < _bf:
                        tx["gasPrice"] = _bf
                fees = fc.fees()
                if not fees:
                    await fc.refresh_fees()
                    fees = fc.fees()
                if "gasPrice" not in tx and "maxFeePerGas" not in tx:
                    if fees.get("type") == 2:
                        tx["maxFeePerGas"] = fees["maxFeePerGas"]
                        tx["maxPriorityFeePerGas"] = fees["maxPriorityFeePerGas"]
                        tx["type"] = 2
                    else:
                        tx["gasPrice"] = fees.get("gasPrice",
                            int(await fc.call("eth_gasPrice"), 16))
                if "gas" not in tx:
                    cached = fc.gas_limit(tx.get("to"), tx.get("data", "0x"))
                    if cached:
                        tx["gas"] = cached
                    else:
                        # estimate once, cache for next time
                        try:
                            est = await fc.call("eth_estimateGas", [{
                                "from": address, "to": tx.get("to"),
                                "data": tx.get("data", "0x"),
                                "value": tx.get("value", "0x0")}])
                            gl = int(est, 16)
                            fc.set_gas_limit(tx.get("to"), tx.get("data", "0x"), gl)
                            tx["gas"] = int(gl * 1.3)
                        except Exception:
                            tx["gas"] = 200000
                if "value" not in tx:
                    tx["value"] = 0
                tx["chainId"] = cid
                # normalize hex fields
                for k in ("gas", "gasPrice", "nonce", "value", "maxFeePerGas", "maxPriorityFeePerGas"):
                    if k in tx and isinstance(tx[k], str) and tx[k].startswith("0x"):
                        tx[k] = int(tx[k], 16)
                signed = ac.sign_transaction(tx)
                try:
                    h = await fc.call("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()])
                except RuntimeError as e:
                    if "nonce" in str(e).lower():
                        fc.nonce_resync()
                    raise
                ms = (time.time() - t0) * 1000
                log(f"TX {h} in {ms:.0f}ms (policy: {reason})")
                res = h
                if gate_rec is not None:
                    brain_audit_ws("ws_delivered", gate_rec, tx_hash=h,
                                   detail="jalur=ws eth_sendTransaction terkirim via walletd WS (gerbang Brain)")
            elif method in ("eth_call", "eth_estimateGas", "eth_getBalance",
                            "eth_blockNumber", "eth_gasPrice", "eth_getTransactionCount",
                            "eth_getTransactionReceipt", "eth_getCode"):
                # Use fast client with keep-alive; route to the requesting
                # provider's chain (no hint -> default chain, unchanged).
                res = await fast_client(chain_hint).call(method, params)
            elif method == "wallet_switchEthereumChain":
                # Stateless daemon: each provider is pinned to its chain_hint.
                # Switching to the provider's own chain is a no-op success;
                # anything else is refused (EIP-1193 4902).
                target = norm_chain_hex((params[0] or {}).get("chainId")) if params else None
                if target is None:
                    raise RuntimeError("wallet_switchEthereumChain: missing chainId")
                if target == chain_hint:
                    res = None
                else:
                    raise RuntimeError("4902: chain not supported by this wallet")
            elif method == "wallet_addEthereumChain":
                target = norm_chain_hex((params[0] or {}).get("chainId")) if params else None
                if target is None:
                    raise RuntimeError("wallet_addEthereumChain: missing chainId")
                if target in RPCS:
                    res = None
                else:
                    raise RuntimeError("4902: chain not supported by this wallet")
            elif method == "eth_signTypedData_v4":
                # Brain gate v2 (2026-11-02, keputusan owner 22:14 WIB):
                # same gate as personal_sign — approved Brain sign intent
                # over the canonical-JSON payload, consumed before signing.
                # params: [address, typedData] (typedData dict or JSON string)
                signer_addr = params[0] if len(params) > 0 else None
                if signer_addr and signer_addr.lower() != address.lower():
                    raise RuntimeError("4001: signTypedData address does not match the wallet address")
                typed = params[1] if len(params) > 1 else None
                if isinstance(typed, str):
                    typed = json.loads(typed)
                gate_rec = brain_gate_ws_sign(
                    chain_hint, "eth_signTypedData_v4",
                    _brain_canonical_json(typed))
                from eth_account.messages import encode_typed_data
                # eth_account 0.14: bentuk 3-arg TIDAK BOLEH memuat kunci
                # "EIP712Domain" di types (gagal "Unable to determine primary
                # type"); pakai full_message yang menerima typed-data utuh.
                try:
                    signable = encode_typed_data(full_message=typed)
                except Exception:
                    _types = {k: v for k, v in typed.get("types", {}).items()
                              if k != "EIP712Domain"}
                    signable = encode_typed_data(typed.get("domain", {}), _types,
                                                 typed.get("message", {}))
                sig = ac.sign_message(signable)
                res = sig.signature.hex()
                brain_audit_ws("ws_delivered", gate_rec,
                               detail="jalur=ws eth_signTypedData_v4 "
                                      "ditandatangani via walletd WS "
                                      "(gerbang Brain v2); signature=" + res)
                log(f"signed typed data id={mid}")
            # ---- Solana ----
            elif method == "sol_connect":
                res = {"publicKey": str(sol_kp().pubkey())}
            elif method == "sol_signMessage":
                m = params[0]
                raw = base64.b64decode(m) if not m.startswith("0x") else bytes.fromhex(m[2:])
                sig = sol_kp().sign_message(raw)
                res = {"signature": base64.b64encode(bytes(sig)).decode()}
            elif method == "sol_signAndSendTransaction":
                # Network guard (2026-10-30, fix id 685): broadcasts go to
                # the resolved network only; MAINNET is rejected unless the
                # caller explicitly opted in with {"network": "mainnet",
                # "confirmMainnet": true} in params. Before this fix the
                # RPC URL was hardcoded mainnet, so every broadcast —
                # including devnet work — silently went to mainnet.
                net = sol_network(params)
                if net == "mainnet" and not sol_mainnet_confirmed(params):
                    raise RuntimeError("sol_signAndSendTransaction: mainnet broadcast disabled in daemon (devnet/testnet-only); explicit {\"network\": \"mainnet\", \"confirmMainnet\": true} in params required")
                raw = base64.b64decode(params[0])
                kp = sol_kp()
                from solders.transaction import VersionedTransaction
                try:
                    vtx = VersionedTransaction.from_bytes(raw)
                    sigs = list(vtx.signatures)
                    sigs[0] = kp.sign_message(vtx.message.to_bytes())
                    signed = VersionedTransaction.populate(vtx.message, sigs)
                    payload = base64.b64encode(bytes(signed)).decode()
                except Exception:
                    # fallback: legacy transaction
                    from solders.transaction import Transaction as LegacyTx
                    ltx = LegacyTx.from_bytes(raw)
                    ltx.sign([kp])
                    payload = base64.b64encode(bytes(ltx)).decode()
                h = await sol_rpc_fast("sendTransaction", [payload,
                                                {"skipPreflight": True, "maxRetries": 0}], net)
                log(f"SOL-TX[{net}] {h}")
                res = {"signature": h}
            elif method == "sol_getBalance":
                # Result stays the plain lamports integer (back-compat:
                # callers compare numerically); the network is resolved
                # per-call (default devnet) instead of the old mainnet.
                res = (await sol_rpc_fast("getBalance", [str(sol_kp().pubkey())],
                                          sol_network(params)))["value"]
            # ---- DEX swap with slippage ----
            elif method == "dex_swap":
                # params: {router, tokenIn, tokenOut, amountInWei, slippageBps, chainId}
                p = params[0] if isinstance(params, list) else params
                # Explicit chainId must be supported (fix 2026-10-30);
                # absent/empty -> default chain, as before.
                sch = resolve_chain_hint(p.get("chainId"))
                sfc = fast_client(sch)
                txp = await dex_swap_prepare(
                    sfc, address, p["router"], p["tokenIn"], p["tokenOut"],
                    int(p["amountInWei"]), int(p.get("slippageBps", 50)))
                txp["chainId"] = sch
                res = txp
            # ---- Sui ----
            elif method == "sui_connect":
                res = {"address": sui_address()}
            elif method == "sui_signMessage":
                # params: [base64_or_utf8_message]
                m = params[0]
                raw = base64.b64decode(m) if not m.startswith("0x") else bytes.fromhex(m[2:])
                # Personal message intent: 0x03 (PersonalMessage) + 0x00 + 0x00
                intent_msg = b'\x03\x00\x00' + raw
                sig, pub = sui_sign_intent(intent_msg)
                res = {"signature": base64.b64encode(b'\x00' + sig + pub).decode()}
            elif method == "sui_transfer":
                # params: {to, amountMist, network?}
                p = params[0] if isinstance(params, list) else params
                network = sui_network(p)
                if network == "mainnet":
                    # Safety (2026-10-30): daemon Sui transfers are
                    # testnet-only — never build/execute against mainnet.
                    raise RuntimeError("sui_transfer: mainnet disabled in daemon (testnet-only); use sui_signTransaction with an externally built PTB")
                to_addr = p["to"]
                amount = int(p["amountMist"])
                sender = sui_address()
                # Get unsigned tx via unsafe_transferSui
                unsafe = sui_rpc("unsafe_transferSui", [sender, to_addr, amount, None, None], network)
                tx_bytes = base64.b64decode(unsafe["txBytes"])
                sig = sui_sign_tx_bytes(tx_bytes)
                # Execute
                result = sui_rpc("sui_executeTransactionBlock", [
                    unsafe["txBytes"], [sig],
                    {"showEffects": True}, "WaitForLocalExecution"], network)
                res = {"digest": result.get("digest"), "effects": result.get("effects", {}).get("status", {}), "network": network}
            elif method == "sui_signTransaction":
                # params: [base64_txBytes] — sign pre-built tx, return signature
                tx_bytes = base64.b64decode(params[0])
                res = {"signature": sui_sign_tx_bytes(tx_bytes)}
            elif method == "sui_getBalance":
                # params: [] or [{network}] — read via GraphQL (2026-10-30)
                res = sui_get_balance(sui_address(), sui_network(params))
            # ---- Cosmos/IBC ----
            elif method == "cosmos_connect":
                res = {"address": cosmos_address()}
            elif method == "cosmos_getBalance":
                res = cosmos_rest(f"/cosmos/bank/v1beta1/balances/{cosmos_address()}")["balances"]
            elif method == "cosmos_transfer":
                # params: {to, amountUatom, denom, chainId, gas}
                p = params[0] if isinstance(params, list) else params
                from cosmospy import Transaction as CosmosTx
                addr = cosmos_address()
                acc_num, seq = cosmos_account_info(addr)
                tx = CosmosTx(
                    privkey=cosmos_privkey(),
                    account_num=acc_num,
                    sequence=seq,
                    fee=int(p.get("fee", 5000)),
                    gas=int(p.get("gas", 200000)),
                    fee_denom=p.get("feeDenom", "uatom"),
                    memo=p.get("memo", ""),
                    chain_id=p.get("chainId", "cosmoshub-4"),
                    hrp="cosmos",
                )
                tx.add_transfer(p["to"], int(p["amountUatom"]), p.get("denom", "uatom"))
                pushable = tx.get_pushable()
                # Broadcast via Tendermint RPC
                bc = rpc_call(COSMOS_RPC, "broadcast_tx_sync", [pushable])
                res = {"txhash": bc.get("hash"), "code": bc.get("code")}
            elif method == "cosmos_sign":
                # params: [base64_sign_doc_bytes] — sign arbitrary Cosmos SignDoc
                p = params[0] if isinstance(params, list) else params
                from cosmospy import Transaction as CosmosTx
                # Use eth_account-style secp256k1 signing for raw bytes
                from eth_account import Account as EthAccount
                ac = EthAccount.from_key(cosmos_privkey().hex())
                doc_bytes = base64.b64decode(p) if isinstance(p, str) else p
                sig = ac.sign_message(encode_defunct(doc_bytes))
                res = {"signature": sig.signature.hex()}
            else:
                raise RuntimeError(f"unsupported method {method}")
            await ws.send(json.dumps({"id": mid, "result": res}))
        except Exception as e:
            if gate_rec is not None:
                # Intent was consumed by the Brain gate but the send failed
                # afterwards — record it so the audit trail stays complete
                # (the intent stays burned, fail-closed).
                try:
                    brain_audit_ws("ws_consume_failed", gate_rec,
                                   detail="jalur=ws " + str(e))
                except Exception:
                    pass
            log(f"ERR {method} id={mid}: {e}")
            _code, _message = error_code(str(e))
            await ws.send(json.dumps({"id": mid, "error": {"code": _code, "message": _message}}))

async def main():
    log(f"SuperAgentic Wallet Daemon listening on 127.0.0.1:{PORT} (ws) + 18924 (http)")
    log("AUTH[http] caller token " + ("configured (HTTP POST wajib X-Walletd-Token)"
        if _load_caller_token() else "NOT CONFIGURED — HTTP POST akan ditolak semua (fail-closed)"))
    for _ch in RPCS:
        log(f"RPC-ENDPOINTS chain={_ch} endpoints={rpc_endpoints_filtered(_ch)} "
            f"blacklist={RPC_BLACKLIST}")
    # start background fee refresher
    asyncio.create_task(fee_refresher())
    # HTTP POST bridge for browser fetch (same JSON protocol as WS)
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading

    parent_handle = handle

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            if not caller_authorized(self.headers.get(CALLER_TOKEN_HEADER)):
                log("AUTH[http] DENY caller token missing/invalid")
                out = json.dumps({"id": None, "result": None, "error": {
                    "code": -32001,
                    "message": "unauthorized: valid X-Walletd-Token header required"}}).encode()
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return
            try:
                msg = json.loads(body)
            except Exception:
                self.send_response(400); self.end_headers(); return
            # run the WS handler logic synchronously via a fake ws
            result = {}
            class FakeWS:
                remote_address = ("http", 0)
                async def send(self, data):
                    result["data"] = data
                def __aiter__(self):
                    return self
                async def __anext__(self):
                    raise StopAsyncIteration
            async def run_one():
                # replicate handle() for a single message
                ac = acct()
                address = ac.address
                mid, method, params = msg.get("id"), msg.get("method"), msg.get("params", [])
                # Same per-request chain hint as the WS handler,
                # resolved inside the try via resolve_chain_hint (fix
                # 2026-10-30): explicit unknown chain -> 4902 rejection
                # instead of a silent remap to BSC; absent -> default.
                chain_hint = DEFAULT_CHAIN
                log(f"REQ[http] {method} id={mid}")
                try:
                    chain_hint = resolve_chain_hint(msg.get("chain"))
                    if method in ("eth_requestAccounts", "eth_accounts"):
                        res = [address]
                    elif method == "eth_chainId":
                        res = chain_hint
                    elif method == "net_version":
                        res = str(int(chain_hint, 16))
                    elif method == "personal_sign":
                        message = params[0]
                        sig = ac.sign_message(encode_defunct(hexstr=message) if message.startswith("0x")
                                                             else encode_defunct(text=message))
                        res = sig.signature.hex()
                    elif method == "eth_sendTransaction":
                        tx = dict(params[0] if isinstance(params, list) else params)
                        _raw_chain = tx.pop("chainId", None)
                        # Explicit tx chainId must be supported (fix
                        # 2026-10-30); absent/empty -> the request hint.
                        _no_chain = (_raw_chain is None or
                                     (isinstance(_raw_chain, str) and not _raw_chain.strip()))
                        chain_hex = chain_hint if _no_chain else resolve_chain_hint(_raw_chain)
                        url, cid = RPCS[chain_hex]
                        ok, reason = policy_check(tx, chain_hex)
                        if not ok:
                            raise RuntimeError(f"policy denied: {reason}")
                        t0 = time.time()
                        if "nonce" not in tx:
                            tx["nonce"] = int(rpc_call_chain(chain_hex, "eth_getTransactionCount", [address, "pending"]), 16)
                        if chain_hex == ELYSIUM_CHAIN_HEX:
                            # Elysium: fee ikut chain (revisi owner 2 Nov
                            # 2026 22:18 WIB) — gasPrice = eth_gasPrice
                            # chain apa adanya; clamp 0,1/0,2 gwei dan
                            # lift baseFee DICABUT di jalur HTTP juga.
                            tx.pop("maxFeePerGas", None)
                            tx.pop("maxPriorityFeePerGas", None)
                            tx.pop("type", None)
                            if "gasPrice" not in tx:
                                tx["gasPrice"] = int(rpc_call_chain(chain_hex, "eth_gasPrice"), 16)
                            # Mekanik penerimaan chain (pengecualian yang
                            # diizinkan revisi 22:18): naikkan ke TEPAT
                            # baseFee blok "pending" bila di bawahnya —
                            # tanpa pengali, tanpa cap. (Detail di WS.)
                            _bf = 0
                            for _tag in ("pending", "latest"):
                                try:
                                    _blk = rpc_call_chain(chain_hex, "eth_getBlockByNumber", [_tag, False])
                                    _bf = int(_blk["baseFeePerGas"], 16) if isinstance(_blk, dict) and _blk.get("baseFeePerGas") else 0
                                except Exception:
                                    _bf = 0
                                if _bf:
                                    break
                            _gp2 = tx["gasPrice"]
                            _gp2 = int(_gp2, 16) if isinstance(_gp2, str) else int(_gp2)
                            if _bf and _gp2 < _bf:
                                tx["gasPrice"] = _bf
                        elif "gasPrice" not in tx and "maxFeePerGas" not in tx:
                            tx["gasPrice"] = int(rpc_call_chain(chain_hex, "eth_gasPrice"), 16)
                        if "gas" not in tx:
                            tx["gas"] = 200000
                        if "value" not in tx:
                            tx["value"] = 0
                        tx["chainId"] = cid
                        for k in ("gas", "gasPrice", "nonce", "value"):
                            if isinstance(tx[k], str) and tx[k].startswith("0x"):
                                tx[k] = int(tx[k], 16)
                        signed = ac.sign_transaction(tx)
                        h = rpc_call_chain(chain_hex, "eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()])
                        ms = (time.time() - t0) * 1000
                        log(f"TX[http] {h} in {ms:.0f}ms")
                        res = h
                    elif method in ("eth_call", "eth_estimateGas", "eth_getBalance",
                                    "eth_blockNumber", "eth_gasPrice", "eth_getTransactionCount",
                                    "eth_getTransactionReceipt", "eth_getCode"):
                        res = rpc_call_chain(chain_hint, method, params)
                    elif method == "wallet_switchEthereumChain":
                        target = norm_chain_hex((params[0] or {}).get("chainId")) if params else None
                        if target is None:
                            raise RuntimeError("wallet_switchEthereumChain: missing chainId")
                        if target == chain_hint:
                            res = None
                        else:
                            raise RuntimeError("4902: chain not supported by this wallet")
                    elif method == "wallet_addEthereumChain":
                        target = norm_chain_hex((params[0] or {}).get("chainId")) if params else None
                        if target is None:
                            raise RuntimeError("wallet_addEthereumChain: missing chainId")
                        if target in RPCS:
                            res = None
                        else:
                            raise RuntimeError("4902: chain not supported by this wallet")
                    elif method == "eth_signTypedData_v4":
                        signer_addr = params[0] if len(params) > 0 else None
                        if signer_addr and signer_addr.lower() != address.lower():
                            raise RuntimeError("4001: signTypedData address does not match the wallet address")
                        typed = params[1] if len(params) > 1 else None
                        if isinstance(typed, str):
                            typed = json.loads(typed)
                        from eth_account.messages import encode_typed_data
                        # eth_account 0.14: bentuk 3-arg TIDAK BOLEH memuat kunci
                        # "EIP712Domain" di types (gagal "Unable to determine
                        # primary type"); pakai full_message yang menerima
                        # typed-data utuh.
                        try:
                            signable = encode_typed_data(full_message=typed)
                        except Exception:
                            _types = {k: v for k, v in typed.get("types", {}).items()
                                      if k != "EIP712Domain"}
                            signable = encode_typed_data(typed.get("domain", {}), _types,
                                                         typed.get("message", {}))
                        sig = ac.sign_message(signable)
                        res = sig.signature.hex()
                    # ---- Solana ----
                    elif method == "sol_connect":
                        res = {"publicKey": str(sol_kp().pubkey())}
                    elif method == "sol_signMessage":
                        # params: [base64_or_utf8_message]
                        m = params[0]
                        raw = base64.b64decode(m) if not m.startswith("0x") else bytes.fromhex(m[2:])
                        sig = sol_kp().sign_message(raw)
                        res = {"signature": base64.b64encode(bytes(sig)).decode()}
                    elif method == "sol_signAndSendTransaction":
                        # Network guard (2026-10-30, fix id 685): same rule
                        # as the WS handler — MAINNET broadcast is rejected
                        # unless the caller explicitly opted in with
                        # {"network": "mainnet", "confirmMainnet": true}.
                        net = sol_network(params)
                        if net == "mainnet" and not sol_mainnet_confirmed(params):
                            raise RuntimeError("sol_signAndSendTransaction: mainnet broadcast disabled in daemon (devnet/testnet-only); explicit {\"network\": \"mainnet\", \"confirmMainnet\": true} in params required")
                        raw = base64.b64decode(params[0])
                        kp = sol_kp()
                        from solders.transaction import VersionedTransaction
                        try:
                            vtx = VersionedTransaction.from_bytes(raw)
                            sigs = list(vtx.signatures)
                            sigs[0] = kp.sign_message(vtx.message.to_bytes())
                            signed = VersionedTransaction.populate(vtx.message, sigs)
                            payload = base64.b64encode(bytes(signed)).decode()
                        except Exception:
                            from solders.transaction import Transaction as LegacyTx
                            ltx = LegacyTx.from_bytes(raw)
                            ltx.sign([kp])
                            payload = base64.b64encode(bytes(ltx)).decode()
                        h = sol_rpc("sendTransaction", [payload,
                                                        {"skipPreflight": True, "maxRetries": 2}], net)
                        log(f"SOL-TX[http][{net}] {h}")
                        res = {"signature": h}
                    elif method == "sol_getBalance":
                        # Plain lamports integer (back-compat), resolved
                        # per-call network (default devnet).
                        res = sol_rpc("getBalance", [str(sol_kp().pubkey())],
                                      sol_network(params))["value"]
                    elif method == "sui_connect":
                        res = {"address": sui_address()}
                    elif method == "sui_signMessage":
                        m = params[0]
                        raw = base64.b64decode(m) if not m.startswith("0x") else bytes.fromhex(m[2:])
                        # Personal message intent: 0x03 (PersonalMessage) + 0x00 + 0x00
                        intent_msg = b'\x03\x00\x00' + raw
                        sig, pub = sui_sign_intent(intent_msg)
                        res = {"signature": base64.b64encode(b'\x00' + sig + pub).decode()}
                    elif method == "sui_signTransaction":
                        tx_bytes = base64.b64decode(params[0])
                        res = {"signature": sui_sign_tx_bytes(tx_bytes)}
                    elif method == "sui_transfer":
                        p = params[0] if isinstance(params, list) else params
                        network = sui_network(p)
                        if network == "mainnet":
                            # Safety (2026-10-30): testnet-only transfers.
                            raise RuntimeError("sui_transfer: mainnet disabled in daemon (testnet-only); use sui_signTransaction with an externally built PTB")
                        sender = sui_address()
                        unsafe = sui_rpc("unsafe_transferSui", [sender, p["to"], int(p["amountMist"]), None, None], network)
                        tx_bytes = base64.b64decode(unsafe["txBytes"])
                        sig = sui_sign_tx_bytes(tx_bytes)
                        result = sui_rpc("sui_executeTransactionBlock", [
                            unsafe["txBytes"], [sig],
                            {"showEffects": True}, "WaitForLocalExecution"], network)
                        res = {"digest": result.get("digest"), "effects": result.get("effects", {}).get("status", {}), "network": network}
                    elif method == "sui_getBalance":
                        # Read via GraphQL (2026-10-30); params [] or [{network}]
                        res = sui_get_balance(sui_address(), sui_network(params))
                    elif method == "cosmos_connect":
                        res = {"address": cosmos_address()}
                    elif method == "cosmos_getBalance":
                        addr = cosmos_address()
                        res = cosmos_rest(f"/cosmos/bank/v1beta1/balances/{addr}")["balances"]
                    elif method == "cosmos_transfer":
                        p = params[0] if isinstance(params, list) else params
                        from cosmospy import Transaction as CosmosTx
                        addr = cosmos_address()
                        acc_num, seq = cosmos_account_info(addr)
                        tx = CosmosTx(
                            privkey=cosmos_privkey(),
                            account_num=acc_num,
                            sequence=seq,
                            fee=int(p.get("fee", 5000)),
                            gas=int(p.get("gas", 200000)),
                            fee_denom=p.get("feeDenom", "uatom"),
                            memo=p.get("memo", ""),
                            chain_id=p.get("chainId", "cosmoshub-4"),
                            hrp="cosmos",
                        )
                        tx.add_transfer(p["to"], int(p["amountUatom"]), p.get("denom", "uatom"))
                        pushable = tx.get_pushable()
                        bc = rpc_call(COSMOS_RPC, "broadcast_tx_sync", [pushable])
                        res = {"txhash": bc.get("hash"), "code": bc.get("code")}
                    else:
                        raise RuntimeError(f"unsupported method {method}")
                    await FakeWS().send(json.dumps({"id": mid, "result": res}))
                except Exception as e:
                    log(f"ERR[http] {method} id={mid}: {e}")
                    _code, _message = error_code(str(e))
                    await FakeWS().send(json.dumps({"id": mid, "error": {"code": _code, "message": _message}}))
            asyncio.run(run_one())
            data = json.loads(result["data"])
            out = json.dumps({"id": data["id"], "result": data.get("result"), "error": data.get("error")}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        def do_OPTIONS(self):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Walletd-Token")
            self.end_headers()
        def log_message(self, *a):
            pass

    def serve_http():
        HTTPServer(("127.0.0.1", 18924), H).serve_forever()
    threading.Thread(target=serve_http, daemon=True).start()

    async with websockets.serve(handle, "127.0.0.1", PORT):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
