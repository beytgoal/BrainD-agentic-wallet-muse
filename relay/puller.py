#!/usr/bin/env python3
"""BrainD puller — runs on the SIGNER machine, talks to the relay with
OUTBOUND HTTPS only (the signer never listens on the internet).

Loop:
  1. claim one pending proposal from the relay
  2. re-validate it locally (never trust the relay), then feed it to
     `brain.py propose` -> Brain's policy gate (cap, allowlist) applies
  3. report queued / rejected (+ intent id and hash) back to the relay
  4. watch queued/approved intents; when the OWNER approves one with
     `brain.py approve`, report it, and (only with --auto-deliver) run
     `brain.py deliver`; report executed/failed with the tx hash

The puller never approves anything. Approval stays a human action on the
signer side (brain.py approve / the web approver).

Env / flags:
  RELAY_URL              https://relay.example.com
  RELAY_PULLER_KEY_FILE  file holding the puller key (0600)
  BRAIND_BRAIN_CMD       default: "<python> <repo>/bin/brain.py"
  BRAIND_STATE_DIR, BRAIND_TOKEN_FILE, ... as for brain.py
"""
import argparse, json, os, re, shlex, subprocess, sys, time, urllib.error, urllib.request

ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
DATA_RE = re.compile(r"^0x(?:[0-9a-fA-F]{2})*$")
VALUE_RE = re.compile(r"^\d{1,6}(?:\.\d{1,18})?$")
CHAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,23}$")
HERE = os.path.dirname(os.path.abspath(__file__))


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


class Relay:
    def __init__(self, url, key):
        self.url, self.key = url.rstrip("/"), key

    def call(self, method, path, body=None):
        req = urllib.request.Request(
            self.url + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"{}")


def run_brain(brain_cmd, args, timeout=30):
    """Run brain.py; return (ok, parsed_json_or_None, error_text)."""
    try:
        p = subprocess.run(brain_cmd + args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, None, "brain.py timed out"
    text = p.stdout if p.returncode == 0 else p.stderr
    try:
        data = json.loads(text)
    except Exception:
        data = None
    if p.returncode != 0:
        err = (data or {}).get("error") if isinstance(data, dict) else None
        return False, data, (err or text.strip() or "brain.py failed")[:300]
    return True, data, ""


def valid_local(p):
    """Defense in depth: the relay already validated, but the signer side re-checks."""
    return (p.get("method") == "eth_sendTransaction" and CHAIN_RE.match(str(p.get("chain", "")))
            and ADDR_RE.match(str(p.get("to", ""))) and VALUE_RE.match(str(p.get("value", "")))
            and DATA_RE.match(str(p.get("data", "")))
            and 3 <= len(str(p.get("reason", ""))) <= 300
            and not any(ord(c) < 32 or ord(c) == 127 for c in str(p.get("reason", ""))))


def report(relay, pid, status, **kw):
    body = {"status": status}
    body.update({k: v for k, v in kw.items() if v is not None})
    try:
        relay.call("POST", f"/v1/puller/proposals/{pid}/status", body)
    except urllib.error.HTTPError as e:
        log(f"report {pid} -> {status} refused by relay: HTTP {e.code}")


def handle_claim(relay, brain_cmd, p, caller):
    pid = p["id"]
    if not valid_local(p):
        log(f"{pid} failed local validation")
        return report(relay, pid, "rejected", note="rejected by signer-side validation")
    args = ["propose", f"--caller={caller}", f"--chain={p['chain']}", "--method=eth_sendTransaction",
            f"--to={p['to']}", f"--value={p['value']}", f"--data={p['data']}",
            f"--reason=[relay {pid}] {p['reason']}"]
    ok, data, err = run_brain(brain_cmd, args)
    if not ok:
        log(f"{pid} rejected by Brain policy: {err}")
        return report(relay, pid, "rejected", note="Brain policy: " + err)
    log(f"{pid} queued as intent {data['intent_id']} hash {data['intent_hash'][:12]}…")
    report(relay, pid, "queued", intent_id=data["intent_id"], intent_hash=data["intent_hash"],
           note="waiting for owner approval on the signer")


def handle_active(relay, brain_cmd, item, auto_deliver):
    pid, iid = item["id"], item.get("intent_id")
    if not iid:
        return
    ok, rec, err = run_brain(brain_cmd, ["show", f"--id={iid}"])
    if not ok:
        return log(f"{pid} cannot read intent: {err}")
    st = rec.get("status")
    if st == "rejected":
        report(relay, pid, "rejected", note="rejected by owner")
    elif st == "executed":
        report(relay, pid, "executed", tx_hash=rec.get("tx_hash"))
    elif st == "execution_failed":
        report(relay, pid, "failed", tx_hash=rec.get("tx_hash"), note=str(rec.get("note") or "execution failed"))
    elif st == "approved":
        if item["status"] != "approved":
            report(relay, pid, "approved", note="approved by owner")
        if auto_deliver:
            log(f"{pid} approved -> delivering intent {iid}")
            ok, d, err = run_brain(brain_cmd, ["deliver", f"--id={iid}"], timeout=150)
            if ok:
                report(relay, pid, "executed", tx_hash=(d or {}).get("tx_hash"))
            else:
                report(relay, pid, "failed", note=err)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--relay-url", default=os.environ.get("RELAY_URL"))
    ap.add_argument("--key-file", default=os.environ.get("RELAY_PULLER_KEY_FILE"))
    ap.add_argument("--caller", default="muse-relay")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--auto-deliver", action="store_true",
                    help="after the OWNER approves an intent, execute it automatically")
    ap.add_argument("--once", action="store_true", help="single pass (for testing)")
    a = ap.parse_args()
    if not a.relay_url or not a.key_file:
        sys.exit("need --relay-url/RELAY_URL and --key-file/RELAY_PULLER_KEY_FILE")
    if not a.relay_url.startswith("https://") and not a.relay_url.startswith("http://127.0.0.1"):
        sys.exit("refusing a non-HTTPS relay URL (the puller key would travel in clear text)")
    key = open(a.key_file).read().strip()
    brain_cmd = shlex.split(os.environ.get("BRAIND_BRAIN_CMD", "")) or \
        [sys.executable, os.path.join(HERE, "..", "bin", "brain.py")]
    relay = Relay(a.relay_url, key)
    log(f"puller started; relay={a.relay_url} auto_deliver={a.auto_deliver}")
    while True:
        try:
            # drain claims first, then reconcile active intents
            while True:
                p = relay.call("POST", "/v1/puller/claim").get("proposal")
                if not p:
                    break
                handle_claim(relay, brain_cmd, p, a.caller)
            for item in relay.call("GET", "/v1/puller/active").get("proposals", []):
                handle_active(relay, brain_cmd, item, a.auto_deliver)
        except Exception as e:  # network blips must not kill the loop
            log(f"loop error: {type(e).__name__}: {e}")
        if a.once:
            break
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
