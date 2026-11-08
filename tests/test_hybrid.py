#!/usr/bin/env python3
"""Uji KOMBINASI build hybrid BrainD (v2-2 + simulasi Fase 3 + sakelar
pairing + sakelar policy) terhadap instance uji terisolasi.

Jalankan dengan venv wallet:  ~/workspace/.wallet-venv/bin/python tests/test_hybrid.py

Kelas:
  TestApprovalSimulation — BRAIND_APPROVAL_KEY_FILE aktif:
    sig hilang / sig palsu -> ditolak gerbang, TANPA record simulasi;
    sig valid + tx revert  -> -32006; sig valid + tx valid -> simulasi lolos.
  TestPairingSwitch — BRAIND_PAIRING_ENABLED:
    default OFF -> pairBegin/pairPoll -32007; "1" -> alur pairing penuh;
    OFF lagi -> pembuatan ditolak, sesi lama tetap berlaku.
  TestPolicySwitch — BRAIND_POLICY_ENABLED:
    OFF -> signing tanpa intent jalan (simulasi TETAP aktif: tx revert
    tetap -32006; tujuan BLOCKED tetap ditolak); ON -> tanpa intent
    ditolak "brain approval required".

Semua daemon uji: seed/token/state sekali pakai di folder temp di dalam
braind-hybrid, port WS 28923 / HTTP 28924. Akun uji = akun dari
test-seed.txt hybrid (saldo Sepolia 0 — pola uji Fase 3: broadcast akan
gagal dana, dan itu BUKAN kegagalan gerbang/simulasi).
"""
import asyncio, json, os, secrets, shutil, socket, subprocess, sys, tempfile, time, unittest, urllib.request

HYBRID = "/home/hatch/workspace/braind-hybrid"
PY = sys.executable
SELF = "0x5fFc81cb10d1C5354ecD48Be3a46576f07cd7e99"
WS_URL = "ws://127.0.0.1:28923"
HTTP_URL = "http://127.0.0.1:28924/"
ONE_ETH_HEX = "0xde0b6b3a7640000"
BLOCKED_ADDR = "0x00000000000000000000000000000000deadbeef"


def make_fixture():
    d = tempfile.mkdtemp(prefix="hybrid-test-", dir=HYBRID)
    fx = {"dir": d, "state": os.path.join(d, "state"),
          "seed": os.path.join(d, "seed.txt"), "token": os.path.join(d, "token.txt"),
          "log": os.path.join(d, "walletd.log")}
    os.makedirs(fx["state"])
    shutil.copy(os.path.join(HYBRID, "test-seed.txt"), fx["seed"])
    shutil.copy(os.path.join(HYBRID, "test-token.txt"), fx["token"])
    shutil.copy(os.path.join(HYBRID, "test-state", "brain_policy.json"),
                os.path.join(fx["state"], "brain_policy.json"))
    fx["master_token"] = open(fx["token"]).read().strip()
    return fx


def port_open(port):
    s = socket.socket()
    s.settimeout(0.4)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def wait_port(port, want, timeout=45):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if port_open(port) == want:
            return True
        time.sleep(0.25)
    return False


