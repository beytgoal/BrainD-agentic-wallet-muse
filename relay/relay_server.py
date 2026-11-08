#!/usr/bin/env python3
"""BrainD relay — a tiny PUBLIC proposal inbox for cloud agents (e.g. Meta Muse).

Why it exists: a cloud agent cannot reach the signer (it listens on
127.0.0.1, and must never be exposed). The agent talks to THIS service
instead. The relay holds no keys, cannot sign, cannot approve and cannot talk
to walletd. It only stores proposals; a puller on the signer machine
(relay/puller.py) fetches them with OUTBOUND requests and feeds them into
`brain.py propose`, where the normal policy + human approval gate applies.

Two credentials, two roles:
  RELAY_AGENT_KEY   the agent: POST /v1/proposals, GET /v1/proposals[/id]
  RELAY_PULLER_KEY  the signer-side puller: /v1/puller/*  (never give to the agent)

Only plain native/contract-call transactions (eth_sendTransaction) can be
proposed. Message signing (personal_sign / typed data) is deliberately NOT
exposed here.

Run behind an HTTPS reverse proxy (Caddy/nginx). Stdlib only.
Config (env): RELAY_AGENT_KEY, RELAY_PULLER_KEY (>=32 chars, different),
RELAY_BIND=127.0.0.1, RELAY_PORT=18930, RELAY_DB=./relay.db,
RELAY_CHAINS=sepolia,base-sepolia   (mainnets must be added on purpose),
RELAY_MAX_VALUE_ETHER=0.01, RELAY_RATE_PER_MIN=10, RELAY_MAX_PENDING=20,
RELAY_TTL_SECONDS=3600, RELAY_PUBLIC_URL=https://relay.example.com
"""
import hmac, json, os, re, secrets, sqlite3, sys, threading, time
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
DATA_RE = re.compile(r"^0x(?:[0-9a-fA-F]{2})*$")
VALUE_RE = re.compile(r"^\d{1,6}(?:\.\d{1,18})?$")
CHAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,23}$")
MAX_BODY = 16 * 1024
MAX_DATA_BYTES = 4096
ACTIVE = ("pending", "claimed", "queued", "approved")
PULLER_STATUSES = {"queued", "approved", "rejected", "executed", "failed"}
# allowed puller transitions (current -> next)
TRANSITIONS = {
    "claimed": {"queued", "rejected", "failed"},
    "queued": {"approved", "rejected", "executed", "failed"},
    "approved": {"executed", "failed", "rejected"},
}


