#!/usr/bin/env python3
"""BrainD web approver (Fase 2) — the owner's approval page.

One small page, three jobs:
  1. Brain intents: approve / reject pending intents (exact-hash, via the
     brain.py CLI — this page never edits intents.jsonl itself).
  2. Pairings: approve pending pairing requests into scoped sessions.
  3. Sessions: list active session tokens and revoke them.

Auth: the master wallet token (login form; a SHA-256 cookie is set, the
token itself is never stored by the server). Bind stays localhost by
default — expose it only behind your own tunnel/VPN (see
docs/self-host.md).

Env: BRAIND_APPROVER_BIND (127.0.0.1), BRAIND_APPROVER_PORT (18926),
BRAIND_WALLETD_URL, BRAIND_TOKEN_FILE, BRAIND_STATE_DIR.
"""
import hashlib
import hmac
import html
import json
import os
import subprocess
import sys
import urllib.request

from aiohttp import web

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(BASE_DIR)
STATE_DIR = os.environ.get("BRAIND_STATE_DIR", os.path.join(REPO_DIR, "state"))
WALLETD_URL = os.environ.get("BRAIND_WALLETD_URL", "http://127.0.0.1:18924")
TOKEN_FILE = os.environ.get("BRAIND_TOKEN_FILE",
                            os.path.join(STATE_DIR, "walletd.token"))
BRAIN_PY = os.path.join(BASE_DIR, "brain.py")
INTENTS_PATH = os.path.join(STATE_DIR, "intents.jsonl")
BIND = os.environ.get("BRAIND_APPROVER_BIND", "127.0.0.1")
PORT = int(os.environ.get("BRAIND_APPROVER_PORT", "18926"))


def master_token():
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except Exception:
        return None


def authed(request):
    tok = master_token()
    if not tok:
        return False
    cookie = request.cookies.get("braind_auth", "")
    expect = hashlib.sha256(tok.encode()).hexdigest()
    return hmac.compare_digest(cookie, expect)


def rpc(method, params=None):
    body = json.dumps({"id": 1, "method": method,
                       "params": params or {}}).encode()
    req = urllib.request.Request(
        WALLETD_URL, data=body,
        headers={"Content-Type": "application/json",
                 "X-Walletd-Token": master_token() or ""})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        data = json.loads(e.read())
    if data.get("error"):
        raise RuntimeError(data["error"].get("message"))
    return data.get("result")