class Daemon:
    def __init__(self, fx, extra_env=None, blocked=False):
        env = dict(os.environ)
        env.update(BRAIND_SEED_FILE=fx["seed"], BRAIND_TOKEN_FILE=fx["token"],
                   BRAIND_STATE_DIR=fx["state"], BRAIND_WS_PORT="28923",
                   BRAIND_HTTP_PORT="28924", BRAIND_LOG_FILE=fx["log"])
        env.pop("BRAIND_APPROVAL_KEY_FILE", None)
        env.pop("BRAIND_PAIRING_ENABLED", None)
        env.pop("BRAIND_POLICY_ENABLED", None)
        if extra_env:
            env.update(extra_env)
        if blocked:
            code = ("import sys; sys.path.insert(0, %r); import walletd; "
                    "walletd.BLOCKED.add(%r); "
                    "import asyncio; asyncio.run(walletd.main())"
                    % (os.path.join(HYBRID, "daemon"), BLOCKED_ADDR))
            cmd = [PY, "-c", code]
        else:
            cmd = [PY, os.path.join(HYBRID, "daemon", "walletd.py")]
        self.out = open(os.path.join(fx["dir"], "daemon-stdout.log"), "ab")
        self.p = subprocess.Popen(cmd, cwd=HYBRID, env=env,
                                  stdout=self.out, stderr=subprocess.STDOUT)
        # HTTP thread naik lebih dulu; listener WS menyusul sesaat sesudahnya.
        # Tunggu KEDUA port agar tidak balapan (connect-refused palsu).
        if not (wait_port(28924, True) and wait_port(28923, True)):
            raise RuntimeError("daemon uji tidak naik; lihat " + self.out.name)

    def stop(self):
        self.p.terminate()
        try:
            self.p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait(timeout=10)
        self.out.close()
        wait_port(28924, False)


def brain_env(fx, extra=None):
    env = dict(os.environ)
    env.pop("BRAIND_APPROVAL_KEY_FILE", None)
    env.update(BRAIND_STATE_DIR=fx["state"], BRAIND_TOKEN_FILE=fx["token"],
               BRAIND_WALLETD_URL=HTTP_URL)
    if extra:
        env.update(extra)
    return env


def brain(fx, *args, extra_env=None):
    r = subprocess.run([PY, os.path.join(HYBRID, "bin", "brain.py"), *args],
                       env=brain_env(fx, extra_env), capture_output=True,
                       text=True, timeout=180)
    out = (r.stdout or "").strip()
    try:
        data = json.loads(out) if out else {}
    except Exception:
        data = {"_raw": out}
    return data, r


def propose_approve(fx, value_ether, key_env):
    p, r = brain(fx, "propose", "--caller", "uji-hybrid", "--chain", "sepolia",
                 "--method", "eth_sendTransaction", "--to", SELF,
                 "--value", value_ether, "--data", "0x",
                 "--reason", "uji hybrid kombinasi", extra_env=key_env)
    assert p.get("ok"), f"propose gagal: {p} / {r.stderr}"
    a, r2 = brain(fx, "approve", "--id", p["intent_id"], "--hash", p["intent_hash"],
                  "--by", "owner-uji", extra_env=key_env)
    assert a.get("ok"), f"approve gagal: {a} / {r2.stderr}"
    return p["intent_id"]


async def _ws_once(method, params, chain="0xaa36a7"):
    import websockets
    async with websockets.connect(WS_URL, max_size=2**22) as ws:
        await ws.send(json.dumps({"id": 1, "method": method,
                                  "params": params, "chain": chain}))
        raw = await asyncio.wait_for(ws.recv(), timeout=150)
        return json.loads(raw)


def ws_call(method, params):
    return asyncio.run(_ws_once(method, params))


def http_rpc(method, params=None, token=None):
    body = {"id": 1, "method": method, "params": params if params is not None else {}}
    req = urllib.request.Request(HTTP_URL, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("X-Walletd-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}")


def read_jsonl(state, name):
    path = os.path.join(state, name)
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in open(path) if l.strip()]


def sims_for(state, intent_id):
    return [s for s in read_jsonl(state, "simulations.jsonl")
            if s.get("intent_id") == intent_id]


def consumed_for(state, intent_id):
    return [c for c in read_jsonl(state, "ws_consumed.jsonl")
            if c.get("intent_id") == intent_id]


