"""Tests for the security patches. Stdlib only: heavy deps are stubbed.
Run: python3 tests/test_security_patches.py"""
import hashlib, hmac, importlib, json, os, sys, tempfile, types, unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
STATE = os.path.join(TMP, "state"); os.makedirs(STATE)
KEY = os.path.join(TMP, "approval.key")
open(KEY, "w").write("k" * 64)
os.environ.update(BRAIND_STATE_DIR=STATE, BRAIND_LOG_FILE=os.path.join(TMP, "nodir", "x", "w.log"),
                  BRAIND_APPROVAL_KEY_FILE=KEY, BRAIND_TOKEN_FILE=os.path.join(TMP, "tok"),
                  BRAIND_SEED_FILE=os.path.join(TMP, "seed"))
for m in ("websockets", "eth_account", "eth_account.messages", "bip_utils", "solders",
          "solders.keypair", "fast_rpc"):
    sys.modules[m] = mock.MagicMock()
sys.path.insert(0, os.path.join(ROOT, "daemon")); sys.path.insert(0, os.path.join(ROOT, "bin"))
walletd = importlib.import_module("walletd")
brain = importlib.import_module("brain")

POLICY = {"chains": {"base": {"chain_id": "0x2105", "class": "mainnet", "cap_wei": 10**16},
                     "sepolia": {"chain_id": "0xaa36a7", "class": "testnet", "cap_wei": None}},
          "destinations": {"self": "0x" + "11" * 20, "allowlist": ["0x" + "22" * 20]}}
json.dump(POLICY, open(os.path.join(STATE, "brain_policy.json"), "w"))
SELF, ALLOWED, EVIL = "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "ee" * 20

class WS:
    def __init__(self, origin=None):
        self.request = types.SimpleNamespace(headers={"Origin": origin} if origin else {})

class T(unittest.TestCase):
    def test_log_creates_dir_and_never_crashes(self):
        walletd.log("hello"); self.assertTrue(os.path.exists(walletd.LOG))
        with mock.patch.object(walletd, "LOG", "/proc/nope/x.log"):
            walletd.log("still ok")  # must not raise

    def test_ws_origin(self):
        self.assertTrue(walletd._ws_origin_ok(WS()))
        self.assertTrue(walletd._ws_origin_ok(WS("chrome-extension://abc")))
        self.assertFalse(walletd._ws_origin_ok(WS("https://evil.example")))
        with mock.patch.dict(os.environ, {"BRAIND_ALLOWED_ORIGINS": "chrome-extension://mine"}):
            self.assertTrue(walletd._ws_origin_ok(WS("chrome-extension://mine")))
            self.assertFalse(walletd._ws_origin_ok(WS("chrome-extension://other")))

    def test_cap_and_allowlist(self):
        L = walletd.policy_limits_ok
        self.assertTrue(L({"to": ALLOWED, "value": hex(10**16)}, "0x2105", True)[0])
        self.assertFalse(L({"to": ALLOWED, "value": hex(10**16 + 1)}, "0x2105", True)[0])
        self.assertFalse(L({"to": EVIL, "value": "0x0"}, "0x2105", True)[0])
        self.assertTrue(L({"to": EVIL, "value": "0x0"}, "0x2105", False)[0])   # allowlist only for sessions
        self.assertFalse(L({"value": "0x0", "data": "0x6080"}, "0x2105", True)[0])  # CREATE on mainnet
        self.assertTrue(L({"value": "0x0", "data": "0x6080"}, "0xaa36a7", True)[0])
        self.assertFalse(L({"to": SELF, "value": "garbage"}, "0x2105", True)[0])    # fail closed

    def test_session_scope(self):
        sess = {"methods": ["eth_sendTransaction"], "chains": ["*"], "max_value_wei": "1000"}
        def msg(to, val): return {"method": "eth_sendTransaction", "chain": "0x2105",
                                  "params": [{"to": to, "value": hex(val)}]}
        self.assertTrue(walletd._session_scope_ok(sess, "eth_sendTransaction", msg(ALLOWED, 1000))[0])
        self.assertFalse(walletd._session_scope_ok(sess, "eth_sendTransaction", msg(ALLOWED, 1001))[0])
        self.assertFalse(walletd._session_scope_ok(sess, "eth_sendTransaction", msg(EVIL, 1))[0])
        self.assertFalse(walletd._session_scope_ok(sess, "personal_sign", {"method": "personal_sign"})[0])

    def test_approval_sig_roundtrip_and_forgery(self):
        rec = {"intent_id": "i1", "intent_hash": "h1", "status": "approved"}
        self.assertFalse(walletd._approval_sig_ok(rec))                 # forged: no sig
        rec["approval_sig"] = brain.approval_sig(rec)
        self.assertTrue(walletd._approval_sig_ok(rec))                  # brain sig == walletd check
        self.assertTrue(brain.approval_sig_ok(rec))
        self.assertFalse(walletd._approval_sig_ok(dict(rec, intent_hash="h2")))  # bound to hash
        with mock.patch.object(walletd, "APPROVAL_KEY_FILE", "/nonexistent"):
            self.assertFalse(walletd._approval_sig_ok(rec))             # unreadable key -> fail closed
        with mock.patch.object(walletd, "APPROVAL_KEY_FILE", None):
            self.assertTrue(walletd._approval_sig_ok({"intent_id": "x"}))  # feature off = legacy

    def test_default_policy_mainnet_caps(self):
        for n in ("bsc", "ethereum", "base", "polygon"):
            self.assertEqual(brain.DEFAULT_POLICY["chains"][n]["cap_wei"], 10**16)
        with open(os.path.join(ROOT, "config", "brain_policy.example.json")) as fh:
            ex = json.load(fh)
        for n, i in ex["chains"].items():
            if i["class"] == "mainnet": self.assertEqual(i["cap_wei"], 10**16)

if __name__ == "__main__":
    unittest.main(verbosity=2)