def pending_intents():
    latest = {}
    order = []
    if os.path.exists(INTENTS_PATH):
        with open(INTENTS_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                iid = rec.get("intent_id")
                if not iid:
                    continue
                if iid not in latest:
                    order.append(iid)
                latest[iid] = rec
    return [latest[i] for i in order if latest[i].get("status") == "queued"]


def run_brain(*args):
    out = subprocess.run([sys.executable, BRAIN_PY, *args],
                         capture_output=True, text=True, timeout=60)
    return (out.stdout + out.stderr).strip()[:400]


CSS = ("body{font-family:system-ui;max-width:860px;margin:2rem auto;"
       "padding:0 1rem}code{word-break:break-all;font-size:.8em}"
       ".card{border:1px solid #ccc;border-radius:8px;padding:1rem;"
       "margin:1rem 0}button{padding:.4rem 1rem;margin-right:.5rem}"
       ".ok{color:green}.err{color:#b00}input{padding:.3rem}")


def page(body, flash=""):
    f = f'<p class="{"err" if flash.startswith("ERR") else "ok"}">{html.escape(flash)}</p>' if flash else ""
    return ("<!doctype html><meta charset=utf-8><title>BrainD Approver</title>"
            f"<style>{CSS}</style><h1>BrainD Approver</h1>{f}{body}")


async def login_form(request):
    return web.Response(text=page(
        '<form method=post action=/login><p>Master token: '
        '<input type=password name=token size=40> '
        '<button>Login</button></p></form>'), content_type="text/html")


async def login(request):
    data = await request.post()
    tok = master_token()
    if tok and hmac.compare_digest(str(data.get("token", "")).strip(), tok):
        resp = web.HTTPFound("/")
        resp.set_cookie("braind_auth",
                        hashlib.sha256(tok.encode()).hexdigest(),
                        httponly=True, samesite="Strict")
        return resp
    return web.Response(text=page("login gagal", "ERR token salah"),
                        content_type="text/html")


async def home(request):
    if not authed(request):
        return await login_form(request)
    flash = request.query.get("msg", "")
    parts = []
    # --- pending pairings ---
    try:
        pairings = [p for p in rpc("braind_listPairings")
                    if p.get("status") == "pending"]
    except Exception as e:
        pairings = []
        parts.append(f'<p class=err>walletd: {html.escape(str(e))}</p>')
    parts.append("<h2>Pairing menunggu</h2>")
    if not pairings:
        parts.append("<p><i>tidak ada</i></p>")
    for p in pairings:
        parts.append(
            '<div class=card><form method=post action=/pairing/approve>'
            f'<input type=hidden name=pairing_id value="{html.escape(p["pairing_id"])}">'
            f'<b>{html.escape(str(p.get("label")))}</b> '
            f'<code>{html.escape(p["pairing_id"])}</code><br>'
            f'chains: <input name=chains value="{html.escape(",".join(map(str, p.get("chains") or [])))}" size=30> '
            f'ttl jam: <input name=ttl_hours value="48" size=4> '
            f'kadaluarsa dalam {p.get("expires_in")}s<br><br>'
            '<button>Approve pairing</button></form></div>')
    # --- active sessions ---
    try:
        sessions = rpc("braind_listSessions")
    except Exception:
        sessions = []
    parts.append("<h2>Sesi aktif</h2>")
    live = [s for s in sessions if not s.get("revoked")]
    if not live:
        parts.append("<p><i>tidak ada</i></p>")
    for s in live:
        parts.append(
            '<div class=card><form method=post action=/session/revoke>'
            f'<input type=hidden name=session_id value="{html.escape(s["id"])}">'
            f'<b>{html.escape(str(s.get("label")))}</b> '
            f'<code>{html.escape(s["id"])}</code><br>'
            f'chains: {html.escape(",".join(map(str, s.get("chains") or [])))} · '
            f'methods: {html.escape(",".join(map(str, s.get("methods") or [])))}<br><br>'
            '<button>Revoke</button></form></div>')
    # --- pending brain intents ---
    parts.append("<h2>Intent Brain menunggu</h2>")
    intents = pending_intents()
    if not intents:
        parts.append("<p><i>tidak ada</i></p>")
    for it in intents:
        summary = {k: it.get(k) for k in
                   ("chain", "method", "to", "value", "caller", "reason")
                   if it.get(k) not in (None, "")}
        parts.append(
            '<div class=card>'
            f'<b>{html.escape(str(summary))}</b><br>'
            f'id: <code>{html.escape(str(it.get("intent_id")))}</code><br>'
            f'hash: <code>{html.escape(str(it.get("intent_hash")))}</code><br><br>'
            '<form method=post action=/intent/approve style="display:inline">'
            f'<input type=hidden name=id value="{html.escape(str(it.get("intent_id")))}">'
            f'<input type=hidden name=hash value="{html.escape(str(it.get("intent_hash")))}">'
            '<button>Approve</button></form>'
            '<form method=post action=/intent/reject style="display:inline">'
            f'<input type=hidden name=id value="{html.escape(str(it.get("intent_id")))}">'
            '<button>Reject</button></form></div>')
    return web.Response(text=page("".join(parts), flash),
                        content_type="text/html")


def _guard(request):
    if not authed(request):
        raise web.HTTPForbidden(text="login dulu")


async def pairing_approve(request):
    _guard(request)
    d = await request.post()
    chains = [c.strip() for c in str(d.get("chains", "")).split(",")
              if c.strip()]
    try:
        res = rpc("braind_pairApprove", {
            "pairing_id": d.get("pairing_id"),
            "chains": chains or None,
            "ttl_hours": float(d.get("ttl_hours", 48))})
        msg = f"pairing disetujui -> sesi {res.get('session_id')}"
    except Exception as e:
        msg = f"ERR {e}"
    return web.HTTPFound(f"/?msg={msg}")


async def session_revoke(request):
    _guard(request)
    d = await request.post()
    try:
        rpc("braind_revokeSession", {"session_id": d.get("session_id")})
        msg = "sesi dicabut"
    except Exception as e:
        msg = f"ERR {e}"
    return web.HTTPFound(f"/?msg={msg}")


async def intent_approve(request):
    _guard(request)
    d = await request.post()
    out = run_brain("approve", "--id", str(d.get("id")),
                    "--hash", str(d.get("hash")), "--by", "web-approver")
    return web.HTTPFound(f"/?msg={out or 'approved'}")


async def intent_reject(request):
    _guard(request)
    d = await request.post()
    out = run_brain("reject", "--id", str(d.get("id")),
                    "--by", "web-approver")
    return web.HTTPFound(f"/?msg={out or 'rejected'}")


@web.middleware
async def same_origin_only(request, handler):
    """CSRF guard: state-changing requests must not come from another origin."""
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin:
            from urllib.parse import urlparse
            if urlparse(origin).netloc != request.headers.get("Host", ""):
                raise web.HTTPForbidden(text="cross-origin request refused")
    return await handler(request)


def main():
    app = web.Application(middlewares=[same_origin_only])
    app.router.add_get("/", home)
    app.router.add_post("/login", login)
    app.router.add_post("/pairing/approve", pairing_approve)
    app.router.add_post("/session/revoke", session_revoke)
    app.router.add_post("/intent/approve", intent_approve)
    app.router.add_post("/intent/reject", intent_reject)
    print(f"BrainD approver on http://{BIND}:{PORT} "
          f"(walletd {WALLETD_URL}, state {STATE_DIR})")
    web.run_app(app, host=BIND, port=PORT, print=None)


if __name__ == "__main__":
    main()
