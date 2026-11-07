#!/usr/bin/env python3
"""BrainD pairing CLI (Fase 2) — scoped session tokens, Binance-style.

Flow:
  agent side : pair.py begin [--label X] [--chains 11155111,99801]
               -> prints a pairing id + secret (and a QR of the payload)
  owner side : pair.py approve --pairing-id brd_xxxx [--ttl-hours 48]
               (needs the master token; run it on the signer host or use
               the web approver) — this is the only step that can create
               a session
  agent side : pair.py poll --pairing-id brd_xxxx --secret ... \
                 --save state/session.token
               -> receives the session token exactly once, saves it 0600

The agent then uses the session token instead of the master token:
  BRAIND_TOKEN_FILE=state/session.token python3 bin/wd.py balance sepolia

Owner utilities: pair.py list / pair.py revoke --session-id sess_xxxx

Config (env): BRAIND_WALLETD_URL (default http://127.0.0.1:18924),
BRAIND_TOKEN_FILE (master token; default <repo>/state/walletd.token).
"""
import argparse
import json
import os
import sys
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(BASE_DIR)
STATE_DIR = os.environ.get("BRAIND_STATE_DIR", os.path.join(REPO_DIR, "state"))
WALLETD_URL = os.environ.get("BRAIND_WALLETD_URL", "http://127.0.0.1:18924")
TOKEN_FILE = os.environ.get("BRAIND_TOKEN_FILE",
                            os.path.join(STATE_DIR, "walletd.token"))


def rpc(method, params=None, token=None):
    body = json.dumps({"id": 1, "method": method,
                       "params": params or {}}).encode()
    req = urllib.request.Request(WALLETD_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("X-Walletd-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        data = json.loads(e.read())
    if data.get("error"):
        raise SystemExit(f"walletd error: {data['error'].get('message')}")
    return data.get("result")


def master_token():
    with open(TOKEN_FILE) as f:
        return f.read().strip()


def cmd_begin(a):
    res = rpc("braind_pairBegin", {
        "label": a.label,
        "chains": [c.strip() for c in a.chains.split(",") if c.strip()],
        "methods": [m.strip() for m in a.methods.split(",") if m.strip()],
    })
    payload = {"braind_pair": 1, "url": WALLETD_URL,
               "pairing_id": res["pairing_id"], "secret": res["secret"]}
    print(f"pairing_id : {res['pairing_id']}")
    print(f"secret     : {res['secret']}")
    print(f"expires in : {res['expires_in']}s — approve it on the signer "
          f"host (pair.py approve) or in the web approver.")
    print(f"payload    : {json.dumps(payload)}")
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(json.dumps(payload))
        qr.make()
        qr.print_ascii(invert=True)
    except ImportError:
        print("(install 'qrcode' for a scannable QR; the payload line "
              "above carries the same data)")


def cmd_approve(a):
    res = rpc("braind_pairApprove", {
        "pairing_id": a.pairing_id,
        "label": a.label,
        "chains": [c.strip() for c in a.chains.split(",") if c.strip()] or None,
        "methods": [m.strip() for m in a.methods.split(",") if m.strip()] or None,
        "ttl_hours": a.ttl_hours,
    }, token=master_token())
    print(json.dumps(res, indent=1))


def cmd_poll(a):
    deadline = time.time() + a.timeout
    while time.time() < deadline:
        res = rpc("braind_pairPoll", {"pairing_id": a.pairing_id,
                                      "secret": a.secret})
        st = res.get("status")
        if st == "approved" and res.get("session_token"):
            path = a.save
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(res["session_token"] + "\n")
            print(f"session {res.get('session_id')} token saved to {path} "
                  f"(0600). Use it with:")
            print(f"  BRAIND_TOKEN_FILE={path} python3 bin/wd.py ...")
            return
        if st in ("expired",):
            raise SystemExit("pairing expired — run begin again")
        time.sleep(3)
    raise SystemExit("timeout waiting for approval")


def cmd_list(a):
    tok = master_token()
    print("sessions:")
    print(json.dumps(rpc("braind_listSessions", token=tok), indent=1))
    print("pairings:")
    print(json.dumps(rpc("braind_listPairings", token=tok), indent=1))


def cmd_revoke(a):
    print(json.dumps(rpc("braind_revokeSession",
                         {"session_id": a.session_id},
                         token=master_token()), indent=1))


def main():
    ap = argparse.ArgumentParser(description="BrainD pairing (Fase 2)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("begin")
    p.add_argument("--label", default="agent")
    p.add_argument("--chains", default="*")
    p.add_argument("--methods",
                   default="eth_sendTransaction,personal_sign,"
                           "eth_signTypedData_v4")
    p.set_defaults(fn=cmd_begin)
    p = sub.add_parser("approve")
    p.add_argument("--pairing-id", required=True)
    p.add_argument("--label", default=None)
    p.add_argument("--chains", default="")
    p.add_argument("--methods", default="")
    p.add_argument("--ttl-hours", type=float, default=48)
    p.set_defaults(fn=cmd_approve)
    p = sub.add_parser("poll")
    p.add_argument("--pairing-id", required=True)
    p.add_argument("--secret", required=True)
    p.add_argument("--save", default=os.path.join(STATE_DIR, "session.token"))
    p.add_argument("--timeout", type=int, default=600)
    p.set_defaults(fn=cmd_poll)
    p = sub.add_parser("list")
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("revoke")
    p.add_argument("--session-id", required=True)
    p.set_defaults(fn=cmd_revoke)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
