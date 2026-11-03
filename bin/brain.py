#!/usr/bin/env python3
"""brain — Lapis "Brain" di atas walletd: dompet agent yang DIMEDIASI.

Modul TERPISAH dari walletd. File ini TIDAK mengubah walletd.py dan TIDAK
pernah membaca seed / menandatangani sendiri. Semua eksekusi diteruskan
sebagai JSON EIP-1193 ke daemon walletd lewat HTTP bridge localhost
(127.0.0.1:18924, POST) — persis pola bin/wd.py. Sejak revisi owner
2 Nov 2026 22:18 WIB tidak ada cap nilai di lapis mana pun (Brain maupun
walletd); gerbang Brain di file ini adalah gerbang persetujuannya.

Alur (arsitektur Brain):
  propose -> gerbang kebijakan (brain_policy.json) -> intent `queued`
  approve -> persetujuan exact-match (intent_id + intent_hash, satu intent)
  deliver -> hanya intent `approved`; panggil walletd SATU kali (tanpa
             retry otomatis), lalu verifikasi receipt on-chain.

State (append-only, tanpa kredensial/seed/token apa pun):
  state/brain_policy.json  gerbang kebijakan (chain, method, cap, tujuan)
  state/intents.jsonl      satu baris JSON per transisi status intent
  state/audit.jsonl        satu baris JSON per peristiwa audit

Jalankan dengan venv wallet:
    ~/workspace/.wallet-venv/bin/python bin/brain.py <cmd> ...

Perintah:
  check                        status walletd + chainId & saldo burner
                               (sepolia & base-sepolia)
  propose --caller N --chain C --method eth_sendTransaction --to A
          --value <ether desimal> [--data 0x] --reason "<alasan>"
  propose --caller N --chain C --method personal_sign
          --message "<teks>" --reason "<alasan>"
  propose --caller N --chain C --method eth_signTypedData_v4
          --typed-data <json literal|@file.json> --reason "<alasan>"
  list                         status terakhir semua intent
  show --id <intent_id>        status terakhir satu intent
  approve --id <intent_id> --hash <intent_hash> --by <actor>
  reject  --id <intent_id> --by <actor> --note <teks>
  deliver --id <intent_id>     eksekusi intent approved via walletd
"""
import argparse, hashlib, json, os, sys, time, urllib.request, urllib.error, uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

WALLETD_HTTP = "http://127.0.0.1:18924/"
WALLETD_TOKEN_FILE = os.path.expanduser("~/.burner/walletd.token")
BURNER_EVM = "0x0000000000000000000000000000000000000000"

def walletd_headers():
    """Header HTTP walletd termasuk token pemanggil (2026-10-30).
    Token TIDAK pernah ditampilkan/dicatat; file hilang = error jelas."""
    try:
        with open(WALLETD_TOKEN_FILE) as f:
            tok = f.read().strip()
    except Exception:
        tok = ""
    if len(tok) < 32:
        raise RuntimeError(
            "token pemanggil walletd tidak ditemukan/invalid di %s" % WALLETD_TOKEN_FILE)
    return {"Content-Type": "application/json", "X-Walletd-Token": tok}
WIB = timezone(timedelta(hours=7))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(os.path.dirname(BASE_DIR), "state")
POLICY_PATH = os.path.join(STATE_DIR, "brain_policy.json")
INTENTS_PATH = os.path.join(STATE_DIR, "intents.jsonl")
AUDIT_PATH = os.path.join(STATE_DIR, "audit.jsonl")

# Peta chain walletd (sama seperti wd.py): nama -> chainId hex.
CHAINS = {
    "bsc":          "0x38",
    "ethereum":     "0x1",
    "base":         "0x2105",
    "polygon":      "0x89",
    "elysium":      "0x185d9",
    "sepolia":      "0xaa36a7",
    "bsc-testnet":  "0x61",
    "base-sepolia": "0x14a34",
    "amoy":         "0x13882",
}