class TestApprovalSimulation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = make_fixture()
        cls.key_file = os.path.join(cls.fx["dir"], "approval.key")
        open(cls.key_file, "w").write(secrets.token_hex(32))
        cls.key_env = {"BRAIND_APPROVAL_KEY_FILE": cls.key_file}
        cls.daemon = Daemon(cls.fx, {"BRAIND_APPROVAL_KEY_FILE": cls.key_file})

    @classmethod
    def tearDownClass(cls):
        cls.daemon.stop()

    def test_1_missing_sig_rejected_before_simulation(self):
        iid = propose_approve(self.fx, "1", None)  # approve TANPA kunci -> tanpa sig
        resp = ws_call("eth_sendTransaction",
                       [{"to": SELF, "value": ONE_ETH_HEX, "data": "0x"}])
        err = resp.get("error") or {}
        self.assertIn("brain approval required", str(err.get("message", "")))
        self.assertNotEqual(err.get("code"), -32006)
        self.assertEqual(sims_for(self.fx["state"], iid), [])
        self.assertEqual(consumed_for(self.fx["state"], iid), [])

    def test_2_forged_sig_rejected_before_simulation(self):
        iid = propose_approve(self.fx, "0", self.key_env)
        rec, _ = brain(self.fx, "show", "--id", iid, extra_env=self.key_env)
        forged = dict(rec)
        forged["approval_sig"] = "0" * 64
        with open(os.path.join(self.fx["state"], "intents.jsonl"), "a") as f:
            f.write(json.dumps(forged) + "\n")
        resp = ws_call("eth_sendTransaction",
                       [{"to": SELF, "value": "0x0", "data": "0x"}])
        err = resp.get("error") or {}
        self.assertIn("brain approval required", str(err.get("message", "")))
        self.assertEqual(sims_for(self.fx["state"], iid), [])
        self.assertEqual(consumed_for(self.fx["state"], iid), [])

    def test_3_valid_sig_reverting_tx_rejected_32006(self):
        iid = propose_approve(self.fx, "1", self.key_env)
        resp = ws_call("eth_sendTransaction",
                       [{"to": SELF, "value": ONE_ETH_HEX, "data": "0x"}])
        err = resp.get("error") or {}
        self.assertEqual(err.get("code"), -32006)
        sims = sims_for(self.fx["state"], iid)
        self.assertTrue(any(s["simulation"]["ok"] is False for s in sims))
        self.assertEqual(consumed_for(self.fx["state"], iid), [])

    def test_4_valid_sig_valid_tx_passes_simulation(self):
        iid = propose_approve(self.fx, "0", self.key_env)
        resp = ws_call("eth_sendTransaction",
                       [{"to": SELF, "value": "0x0", "data": "0x"}])
        err = resp.get("error") or {}
        self.assertNotEqual(err.get("code"), -32006)
        self.assertNotIn("brain approval required", str(err.get("message", "")))
        sims = sims_for(self.fx["state"], iid)
        self.assertTrue(any(s["simulation"]["ok"] is True for s in sims))
        self.assertEqual(len(consumed_for(self.fx["state"], iid)), 1)


