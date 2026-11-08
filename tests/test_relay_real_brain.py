"""Relay puller against the REAL bin/brain.py (policy gate), no walletd needed.
Run: python3 tests/test_relay_real_brain.py"""
import json, os, subprocess, sys, tempfile, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "relay"))
import puller as pl

SELF = "0x" + "11" * 20
ST = tempfile.mkdtemp()
os.environ.update(BRAIND_STATE_DIR=ST, BRAIND_SELF_ADDRESS=SELF, BRAIND_TOKEN_FILE=os.path.join(ST, "tok"))
BRAIN = [sys.executable, os.path.join(ROOT, "bin", "brain.py")]
def P(**kw):
    d = {"id": "prop_" + "0" * 16, "method": "eth_sendTransaction", "chain": "sepolia", "to": SELF,
         "value": "0.001", "data": "0x", "reason": "unit test payment"}; d.update(kw); return d

class T(unittest.TestCase):
    def run_claim(self, p):
        calls = []
        class R:
            def call(self, m, path, body=None): calls.append(body); return {}
        pl.handle_claim(R(), BRAIN, p, "muse-relay"); return calls[-1]
    def test_allowed_goes_to_queue_with_real_hash(self):
        r = self.run_claim(P())
        self.assertEqual(r["status"], "queued"); self.assertEqual(len(r["intent_hash"]), 64)
        ok, rec, _ = pl.run_brain(BRAIN, ["show", "--id=" + r["intent_id"]])
        self.assertEqual((rec["status"], rec["to"], rec["caller"]), ("queued", SELF, "muse-relay"))
        self.assertIn("[relay prop_", rec["reason"])
    def test_not_allowlisted_destination_rejected_by_brain(self):
        r = self.run_claim(P(to="0x" + "ee" * 20)); self.assertEqual(r["status"], "rejected")
        self.assertIn("allowlist", r["note"])
    def test_mainnet_default_cap_enforced_by_brain(self):
        r = self.run_claim(P(chain="base", value="0.05")); self.assertEqual(r["status"], "rejected")
        self.assertIn("cap", r["note"])
        self.assertEqual(self.run_claim(P(chain="base", value="0.01"))["status"], "queued")
    def test_nothing_is_approved_or_delivered_by_puller(self):
        r = self.run_claim(P(value="0.002"))
        ok, rec, _ = pl.run_brain(BRAIN, ["show", "--id=" + r["intent_id"]])
        self.assertEqual(rec["status"], "queued")   # only the owner can move it to approved
if __name__ == "__main__": unittest.main(verbosity=1)