# SEMUA CAP NILAI DICABUT (revisi owner 2 Nov 2026 22:18 WIB): tidak ada
# konstanta cap di kode — cap_wei di brain_policy.json bernilai null untuk
# semua chain dan gerbang melewatkan nilai berapa pun. Jangan tambahkan
# kembali cap di sini; bila suatu hari policy mengisi cap_wei lagi, gerbang
# propose menghormatinya dari file policy.
SIGN_METHODS = ("personal_sign", "eth_signTypedData_v4")

DEFAULT_POLICY = {
    "version": 3,
    "description": (
        "Gerbang kebijakan Brain v3 (revisi owner 2 Nov 2026 22:18 WIB): "
        "SEMUA CAP NILAI DICABUT (cap_wei null di semua chain) dan method "
        "tanda tangan pesan personal_sign + eth_signTypedData_v4 AKTIF "
        "(v2, keputusan owner 22:14 WIB). Cap walletd juga dicabut di "
        "revisi yang sama; yang tersisa adalah gerbang persetujuan intent "
        "dan allowlist tujuan."
    ),
    "allowed_methods": ["eth_sendTransaction", "personal_sign",
                        "eth_signTypedData_v4"],
    "chains": {
        "sepolia":      {"chain_id": CHAINS["sepolia"],      "class": "testnet", "cap_wei": None},
        "base-sepolia": {"chain_id": CHAINS["base-sepolia"], "class": "testnet", "cap_wei": None},
        "bsc-testnet":  {"chain_id": CHAINS["bsc-testnet"],  "class": "testnet", "cap_wei": None},
        "amoy":         {"chain_id": CHAINS["amoy"],         "class": "testnet", "cap_wei": None},
        "elysium":      {"chain_id": CHAINS["elysium"],      "class": "testnet", "cap_wei": None},
        "bsc":          {"chain_id": CHAINS["bsc"],          "class": "mainnet", "cap_wei": None},
        "ethereum":     {"chain_id": CHAINS["ethereum"],     "class": "mainnet", "cap_wei": None},
        "base":         {"chain_id": CHAINS["base"],         "class": "mainnet", "cap_wei": None},
        "polygon":      {"chain_id": CHAINS["polygon"],      "class": "mainnet", "cap_wei": None},
    },
    "destinations": {
        "self": BURNER_EVM,
        # Kontrak testnet Sepolia terdaftar (Uniswap V2 + WETH9).
        "allowlist": [
            "0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14",
            "0xF62c03E08ada871A0bEb309762E260a7a6a880E6",
            "0xeE567Fe1712Faf6149d80dA1E6934E354124CfE3",
        ],
    },
}

CREATE_TO = "CREATE"


def now_iso():
    return datetime.now(WIB).isoformat()


def ensure_state():
    """Buat state dir + policy bawaan + file log kosong bila belum ada."""
    os.makedirs(STATE_DIR, exist_ok=True)
    if not os.path.exists(POLICY_PATH):
        with open(POLICY_PATH, "w") as f:
            json.dump(DEFAULT_POLICY, f, indent=1, ensure_ascii=False)
            f.write("\n")
    for p in (INTENTS_PATH, AUDIT_PATH):
        if not os.path.exists(p):
            open(p, "a").close()


def load_policy():
    with open(POLICY_PATH) as f:
        return json.load(f)


def append_jsonl(path, obj):
    with open(path, "a") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def audit(event, intent_id=None, caller=None, chain=None, method=None, to=None,
          value_wei=None, reason=None, actor=None, tx_hash=None,
          receipt_status=None, detail=None):
    append_jsonl(AUDIT_PATH, {
        "ts": now_iso(), "event": event, "intent_id": intent_id,
        "caller": caller, "chain": chain, "method": method, "to": to,
        "value_wei": value_wei, "reason": reason, "actor": actor,
        "tx_hash": tx_hash, "receipt_status": receipt_status,
        "detail": detail,
    })


