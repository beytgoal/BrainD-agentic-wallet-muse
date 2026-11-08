"""End-to-end tests for relay + puller (stdlib only, fake brain.py).
Run: python3 tests/test_relay.py"""
import json, os, sys, tempfile, threading, time, unittest, urllib.error, urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "relay"))
import relay_server as rs, puller as pl

AK, PK = "a" * 40, "p" * 40
GOOD = {"chain": "sepolia", "to": "0x" + "22" * 20, "value": "0.001", "reason": "pay invoice 42"}
TMP = tempfile.mkdtemp()
FAKE = os.path.join(TMP, "fakebrain.py")
STATE = os.path.join(TMP, "intents.json")
open(FAKE, "w").write('''
import json, os, sys
S = os.environ["FAKE_STATE"]
db = json.load(open(S)) if os.path.exists(S) else {}
cmd, args = sys.argv[1], dict(a.split("=", 1) for a in sys.argv[2:] if a.startswith("--") and "=" in a)
if cmd == "propose":
    if args["--to"].lower() == "0x" + "ee" * 20:
        print(json.dumps({"ok": False, "error": "policy_rejected: destination not in allowlist"}), file=sys.stderr); sys.exit(1)
    iid = "iid%d" % (len(db) + 1)
    db[iid] = {"intent_id": iid, "intent_hash": "h" * 64, "status": "queued", "args": args}
    json.dump(db, open(S, "w")); print(json.dumps({"ok": True, "intent_id": iid, "intent_hash": "h" * 64}))
elif cmd == "show":
    print(json.dumps(db[args["--id"]]))
elif cmd == "deliver":
    r = db[args["--id"]]
    if r["status"] != "approved":
        print(json.dumps({"ok": False, "error": "not approved"}), file=sys.stderr); sys.exit(1)
    r["status"] = "executed"; r["tx_hash"] = "0x" + "ab" * 32; json.dump(db, open(S, "w"))
    print(json.dumps({"ok": True, "status": "executed", "tx_hash": r["tx_hash"]}))
''')
os.environ["FAKE_STATE"] = STATE
BRAIN = [sys.executable, FAKE]

def owner_sets(iid, status):
    db = json.load(open(STATE)); db[iid]["status"] = status; json.dump(db, open(STATE, "w"))

class Base(unittest.TestCase):
    ttl = 3600
    def setUp(self):
        if os.path.exists(STATE): os.remove(STATE)
        env = dict(RELAY_AGENT_KEY=AK, RELAY_PULLER_KEY=PK, RELAY_DB=os.path.join(TMP, "r%d.db" % time.time_ns()),
                   RELAY_CHAINS="sepolia", RELAY_RATE_PER_MIN="100", RELAY_MAX_PENDING="50",
                   RELAY_TTL_SECONDS=str(self.ttl))
        self.cfg = rs.Config(env); self.store = rs.Store(self.cfg.db)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), rs.make_handler(self.cfg, self.store))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.relay = pl.Relay(self.url, PK)
    def tearDown(self): self.srv.shutdown()
    def req(self, method, path, body=None, key=AK):
        r = urllib.request.Request(self.url + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                   headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"} if key else {})
        try:
            with urllib.request.urlopen(r) as f: return f.status, json.loads(f.read())
        except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}")

class TestRelay(Base):
    def test_auth_roles(self):
        self.assertEqual(self.req("GET", "/v1/proposals", key=None)[0], 401)
        self.assertEqual(self.req("GET", "/v1/proposals", key="x" * 40)[0], 401)
        self.assertEqual(self.req("GET", "/v1/proposals", key=PK)[0], 403)          # puller key can't act as agent
        self.assertEqual(self.req("POST", "/v1/puller/claim", key=AK)[0], 403)      # agent key can't act as puller
        self.assertEqual(self.req("GET", "/healthz", key=None)[0], 200)
        self.assertEqual(self.req("GET", "/openapi.json", key=None)[0], 200)
    def test_validation(self):
        bad = [dict(GOOD, chain="base"), dict(GOOD, chain="../x"), dict(GOOD, to="0x123"), dict(GOOD, value="5"),
               dict(GOOD, value="1e3"), dict(GOOD, value="-1"), dict(GOOD, method="personal_sign"),
               dict(GOOD, reason="x"), dict(GOOD, reason="a\nb\x00c"), dict(GOOD, extra=1),
               dict(GOOD, data="0xzz"), dict(GOOD, data="0x" + "00" * 5000), dict(GOOD, to=None), []]
        for b in bad:
            self.assertEqual(self.req("POST", "/v1/proposals", b)[0], 400, b)
        self.assertEqual(self.req("POST", "/v1/proposals", GOOD)[0], 201)
        self.assertEqual(self.req("POST", "/v1/proposals", dict(GOOD, value=0.001))[0], 201)
    def test_rate_and_pending_limits(self):
        self.cfg.rate = 2
        for _ in range(2): self.assertEqual(self.req("POST", "/v1/proposals", GOOD)[0], 201)
        self.assertEqual(self.req("POST", "/v1/proposals", GOOD)[0], 429)
    def test_agent_cannot_report_status(self):
        pid = self.req("POST", "/v1/proposals", GOOD)[1]["id"]
        self.assertEqual(self.req("POST", f"/v1/puller/proposals/{pid}/status", {"status": "approved"}, key=AK)[0], 403)
        self.relay.call("POST", "/v1/puller/claim")
        # illegal transition claimed -> executed
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.relay.call("POST", f"/v1/puller/proposals/{pid}/status", {"status": "executed"})
        self.assertEqual(cm.exception.code, 409)