class Config:
    def __init__(self, env=os.environ):
        self.agent_key = env.get("RELAY_AGENT_KEY", "")
        self.puller_key = env.get("RELAY_PULLER_KEY", "")
        for name, k in (("RELAY_AGENT_KEY", self.agent_key), ("RELAY_PULLER_KEY", self.puller_key)):
            if len(k) < 32:
                raise SystemExit(f"{name} must be set and at least 32 characters")
        if hmac.compare_digest(self.agent_key, self.puller_key):
            raise SystemExit("RELAY_AGENT_KEY and RELAY_PULLER_KEY must be different")
        self.bind = env.get("RELAY_BIND", "127.0.0.1")
        self.port = int(env.get("RELAY_PORT", "18930"))
        self.db = env.get("RELAY_DB", "relay.db")
        self.chains = [c.strip() for c in env.get("RELAY_CHAINS", "sepolia,base-sepolia").split(",") if c.strip()]
        self.max_value = Decimal(env.get("RELAY_MAX_VALUE_ETHER", "0.01"))
        self.rate = int(env.get("RELAY_RATE_PER_MIN", "10"))
        self.max_pending = int(env.get("RELAY_MAX_PENDING", "20"))
        self.ttl = int(env.get("RELAY_TTL_SECONDS", "3600"))
        self.public_url = env.get("RELAY_PUBLIC_URL", "https://relay.example.com")


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        with self._c() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS proposals(
              id TEXT PRIMARY KEY, created REAL, updated REAL, status TEXT,
              chain TEXT, method TEXT, to_addr TEXT, value TEXT, data TEXT,
              reason TEXT, intent_id TEXT, intent_hash TEXT, tx_hash TEXT, note TEXT)""")

    def _c(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def expire(self, ttl):
        with self.lock, self._c() as c:
            c.execute("UPDATE proposals SET status='expired', updated=?, note='expired before approval' "
                      "WHERE status IN ('pending','claimed','queued') AND created < ?",
                      (time.time(), time.time() - ttl))

    def create(self, p, rate, max_pending):
        with self.lock, self._c() as c:
            n_recent = c.execute("SELECT COUNT(*) FROM proposals WHERE created > ?",
                                 (time.time() - 60,)).fetchone()[0]
            if n_recent >= rate:
                return None, "rate limit exceeded; try again in a minute"
            n_act = c.execute("SELECT COUNT(*) FROM proposals WHERE status IN (%s)" %
                              ",".join("?" * len(ACTIVE)), ACTIVE).fetchone()[0]
            if n_act >= max_pending:
                return None, "too many open proposals; wait for approvals"
            pid = "prop_" + secrets.token_hex(8)
            now = time.time()
            c.execute("INSERT INTO proposals(id,created,updated,status,chain,method,to_addr,value,data,reason) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (pid, now, now, "pending", p["chain"], p["method"], p["to"], p["value"], p["data"], p["reason"]))
            return pid, None

    def get(self, pid):
        with self._c() as c:
            return c.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()

    def list(self, limit=20):
        with self._c() as c:
            return c.execute("SELECT * FROM proposals ORDER BY created DESC LIMIT ?", (limit,)).fetchall()

    def claim(self):
        with self.lock, self._c() as c:
            r = c.execute("SELECT * FROM proposals WHERE status='pending' ORDER BY created LIMIT 1").fetchone()
            if not r:
                return None
            c.execute("UPDATE proposals SET status='claimed', updated=? WHERE id=?", (time.time(), r["id"]))
            return self.get_in(c, r["id"])

    @staticmethod
    def get_in(c, pid):
        return c.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()

    def active(self):
        with self._c() as c:
            return c.execute("SELECT * FROM proposals WHERE status IN ('queued','approved')").fetchall()

    def report(self, pid, status, intent_id, intent_hash, tx_hash, note):
        with self.lock, self._c() as c:
            r = self.get_in(c, pid)
            if not r:
                return "unknown proposal"
            if status not in TRANSITIONS.get(r["status"], set()):
                return f"transition {r['status']} -> {status} not allowed"
            c.execute("UPDATE proposals SET status=?, updated=?, intent_id=COALESCE(?,intent_id), "
                      "intent_hash=COALESCE(?,intent_hash), tx_hash=COALESCE(?,tx_hash), note=? WHERE id=?",
                      (status, time.time(), intent_id, intent_hash, tx_hash, (note or "")[:300], pid))
            return None


def clean_text(s, lo, hi):
    if not isinstance(s, str):
        raise ValueError("must be a string")
    s = s.strip()
    if not (lo <= len(s) <= hi):
        raise ValueError(f"length must be {lo}-{hi}")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in s):
        raise ValueError("control characters are not allowed")
    return s


def validate_proposal(body, cfg):
    """Strict validation. Raises ValueError with a user-safe message."""
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    extra = set(body) - {"chain", "method", "to", "value", "data", "reason"}
    if extra:
        raise ValueError("unknown fields: " + ",".join(sorted(map(str, extra))[:5]))
    method = body.get("method", "eth_sendTransaction")
    if method != "eth_sendTransaction":
        raise ValueError("only eth_sendTransaction proposals are accepted (message signing is not exposed)")
    chain = body.get("chain")
    if not isinstance(chain, str) or not CHAIN_RE.match(chain) or chain not in cfg.chains:
        raise ValueError("chain must be one of: " + ", ".join(cfg.chains))
    to = body.get("to")
    if not isinstance(to, str) or not ADDR_RE.match(to):
        raise ValueError("to must be a 0x-prefixed 20-byte address")
    value = body.get("value", "0")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = format(Decimal(str(value)), "f")
    if not isinstance(value, str) or not VALUE_RE.match(value):
        raise ValueError("value must be a plain decimal ether amount, e.g. \"0.001\"")
    try:
        if Decimal(value) > cfg.max_value:
            raise ValueError(f"value exceeds relay limit of {cfg.max_value} ether")
    except InvalidOperation:
        raise ValueError("invalid value")
    data = body.get("data", "0x")
    if not isinstance(data, str) or not DATA_RE.match(data) or (len(data) - 2) // 2 > MAX_DATA_BYTES:
        raise ValueError(f"data must be 0x-hex, at most {MAX_DATA_BYTES} bytes")
    reason = clean_text(body.get("reason"), 3, 300)
    return {"chain": chain, "method": method, "to": to, "value": value, "data": data, "reason": reason}


def public_view(r):
    return {k: r[k] for k in ("id", "status", "chain", "to_addr", "value", "reason",
                              "intent_hash", "tx_hash", "note")} | {
        "created": int(r["created"]), "updated": int(r["updated"])}


def puller_view(r):
    return {"id": r["id"], "chain": r["chain"], "method": r["method"], "to": r["to_addr"],
            "value": r["value"], "data": r["data"], "reason": r["reason"],
            "created": int(r["created"]), "intent_id": r["intent_id"], "status": r["status"]}


def openapi(cfg):
    sec = [{"bearerAuth": []}]
    prop = {"type": "object", "required": ["chain", "to", "reason"], "additionalProperties": False,
            "properties": {
                "chain": {"type": "string", "enum": cfg.chains},
                "to": {"type": "string", "description": "recipient / contract, 0x + 40 hex"},
                "value": {"type": "string", "description": f"ether, decimal string, max {cfg.max_value}", "default": "0"},
                "data": {"type": "string", "description": "0x hex calldata", "default": "0x"},
                "reason": {"type": "string", "description": "why (3-300 chars); shown to the human approver"}}}
    return {"openapi": "3.0.3",
            "info": {"title": "BrainD proposal relay", "version": "1.0",
                     "description": "Propose a transaction. It is NOT executed until the wallet owner approves it locally."},
            "servers": [{"url": cfg.public_url}],
            "components": {"securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}}},
            "paths": {
                "/v1/proposals": {
                    "post": {"operationId": "proposeTransaction", "security": sec,
                             "summary": "Propose a transaction for the owner to approve",
                             "requestBody": {"required": True, "content": {"application/json": {"schema": prop}}},
                             "responses": {"201": {"description": "queued for owner approval"}}},
                    "get": {"operationId": "listProposals", "security": sec,
                            "summary": "Recent proposals and their status",
                            "responses": {"200": {"description": "ok"}}}},
                "/v1/proposals/{id}": {
                    "get": {"operationId": "getProposal", "security": sec,
                            "summary": "Status of one proposal (pending, claimed, queued, approved, executed, rejected, failed, expired)",
                            "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                            "responses": {"200": {"description": "ok"}}}}}}


def make_handler(cfg, store):
    class H(BaseHTTPRequestHandler):
        server_version = "braind-relay"
        sys_version = ""

        def log_message(self, fmt, *a):
            sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % a))

        def _send(self, status, obj):
            out = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(out)

        def _key(self):
            h = self.headers.get("Authorization", "")
            if h.lower().startswith("bearer "):
                return h[7:].strip()
            return (self.headers.get("X-API-Key") or "").strip()

        def _role(self):
            k = self._key()
            if k and hmac.compare_digest(k, cfg.agent_key):
                return "agent"
            if k and hmac.compare_digest(k, cfg.puller_key):
                return "puller"
            return None

        def _auth(self, need):
            if self._role() != need:
                time.sleep(0.3)  # slow down key guessing
                self._send(401 if self._role() is None else 403,
                           {"error": "unauthorized" if self._role() is None else "wrong key type for this endpoint"})
                return False
            return True

        def _body(self):
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                self._send(413, {"error": "body too large"})
                return None
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                self._send(400, {"error": "invalid JSON"})
                return None

        def do_GET(self):
            store.expire(cfg.ttl)
            path = self.path.split("?")[0].rstrip("/")
            if path == "/healthz":
                return self._send(200, {"ok": True})
            if path == "/openapi.json":
                return self._send(200, openapi(cfg))
            if path == "/v1/proposals":
                if self._auth("agent"):
                    self._send(200, {"proposals": [public_view(r) for r in store.list()]})
                return
            if path.startswith("/v1/proposals/"):
                if self._auth("agent"):
                    r = store.get(path.rsplit("/", 1)[1])
                    self._send(200, public_view(r)) if r else self._send(404, {"error": "not found"})
                return
            if path == "/v1/puller/active":
                if self._auth("puller"):
                    self._send(200, {"proposals": [puller_view(r) for r in store.active()]})
                return
            self._send(404, {"error": "not found"})

        def do_POST(self):
            store.expire(cfg.ttl)
            path = self.path.split("?")[0].rstrip("/")
            if path == "/v1/proposals":
                if not self._auth("agent"):
                    return
                body = self._body()
                if body is None:
                    return
                try:
                    p = validate_proposal(body, cfg)
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                pid, err = store.create(p, cfg.rate, cfg.max_pending)
                if err:
                    return self._send(429, {"error": err})
                return self._send(201, {"id": pid, "status": "pending",
                                        "message": "Proposal stored. It is NOT executed until the wallet owner approves it."})
            if path == "/v1/puller/claim":
                if self._auth("puller"):
                    r = store.claim()
                    self._send(200, {"proposal": puller_view(r) if r else None})
                return
            m = re.match(r"^/v1/puller/proposals/(prop_[0-9a-f]{16})/status$", path)
            if m:
                if not self._auth("puller"):
                    return
                body = self._body()
                if body is None:
                    return
                st = body.get("status")
                if st not in PULLER_STATUSES:
                    return self._send(400, {"error": "bad status"})
                err = store.report(m.group(1), st, body.get("intent_id"), body.get("intent_hash"),
                                   body.get("tx_hash"), str(body.get("note") or ""))
                return self._send(409 if err else 200, {"error": err} if err else {"ok": True})
            self._send(404, {"error": "not found"})

        def do_PUT(self): self._send(405, {"error": "method not allowed"})
        do_DELETE = do_PATCH = do_PUT

    return H


def main():
    cfg = Config()
    store = Store(cfg.db)
    srv = ThreadingHTTPServer((cfg.bind, cfg.port), make_handler(cfg, store))
    print(f"braind-relay on {cfg.bind}:{cfg.port} chains={cfg.chains} max_value={cfg.max_value} "
          f"(put it behind HTTPS)", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