def canonical_json(obj):
    """JSON kanonis untuk payload tanda tangan (kunci terurut, tanpa spasi).
    Dipakai identik oleh brain.py dan gerbang WS walletd — jangan ubah
    formatnya di satu sisi saja."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def intent_hash_of(chain, method, to, value_wei, data, payload=None):
    """sha256 hex atas string kanonis intent.

    eth_sendTransaction: "chain|method|to_lower|value_wei|data" (v1, tetap).
    Method tanda tangan (v2): "chain|method|sign|payload" — payload adalah
    pesan apa adanya (personal_sign) atau JSON kanonis typed data
    (eth_signTypedData_v4), sehingga hash mengikat payload tanda tangan
    secara persis dan approve tetap exact-match."""
    if method in SIGN_METHODS:
        canonical = "|".join([chain, method, "sign", payload or ""])
    else:
        canonical = "|".join([chain, method, to.lower(), str(value_wei), data])
    return hashlib.sha256(canonical.encode()).hexdigest()


def intent_payload_of(rec):
    """Payload kanonis tanda tangan dari record intent (None untuk tx)."""
    if rec.get("method") == "personal_sign":
        return rec.get("message") or ""
    if rec.get("method") == "eth_signTypedData_v4":
        return rec.get("typed_data") or ""
    return None


WS_CONSUMED_PATH = os.path.join(STATE_DIR, "ws_consumed.jsonl")


def load_intents():
    """Replay intents.jsonl -> {intent_id: record terakhir}, urut kemunculan."""
    latest, order = {}, []
    if os.path.exists(INTENTS_PATH):
        with open(INTENTS_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                iid = rec["intent_id"]
                if iid not in latest:
                    order.append(iid)
                latest[iid] = rec
    # Overlay konsumsi jalur WS (brain-gate di walletd, 2026-10-30): intent
    # approved yang sudah dikonsumsi permintaan ekstensi/WS (penanda di
    # ws_consumed.jsonl, ditulis walletd) tampil sebagai executed — supaya
    # tidak bisa di-approve/deliver ulang lewat jalur HTTP dan statusnya
    # jujur di list/show. brain.py tetap satu-satunya penulis intents.jsonl.
    if os.path.exists(WS_CONSUMED_PATH):
        consumed = set()
        with open(WS_CONSUMED_PATH) as f:
            for line in f:
                line = line.strip()
                if line:
                    consumed.add(json.loads(line)["intent_id"])
        for iid in order:
            rec = latest[iid]
            if iid in consumed and rec.get("status") == "approved":
                rec = dict(rec)
                rec["status"] = "executed"
                rec["via"] = "ws"
                rec["note"] = "terkonsumsi via jalur WS (brain-gate walletd)"
                latest[iid] = rec
    return latest, order


def write_transition(rec, status, actor=None, note=None, tx_hash=None,
                     signature=None):
    new = dict(rec)
    new["status"] = status
    new["ts"] = now_iso()
    if actor is not None:
        new["actor"] = actor
    if note is not None:
        new["note"] = note
    if tx_hash is not None:
        new["tx_hash"] = tx_hash
    if signature is not None:
        new["signature"] = signature
    append_jsonl(INTENTS_PATH, new)
    return new


def fail(msg):
    print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False), file=sys.stderr)
    sys.exit(1)


def out(obj):
    print(json.dumps(obj, indent=1, ensure_ascii=False))


# ---------- walletd bridge (pola sama seperti wd.py) ----------

_id = 0

def call_walletd(method, params=None, chain=None):
    """Satu request EIP-1193 ke walletd via HTTP bridge. Tanpa retry."""
    global _id
    _id += 1
    msg = {"id": _id, "method": method, "params": params if params is not None else []}
    if chain:
        msg["chain"] = chain
    body = json.dumps(msg).encode()
    req = urllib.request.Request(WALLETD_HTTP, data=body,
                                 headers=walletd_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read())
            err = data.get("error") or {}
            raise RuntimeError("walletd error %s: %s" % (err.get("code"), err.get("message")))
        except RuntimeError:
            raise
        except Exception:
            raise RuntimeError("walletd HTTP %s" % e.code)
    except urllib.error.URLError as e:
        raise RuntimeError("walletd TIDAK TERJANGKAU di 127.0.0.1:18924 (%s)" % e)
    if data.get("error"):
        err = data["error"]
        raise RuntimeError("walletd error %s: %s" % (err.get("code"), err.get("message")))
    return data.get("result")


def walletd_http_status():
    """GET root walletd: hidup = HTTP 501 (pola wd.py)."""
    try:
        with urllib.request.urlopen(WALLETD_HTTP, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


# ---------- perintah ----------

def estimate_gas_limit(tx, chain_hex):
    """Minta eth_estimateGas LEWAT walletd (bukan angka manual per intent),
    lalu beri buffer 35%. HTTP bridge walletd memakai fallback 200.000 gas
    bila tx tanpa gas — terlalu kecil untuk deploy/LP Sepolia. Baca boleh
    diulang (flake RPC); pengiriman tetap satu kali. None bila estimasi
    tidak tersedia setelah 3x baca."""
    est_tx = {"from": BURNER_EVM, "data": tx.get("data", "0x"),
              "value": tx.get("value", "0x0")}
    if tx.get("to"):
        est_tx["to"] = tx["to"]
    last = None
    for _ in range(3):
        try:
            est = call_walletd("eth_estimateGas", [est_tx], chain_hex)
            gl = int(est, 16)
            return max(21000, (gl * 135 + 99) // 100)
        except Exception as e:
            last = e
            time.sleep(2)
    return None


def cmd_check(_):
    status = walletd_http_status()
    res = {"walletd_http": status, "walletd_alive": status == 501,
           "burner": BURNER_EVM, "chains": {}}
    for name in ("sepolia", "base-sepolia"):
        hexid = CHAINS[name]
        cid = call_walletd("eth_chainId", [], hexid)
        bal = call_walletd("eth_getBalance", [BURNER_EVM, "latest"], hexid)
        wei = int(bal, 16)
        res["chains"][name] = {
            "chain_id": cid, "chain_id_dec": int(cid, 16),
            "balance_wei": str(wei), "balance": wei / 1e18,
        }
    out(res)


def ether_to_wei(value_str):
    """Desimal ether -> wei (int). ValueError bila tidak valid / pecahan wei."""
    try:
        dec = Decimal(value_str)
    except (InvalidOperation, TypeError):
        raise ValueError("value bukan angka desimal yang valid: %r" % value_str)
    wei = dec * 10**18
    if wei != wei.to_integral_value():
        raise ValueError("value menghasilkan pecahan wei: %r" % value_str)
    return int(wei)


def cmd_propose(a):
    policy = load_policy()
    caller, chain, method = a.caller, a.chain, a.method
    is_sign = method in SIGN_METHODS
    to = a.to or ""
    if to.upper() == CREATE_TO:
        to = CREATE_TO
    data = a.data or "0x"
    reason = a.reason if a.reason is not None else ""

    # Payload tanda tangan (hanya method sign): personal_sign memakai
    # --message apa adanya; eth_signTypedData_v4 memakai --typed-data
    # (literal JSON atau @path) yang dikanonikkan SEKALI di sini — bentuk
    # kanonis itulah yang di-hash, disimpan, dan ditandatangani, sehingga
    # propose/approve/deliver dan gerbang WS melihat payload yang sama.
    sign_payload = None
    sign_fields = {}
    payload_err = None
    if method == "personal_sign":
        if a.message is None or a.message == "":
            payload_err = "personal_sign wajib --message non-kosong"
        else:
            sign_payload = a.message
            sign_fields = {"message": a.message}
    elif method == "eth_signTypedData_v4":
        raw_td = a.typed_data
        if raw_td is None or raw_td == "":
            payload_err = "eth_signTypedData_v4 wajib --typed-data"
        else:
            try:
                if raw_td.startswith("@"):
                    with open(raw_td[1:]) as f:
                        typed_obj = json.load(f)
                else:
                    typed_obj = json.loads(raw_td)
                if not isinstance(typed_obj, dict) \
                        or not typed_obj.get("types") \
                        or not typed_obj.get("primaryType") \
                        or "message" not in typed_obj:
                    raise ValueError("typed data wajib objek berisi types, "
                                     "primaryType, dan message")
                sign_payload = canonical_json(typed_obj)
                sign_fields = {"typed_data": sign_payload}
            except Exception as e:
                payload_err = "typed-data tidak valid: %s" % e

    # Konversi nilai dulu supaya audit selalu punya value_wei bila terparse.
    # Intent sign tidak memindahkan nilai: value dipaksa 0, --value diabaikan.
    value_wei = None
    value_err = None
    if is_sign:
        value_wei = 0
    else:
        try:
            value_wei = ether_to_wei(a.value)
        except ValueError as e:
            value_err = str(e)

    def reject(detail):
        audit("policy_rejected", intent_id=None, caller=caller, chain=chain,
              method=method, to=to or None,
              value_wei=str(value_wei) if value_wei is not None else None,
              reason=reason or None, detail=detail)
        fail("policy_rejected: " + detail)

    # Gerbang — urutan tetap: chain -> method -> reason -> payload (sign) /
    # value+cap -> tujuan (tujuan hanya untuk eth_sendTransaction).
    chains = policy.get("chains", {})
    if chain not in chains:
        reject("chain tidak terdaftar di brain_policy.json: %s" % chain)
    if method not in policy.get("allowed_methods", []):
        reject("method tidak diizinkan: %s (diizinkan: %s)"
               % (method, ", ".join(policy.get("allowed_methods", []))))
    if not reason.strip():
        reject("reason kosong: intent tanpa alasan tidak diproses")
    if payload_err is not None:
        reject(payload_err)
    if value_err is not None:
        reject(value_err)
    if value_wei < 0:
        reject("value negatif tidak diizinkan: %s wei" % value_wei)
    # Cap nilai DICABUT (revisi owner 2 Nov 2026 22:18 WIB): cap_wei null
    # atau absen = tanpa batas — tidak ada penolakan karena nilai. Blok
    # ini hanya hidup lagi bila file policy suatu hari mengisi cap_wei.
    cap_raw = chains[chain].get("cap_wei")
    if cap_raw is not None and value_wei > int(cap_raw):
        reject("value %s wei melebihi cap mediator chain %s (%s wei)"
               % (value_wei, chain, cap_raw))
    if is_sign:
        # Intent sign: tanpa tujuan on-chain, tanpa perpindahan nilai —
        # gerbang tujuan tidak berlaku; payload terikat exact di hash.
        to = ""
        data = "0x"
    else:
        dests = policy.get("destinations", {})
        self_addr = (dests.get("self") or BURNER_EVM).lower()
        allowlist = [x.lower() for x in dests.get("allowlist", [])]
        if to == CREATE_TO:
            # Deploy kontrak: hanya chain kelas testnet terdaftar, value
            # wajib 0, dan data wajib bytecode creation hex non-kosong.
            if chains[chain].get("class") != "testnet":
                reject("CREATE hanya diizinkan di chain testnet terdaftar: %s" % chain)
            if value_wei != 0:
                reject("CREATE wajib value 0, diberikan %s wei" % value_wei)
            try:
                raw = bytes.fromhex(data[2:] if data.startswith("0x") else "")
            except ValueError:
                reject("CREATE data bukan hex valid: %s" % data[:40])
            if not data.startswith("0x") or len(raw) < 10:
                reject("CREATE data wajib bytecode creation hex non-kosong")
        elif to.lower() != self_addr and to.lower() not in allowlist:
            reject("destination not in allowlist: %s (v1: hanya alamat burner "
                   "sendiri atau allowlist brain_policy.json)" % to)

    intent = {
        "intent_id": str(uuid.uuid4()),
        "created_at": now_iso(),
        "caller": caller, "chain": chain, "method": method, "to": to,
        "value_wei": str(value_wei), "data": data, "reason": reason,
    }
    intent.update(sign_fields)
    intent["intent_hash"] = intent_hash_of(chain, method, to,
                                           intent["value_wei"], data,
                                           payload=sign_payload)
    intent["status"] = "queued"
    intent["ts"] = now_iso()
    append_jsonl(INTENTS_PATH, intent)
    audit("intent_queued", intent_id=intent["intent_id"], caller=caller,
          chain=chain, method=method, to=to, value_wei=intent["value_wei"],
          reason=reason, detail="intent_hash=" + intent["intent_hash"])
    out({"ok": True, "intent_id": intent["intent_id"],
         "intent_hash": intent["intent_hash"], "status": "queued",
         "chain": chain, "method": method, "to": to,
         "value_wei": intent["value_wei"], "data": data, "reason": reason})


def get_intent_or_fail(intent_id):
    latest, _order = load_intents()
    rec = latest.get(intent_id)
    if rec is None:
        fail("intent tidak ditemukan: %s" % intent_id)
    return rec


def cmd_list(_):
    latest, order = load_intents()
    out([latest[i] for i in order])


def cmd_show(a):
    out(get_intent_or_fail(a.id))


def cmd_approve(a):
    rec = get_intent_or_fail(a.id)
    base = dict(caller=rec["caller"], chain=rec["chain"], method=rec["method"],
                to=rec["to"], value_wei=rec["value_wei"], reason=rec["reason"])
    if rec["status"] != "queued":
        audit("approval_rejected", intent_id=a.id, actor=a.by,
              detail="status intent bukan queued: %s" % rec["status"], **base)
        fail("approval_rejected: status intent bukan queued: %s" % rec["status"])
    if a.hash != rec["intent_hash"]:
        audit("approval_rejected", intent_id=a.id, actor=a.by,
              detail="hash mismatch: diberikan %s, tersimpan %s"
                     % (a.hash, rec["intent_hash"]), **base)
        fail("approval_rejected: intent_hash tidak sama persis "
             "(diberikan %s, tersimpan %s)" % (a.hash, rec["intent_hash"]))
    new = write_transition(rec, "approved", actor=a.by)
    audit("approved", intent_id=a.id, actor=a.by,
          detail="exact-match terverifikasi: intent_hash=" + rec["intent_hash"],
          **base)
    out({"ok": True, "intent_id": a.id, "status": new["status"], "by": a.by,
         "intent_hash": rec["intent_hash"]})


def cmd_reject(a):
    rec = get_intent_or_fail(a.id)
    base = dict(caller=rec["caller"], chain=rec["chain"], method=rec["method"],
                to=rec["to"], value_wei=rec["value_wei"], reason=rec["reason"])
    if rec["status"] in ("executed", "rejected", "execution_failed"):
        audit("reject_rejected", intent_id=a.id, actor=a.by,
              detail="status intent sudah final: %s" % rec["status"], **base)
        fail("reject_rejected: status intent sudah final: %s" % rec["status"])
    write_transition(rec, "rejected", actor=a.by, note=a.note)
    audit("rejected", intent_id=a.id, actor=a.by, detail=a.note, **base)
    out({"ok": True, "intent_id": a.id, "status": "rejected", "by": a.by,
         "note": a.note})


def deliver_sign(rec, base):
    """Eksekusi intent tanda tangan pesan (v2): walletd menandatangani
    payload lewat HTTP, lalu signature diverifikasi LOKAL (recover wajib
    == burner) sebelum intent dicatat executed. Tidak ada tx on-chain,
    tidak ada receipt, tidak ada perubahan nonce."""
    from eth_account import Account
    from eth_account.messages import encode_defunct, encode_typed_data
    chain_hex = CHAINS[rec["chain"]]
    a_id = rec["intent_id"]
    try:
        if rec["method"] == "personal_sign":
            message = rec.get("message") or ""
            sig = call_walletd("personal_sign", [message, BURNER_EVM],
                               chain_hex)
            signable = (encode_defunct(hexstr=message)
                        if message.startswith("0x")
                        else encode_defunct(text=message))
            recovered = Account.recover_message(signable, signature=sig)
        else:
            typed = json.loads(rec.get("typed_data") or "{}")
            sig = call_walletd("eth_signTypedData_v4", [BURNER_EVM, typed],
                               chain_hex)
            # eth_account 0.14: full_message dulu; fallback 3-arg tanpa
            # kunci EIP712Domain (pola yang sama seperti walletd).
            try:
                signable = encode_typed_data(full_message=typed)
            except Exception:
                _types = {k: v for k, v in typed.get("types", {}).items()
                          if k != "EIP712Domain"}
                signable = encode_typed_data(typed.get("domain", {}), _types,
                                             typed.get("message", {}))
            recovered = Account.recover_message(signable, signature=sig)
    except RuntimeError as e:
        write_transition(rec, "execution_failed", note=str(e))
        audit("execution_failed", intent_id=a_id, detail=str(e), **base)
        fail("execution_failed: " + str(e))
    if recovered.lower() != BURNER_EVM.lower():
        write_transition(rec, "execution_failed",
                         note="recover signature bukan burner: %s" % recovered)
        audit("execution_failed", intent_id=a_id,
              detail="recover signature bukan burner: %s" % recovered, **base)
        fail("execution_failed: recover signature bukan burner: %s"
             % recovered)
    write_transition(rec, "executed", signature=sig)
    audit("signed", intent_id=a_id,
          detail="signature terverifikasi recover==burner via walletd HTTP "
                 "(%s); tanpa tx on-chain" % rec["method"], **base)
    out({"ok": True, "intent_id": a_id, "status": "executed",
         "method": rec["method"], "chain": rec["chain"],
         "signature": sig, "recovered": recovered, "match": True})


def cmd_deliver(a):
    rec = get_intent_or_fail(a.id)
    base = dict(caller=rec["caller"], chain=rec["chain"], method=rec["method"],
                to=rec["to"], value_wei=rec["value_wei"], reason=rec["reason"])
    if rec["status"] != "approved":
        audit("deliver_rejected", intent_id=a.id,
              detail="status intent bukan approved: %s" % rec["status"], **base)
        fail("deliver_rejected: status intent bukan approved: %s"
             % rec["status"])
    # Verifikasi ulang intent_hash dari data tersimpan (exact-match).
    recomputed = intent_hash_of(rec["chain"], rec["method"], rec["to"],
                                rec["value_wei"], rec["data"],
                                payload=intent_payload_of(rec))
    if recomputed != rec["intent_hash"]:
        audit("deliver_rejected", intent_id=a.id,
              detail="intent_hash tersimpan tidak cocok dengan data intent: "
                     "tersimpan %s, hitung ulang %s"
                     % (rec["intent_hash"], recomputed), **base)
        fail("deliver_rejected: intent_hash tersimpan tidak cocok dengan "
             "data intent")

    if rec["method"] in SIGN_METHODS:
        deliver_sign(rec, base)
        return

    chain_hex = CHAINS[rec["chain"]]
    tx = {"value": hex(int(rec["value_wei"])), "chainId": chain_hex}
    if rec["to"] != CREATE_TO:
        tx["to"] = rec["to"]
    if rec["data"] and rec["data"] != "0x":
        tx["data"] = rec["data"]
    gas_limit = estimate_gas_limit(tx, chain_hex)
    if gas_limit:
        tx["gas"] = hex(gas_limit)
    try:
        tx_hash = call_walletd("eth_sendTransaction", [tx], chain_hex)
    except RuntimeError as e:
        write_transition(rec, "execution_failed", note=str(e))
        audit("execution_failed", intent_id=a.id, detail=str(e), **base)
        fail("execution_failed: " + str(e))

    # Tx terbit: intent terpakai satu kali -> transisi executed dulu,
    # lalu verifikasi receipt (maks ~60 detik, polling tanpa retry kirim).
    write_transition(rec, "executed", tx_hash=tx_hash)
    audit("executed", intent_id=a.id, tx_hash=tx_hash,
          detail="eth_sendTransaction terkirim via walletd (chain %s)"
                 % chain_hex, **base)

    receipt = None
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            receipt = call_walletd("eth_getTransactionReceipt",
                                   [tx_hash], chain_hex)
        except RuntimeError:
            receipt = None
        if receipt:
            break
        time.sleep(3)

    if not receipt:
        audit("execution_failed", intent_id=a.id, tx_hash=tx_hash,
              detail="receipt tidak muncul dalam ~60 detik", **base)
        fail("execution_failed: receipt tidak muncul dalam ~60 detik "
             "untuk tx %s" % tx_hash)

    status = receipt.get("status")
    if status == "0x1":
        detail = "blockNumber=%s" % receipt.get("blockNumber")
        if receipt.get("contractAddress"):
            detail += " contractAddress=%s" % receipt["contractAddress"]
        audit("receipt_verified", intent_id=a.id, tx_hash=tx_hash,
              receipt_status=status, detail=detail, **base)
        out({"ok": True, "intent_id": a.id, "status": "executed",
             "tx_hash": tx_hash, "receipt_status": status,
             "blockNumber": receipt.get("blockNumber"),
             "contractAddress": receipt.get("contractAddress"),
             "chain": rec["chain"]})
    else:
        audit("execution_failed", intent_id=a.id, tx_hash=tx_hash,
              receipt_status=status,
              detail="receipt status bukan 0x1: %s" % status, **base)
        fail("execution_failed: receipt status %s untuk tx %s"
             % (status, tx_hash))


def main():
    ensure_state()
    ap = argparse.ArgumentParser(
        prog="brain",
        description="Brain — lapis intent + gerbang kebijakan + persetujuan "
                    "exact-match di atas walletd (signing tetap di walletd).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="status walletd + chainId & saldo burner "
                                 "(sepolia & base-sepolia)")
    p = sub.add_parser("propose", help="ajukan intent (lewat gerbang kebijakan)")
    p.add_argument("--caller", required=True)
    p.add_argument("--chain", required=True)
    p.add_argument("--method", default="eth_sendTransaction")
    p.add_argument("--to", default="",
                   help="tujuan (eth_sendTransaction); kosong untuk intent sign")
    p.add_argument("--value", default="0",
                   help="nilai dalam ether desimal (diabaikan untuk intent sign)")
    p.add_argument("--data", default="0x")
    p.add_argument("--message", default=None,
                   help="pesan untuk intent personal_sign")
    p.add_argument("--typed-data", dest="typed_data", default=None,
                   help="typed data JSON (literal atau @file.json) untuk "
                        "intent eth_signTypedData_v4")
    p.add_argument("--reason", default=None,
                   help="alasan intent (WAJIB non-kosong; tanpa alasan ditolak gerbang)")
    sub.add_parser("list", help="status terakhir semua intent")
    p = sub.add_parser("show", help="status terakhir satu intent")
    p.add_argument("--id", required=True)
    p = sub.add_parser("approve", help="setujui intent (exact-match hash)")
    p.add_argument("--id", required=True)
    p.add_argument("--hash", required=True)
    p.add_argument("--by", required=True)
    p = sub.add_parser("reject", help="tolak intent")
    p.add_argument("--id", required=True)
    p.add_argument("--by", required=True)
    p.add_argument("--note", default="")
    p = sub.add_parser("deliver", help="eksekusi intent approved via walletd (satu kali, tanpa retry)")
    p.add_argument("--id", required=True)

    a = ap.parse_args()
    {"check": cmd_check, "propose": cmd_propose, "list": cmd_list,
     "show": cmd_show, "approve": cmd_approve, "reject": cmd_reject,
     "deliver": cmd_deliver}[a.cmd](a)


if __name__ == "__main__":
    main()