class TestPairingSwitch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = make_fixture()
        cls.session_token = None

    def sessions_file(self):
        p = os.path.join(self.fx["state"], "sessions.json")
        return json.load(open(p)) if os.path.exists(p) else {"sessions": [], "pairings": {}}

    def test_1_off_by_default(self):
        d = Daemon(self.fx)
        try:
            r = http_rpc("braind_pairBegin", {"label": "uji"})
            self.assertEqual((r.get("error") or {}).get("code"), -32007)
            self.assertIn("pairing disabled", str((r.get("error") or {}).get("message", "")))
            r2 = http_rpc("braind_pairPoll", {"pairing_id": "brd_x", "secret": "y"})
            self.assertEqual((r2.get("error") or {}).get("code"), -32007)
            data = self.sessions_file()
            self.assertEqual(data.get("pairings"), {})
            self.assertEqual(data.get("sessions"), [])
        finally:
            d.stop()

    def test_2_on_full_flow(self):
        d = Daemon(self.fx, {"BRAIND_PAIRING_ENABLED": "1"})
        try:
            r = http_rpc("braind_pairBegin", {"label": "uji-hybrid"})
            res = r.get("result") or {}
            self.assertTrue(res.get("pairing_id") and res.get("secret"))
            pid, secret = res["pairing_id"], res["secret"]
            poll = http_rpc("braind_pairPoll", {"pairing_id": pid, "secret": secret})
            self.assertEqual((poll.get("result") or {}).get("status"), "pending")
            ap = http_rpc("braind_pairApprove", {"pairing_id": pid},
                          token=self.fx["master_token"])
            self.assertTrue((ap.get("result") or {}).get("session_id"),
                            f"pairApprove gagal: {ap}")
            poll2 = http_rpc("braind_pairPoll", {"pairing_id": pid, "secret": secret})
            res2 = poll2.get("result") or {}
            self.assertEqual(res2.get("status"), "approved")
            self.assertTrue(str(res2.get("session_token", "")).startswith("braind_sess_"))
            type(self).session_token = res2["session_token"]
            self.assertEqual(len(self.sessions_file().get("sessions", [])), 1)
        finally:
            d.stop()

    def test_3_off_again_session_survives(self):
        self.assertIsNotNone(self.session_token)
        d = Daemon(self.fx)
        try:
            r = http_rpc("braind_pairBegin", {"label": "uji"})
            self.assertEqual((r.get("error") or {}).get("code"), -32007)
            # sesi lama tetap berlaku: read method dengan token sesi
            ok = http_rpc("eth_chainId", [], token=self.session_token)
            self.assertIsNone(ok.get("error"))
            self.assertTrue(str(ok.get("result", "")).startswith("0x"))
            # token master juga tidak terpengaruh
            ok2 = http_rpc("eth_chainId", [], token=self.fx["master_token"])
            self.assertIsNone(ok2.get("error"))
        finally:
            d.stop()


class TestPolicySwitch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = make_fixture()

    def test_1_off_signing_without_intent(self):
        d = Daemon(self.fx, {"BRAIND_POLICY_ENABLED": "0"})
        try:
            resp = ws_call("eth_sendTransaction",
                           [{"to": SELF, "value": "0x0", "data": "0x"}])
            err = resp.get("error") or {}
            # mencapai tahap broadcast (gagal dana = bukti lolos gerbang+simulasi)
            self.assertNotIn("brain approval required", str(err.get("message", "")))
            self.assertNotEqual(err.get("code"), -32006)
            sims = [s for s in read_jsonl(self.fx["state"], "simulations.jsonl")
                    if s.get("intent_id") is None]
            self.assertTrue(any(s["simulation"]["ok"] is True for s in sims),
                            "simulasi harus tetap jalan saat policy OFF")
            self.assertEqual(read_jsonl(self.fx["state"], "ws_consumed.jsonl"), [])
            sig = ws_call("personal_sign", ["uji policy off", SELF])
            self.assertEqual(len(str(sig.get("result") or "")), 130)
        finally:
            d.stop()

    def test_2_on_same_requests_rejected(self):
        d = Daemon(self.fx)
        try:
            resp = ws_call("eth_sendTransaction",
                           [{"to": SELF, "value": "0x0", "data": "0x"}])
            self.assertIn("brain approval required",
                          str((resp.get("error") or {}).get("message", "")))
            sig = ws_call("personal_sign", ["uji policy on", SELF])
            self.assertIn("brain approval required",
                          str((sig.get("error") or {}).get("message", "")))
            self.assertIsNone(sig.get("result"))
        finally:
            d.stop()

    def test_3_off_blocked_destination_still_rejected(self):
        d = Daemon(self.fx, {"BRAIND_POLICY_ENABLED": "0"}, blocked=True)
        try:
            resp = ws_call("eth_sendTransaction",
                           [{"to": BLOCKED_ADDR, "value": "0x0", "data": "0x"}])
            self.assertIn("denylisted",
                          str((resp.get("error") or {}).get("message", "")))
        finally:
            d.stop()

    def test_4_off_reverting_tx_still_32006(self):
        d = Daemon(self.fx, {"BRAIND_POLICY_ENABLED": "0"})
        try:
            resp = ws_call("eth_sendTransaction",
                           [{"to": SELF, "value": ONE_ETH_HEX, "data": "0x"}])
            self.assertEqual((resp.get("error") or {}).get("code"), -32006)
        finally:
            d.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