class TestFlow(Base):
    def test_full_flow_with_owner_approval_and_autodeliver(self):
        pid = self.req("POST", "/v1/proposals", GOOD)[1]["id"]
        pl.handle_claim(self.relay, BRAIN, self.relay.call("POST", "/v1/puller/claim")["proposal"], "muse-relay")
        s = self.req("GET", f"/v1/proposals/{pid}")[1]
        self.assertEqual((s["status"], s["intent_hash"][:4]), ("queued", "hhhh"))
        # safe: reason passed with the relay id, value/address pass through unchanged
        a = json.load(open(STATE))["iid1"]["args"]
        self.assertEqual((a["--to"], a["--value"], a["--chain"]), (GOOD["to"], "0.001", "sepolia"))
        # nothing executes before the owner approves, even with auto-deliver on
        for item in self.relay.call("GET", "/v1/puller/active")["proposals"]:
            pl.handle_active(self.relay, BRAIN, item, True)
        self.assertEqual(self.req("GET", f"/v1/proposals/{pid}")[1]["status"], "queued")
        owner_sets("iid1", "approved")
        for item in self.relay.call("GET", "/v1/puller/active")["proposals"]:
            pl.handle_active(self.relay, BRAIN, item, True)
        s = self.req("GET", f"/v1/proposals/{pid}")[1]
        self.assertEqual((s["status"], s["tx_hash"]), ("executed", "0x" + "ab" * 32))
    def test_approved_without_autodeliver_stays_approved(self):
        pid = self.req("POST", "/v1/proposals", GOOD)[1]["id"]
        pl.handle_claim(self.relay, BRAIN, self.relay.call("POST", "/v1/puller/claim")["proposal"], "c")
        owner_sets("iid1", "approved")
        for item in self.relay.call("GET", "/v1/puller/active")["proposals"]:
            pl.handle_active(self.relay, BRAIN, item, False)
        self.assertEqual(self.req("GET", f"/v1/proposals/{pid}")[1]["status"], "approved")
    def test_brain_policy_rejection_and_owner_rejection(self):
        p1 = self.req("POST", "/v1/proposals", dict(GOOD, to="0x" + "ee" * 20))[1]["id"]
        pl.handle_claim(self.relay, BRAIN, self.relay.call("POST", "/v1/puller/claim")["proposal"], "c")
        s = self.req("GET", f"/v1/proposals/{p1}")[1]
        self.assertEqual(s["status"], "rejected"); self.assertIn("allowlist", s["note"])
        p2 = self.req("POST", "/v1/proposals", GOOD)[1]["id"]
        pl.handle_claim(self.relay, BRAIN, self.relay.call("POST", "/v1/puller/claim")["proposal"], "c")
        owner_sets("iid1", "rejected")
        for item in self.relay.call("GET", "/v1/puller/active")["proposals"]:
            pl.handle_active(self.relay, BRAIN, item, True)
        self.assertEqual(self.req("GET", f"/v1/proposals/{p2}")[1]["status"], "rejected")
    def test_puller_revalidates_locally(self):
        evil = {"id": "prop_" + "0" * 16, "method": "eth_sendTransaction", "chain": "sepolia",
                "to": "0x1; rm -rf /", "value": "1", "data": "0x", "reason": "hello there"}
        self.assertFalse(pl.valid_local(evil))
        self.assertFalse(pl.valid_local(dict(evil, to=GOOD["to"], method="personal_sign")))
        self.assertTrue(pl.valid_local(dict(evil, to=GOOD["to"])))

class TestExpiry(Base):
    ttl = 1
    def test_stale_proposals_expire_and_are_not_claimed(self):
        pid = self.req("POST", "/v1/proposals", GOOD)[1]["id"]
        time.sleep(1.2)
        self.assertEqual(self.req("GET", f"/v1/proposals/{pid}")[1]["status"], "expired")
        self.assertIsNone(self.relay.call("POST", "/v1/puller/claim")["proposal"])

class TestConfig(unittest.TestCase):
    def test_weak_or_equal_keys_refused(self):
        for env in ({}, {"RELAY_AGENT_KEY": "a" * 40}, {"RELAY_AGENT_KEY": "a" * 40, "RELAY_PULLER_KEY": "a" * 40},
                    {"RELAY_AGENT_KEY": "short", "RELAY_PULLER_KEY": "p" * 40}):
            with self.assertRaises(SystemExit): rs.Config(env)

if __name__ == "__main__":
    unittest.main(verbosity=1)
