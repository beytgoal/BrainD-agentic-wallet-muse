#!/usr/bin/env python3
"""wd — Walletd Direct CLI (Jalur 2: eksekusi on-chain TANPA browser).

Satu-satunya jalur formal untuk aksi on-chain langsung dari VM, tanpa
browser agen, tanpa ekstensi, tanpa WalletConnect, tanpa dApp HP.

Cara kerja: helper ini TIDAK PERNAH membaca seed dan TIDAK PERNAH
menandatangani sendiri. Semua request diteruskan sebagai JSON EIP-1193
ke daemon walletd lewat HTTP bridge localhost (127.0.0.1:18924, POST).
Signing, policy (auto-approve <=0,5 native; Elysium <=0,05 HYPE),
nonce, fee, dan broadcast semuanya dilakukan walletd.

Jalankan dengan venv wallet agar verifikasi signature (eth_account)
tersedia:
    ~/workspace/.wallet-venv/bin/python bin/wd.py <cmd> ...

Perintah:
  chains                       daftar chain EVM yang didukung walletd
  accounts                     eth_requestAccounts (alamat burner EVM)
  chainid --chain <nama>       eth_chainId
  block --chain <nama>         eth_blockNumber
  balance --chain <nama>       eth_getBalance (alamat burner)
  nonce --chain <nama>         eth_getTransactionCount (pending)
  call --chain <nama> --to <addr> --data <calldata> [--from] [--value]
                               eth_call (read-only, tanpa tx)
  sign --message <teks>        personal_sign + verifikasi recover lokal
  sign-typed --json <file|->   eth_signTypedData_v4 + verifikasi recover
  send --chain <nama> --to <addr> --value <eth> [--data <calldata>]
       [--gas <int>]           eth_sendTransaction (kontrak call = --data)
                               DITOLAK walletd bila di atas cap policy.
  sol balance                  sol_getBalance (alamat Solana burner)
  sol sign --message <teks>    sol_signMessage (base64 signature)
  sui balance                  sui_getBalance (alamat Sui burner)
  cosmos balance               cosmos_getBalance (alamat Cosmos burner)

Chain: bsc | ethereum | base | polygon | elysium | sepolia | bsc-testnet | base-sepolia | amoy
"""
import argparse, base64, json, os, sys, time, urllib.request, urllib.error

WALLETD_HTTP = os.environ.get("BRAIND_WALLETD_URL", "http://127.0.0.1:18924/")
WALLETD_TOKEN_FILE = os.environ.get("BRAIND_TOKEN_FILE", os.path.expanduser("~/.burner/walletd.token"))
_BURNER_EVM = None

def burner_evm():
    """Alamat EVM daemon ini — ditanyakan ke walletd (eth_requestAccounts),
    bukan konstanta tertanam. Override manual: BRAIND_SELF_ADDRESS."""
    global _BURNER_EVM
    if _BURNER_EVM is None:
        _BURNER_EVM = os.environ.get("BRAIND_SELF_ADDRESS") or call_walletd("eth_requestAccounts")[0]
    return _BURNER_EVM

def walletd_headers():
    """Header HTTP walletd termasuk token pemanggil (2026-10-30).
    Token TIDAK pernah ditampilkan; file hilang = berhenti dengan pesan jelas."""
    try:
        with open(WALLETD_TOKEN_FILE) as f:
            tok = f.read().strip()
    except Exception:
        tok = ""
    if len(tok) < 32:
        raise SystemExit(
            "token pemanggil walletd tidak ditemukan/invalid di %s.\n"
            "walletd menolak semua POST tanpa header X-Walletd-Token." % WALLETD_TOKEN_FILE)
    return {"Content-Type": "application/json", "X-Walletd-Token": tok}

CHAINS = {
    "bsc":      "0x38",
    "ethereum": "0x1",
    "base":     "0x2105",
    "polygon":  "0x89",
    "elysium":  "0x185d9",
    "sepolia":  "0xaa36a7",
    "bsc-testnet": "0x61",
    "base-sepolia": "0x14a34",
    "amoy":     "0x13882",
}
CHAIN_IDS = {v: k for k, v in CHAINS.items()}

_id = 0

def call_walletd(method, params=None, chain=None):
    """Satu request EIP-1193 ke walletd via HTTP bridge. Mengembalikan result."""
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
            raise SystemExit("walletd error %s: %s" % (err.get("code"), err.get("message")))
        except SystemExit:
            raise
        except Exception:
            raise SystemExit("walletd HTTP %s" % e.code)
    except urllib.error.URLError as e:
        raise SystemExit(
            "walletd TIDAK TERJANGKAU di 127.0.0.1:18924 (%s).\n"
            "Restart manual:\n"
            "  cd ~/workspace/superagentic-wallet/daemon && "
            "setsid nohup ~/workspace/.wallet-venv/bin/python walletd.py "
            ">> walletd.out 2>&1 < /dev/null &\n"
            "Lalu verifikasi: curl -s http://127.0.0.1:18924/ (harus 501)." % e)
    if data.get("error"):
        err = data["error"]
        raise SystemExit("walletd error %s: %s" % (err.get("code"), err.get("message")))
    return data.get("result")

def out(obj):
    print(json.dumps(obj, indent=1, ensure_ascii=False))

def cmd_chains(_):
    out([{"chain": k, "chainIdHex": v, "chainId": int(v, 16)} for k, v in CHAINS.items()])

def cmd_accounts(_):
    accts = call_walletd("eth_requestAccounts")
    out({"accounts": accts, "burner_match": bool(accts) and accts[0].lower() == burner_evm().lower()})

def cmd_chainid(a):
    cid = call_walletd("eth_chainId", [], CHAINS[a.chain])
    out({"chain": a.chain, "chainId": cid, "chainIdDec": int(cid, 16)})

def cmd_block(a):
    n = call_walletd("eth_blockNumber", [], CHAINS[a.chain])
    out({"chain": a.chain, "blockNumber": n, "blockNumberDec": int(n, 16)})

def cmd_balance(a):
    bal = call_walletd("eth_getBalance", [burner_evm(), "latest"], CHAINS[a.chain])
    wei = int(bal, 16)
    out({"chain": a.chain, "address": burner_evm(),
         "balance_wei": str(wei), "balance": wei / 1e18})

def cmd_nonce(a):
    n = call_walletd("eth_getTransactionCount", [burner_evm(), "pending"], CHAINS[a.chain])
    out({"chain": a.chain, "address": burner_evm(), "nonce": int(n, 16)})

def cmd_call(a):
    tx = {"to": a.to, "data": a.data}
    if a.value:
        tx["value"] = hex(int(float(a.value) * 1e18))
    res = call_walletd("eth_call", [tx, "latest"], CHAINS[a.chain])
    out({"chain": a.chain, "to": a.to, "data": a.data, "result": res})

def _recover_evm(message, sig):
    """Verifikasi lokal: recover alamat dari signature personal_sign."""
    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
        return Account.recover_message(encode_defunct(text=message), signature=sig)
    except Exception as e:
        return "verifikasi gagal (eth_account tidak tersedia): %s" % e

def cmd_sign(a):
    msg_hex = "0x" + a.message.encode().hex()
    sig = call_walletd("personal_sign", [msg_hex, burner_evm()])
    recovered = _recover_evm(a.message, sig)
    ok = isinstance(recovered, str) and recovered.lower() == burner_evm().lower()
    out({"message": a.message, "signature": sig,
         "recovered": recovered, "burner": burner_evm(), "match": ok})

def cmd_sign_typed(a):
    raw = sys.stdin.read() if a.json == "-" else open(a.json).read()
    typed = json.loads(raw)
    sig = call_walletd("eth_signTypedData_v4", [burner_evm(), typed])
    # Verifikasi recover EIP-712 lokal bila eth_account tersedia.
    recovered = None
    try:
        from eth_account import Account
        from eth_account.messages import encode_typed_data
        # eth_account 0.14: verifikasi harus pakai full_message (bentuk
        # 3-arg menolak types yang memuat kunci EIP712Domain).
        try:
            signable = encode_typed_data(full_message=typed)
        except Exception:
            _types = {k: v for k, v in typed.get("types", {}).items()
                      if k != "EIP712Domain"}
            signable = encode_typed_data(typed.get("domain", {}), _types,
                                         typed.get("message", {}))
        recovered = Account.recover_message(signable, signature=sig)
    except Exception as e:
        recovered = "verifikasi gagal: %s" % e
    ok = isinstance(recovered, str) and recovered.lower() == burner_evm().lower()
    out({"signature": sig, "recovered": recovered, "burner": burner_evm(), "match": ok})

def cmd_send(a):
    tx = {"to": a.to, "value": hex(int(float(a.value) * 1e18)),
          "chainId": CHAINS[a.chain]}
    if a.data:
        tx["data"] = a.data
    if a.gas:
        tx["gas"] = hex(a.gas)
    t0 = time.time()
    h = call_walletd("eth_sendTransaction", [tx], CHAINS[a.chain])
    out({"chain": a.chain, "tx_hash": h, "from": burner_evm(), "to": a.to,
         "value_eth": a.value, "data": a.data or "0x",
         "total_ms": round((time.time() - t0) * 1000, 1),
         "note": "Policy walletd berlaku: auto-approve <=0,5 native, "
                 "Elysium <=0,05 HYPE. Di atas itu DENY."})

def cmd_sol_balance(_):
    lamports = call_walletd("sol_getBalance")
    out({"chain": "solana",
         "balance_lamports": lamports, "balance_sol": lamports / 1e9})

def cmd_sol_sign(a):
    raw_b64 = base64.b64encode(a.message.encode()).decode()
    res = call_walletd("sol_signMessage", [raw_b64])
    out({"message": a.message, "signature_base64": res.get("signature")})

def cmd_sui_balance(_):
    res = call_walletd("sui_getBalance")
    out({"chain": "sui",
         "balance": res})

def cmd_cosmos_balance(_):
    res = call_walletd("cosmos_getBalance")
    out({"chain": "cosmos",
         "balances": res})

ap = argparse.ArgumentParser(prog="wd",
    description="Walletd Direct CLI — aksi on-chain tanpa browser (signing via walletd).")
sub = ap.add_subparsers(dest="cmd", required=True)

sub.add_parser("chains", help="daftar chain EVM yang didukung walletd")
sub.add_parser("accounts", help="eth_requestAccounts")
p = sub.add_parser("chainid", help="eth_chainId"); p.add_argument("--chain", required=True, choices=list(CHAINS))
p = sub.add_parser("block", help="eth_blockNumber"); p.add_argument("--chain", required=True, choices=list(CHAINS))
p = sub.add_parser("balance", help="eth_getBalance burner"); p.add_argument("--chain", required=True, choices=list(CHAINS))
p = sub.add_parser("nonce", help="eth_getTransactionCount burner"); p.add_argument("--chain", required=True, choices=list(CHAINS))
p = sub.add_parser("call", help="eth_call read-only")
p.add_argument("--chain", required=True, choices=list(CHAINS))
p.add_argument("--to", required=True); p.add_argument("--data", required=True)
p.add_argument("--value", default=None, help="ETH, default 0")
p = sub.add_parser("sign", help="personal_sign + verifikasi recover")
p.add_argument("--message", required=True)
p = sub.add_parser("sign-typed", help="eth_signTypedData_v4 + verifikasi recover")
p.add_argument("--json", required=True, help="file JSON typed-data, atau - untuk stdin")
p = sub.add_parser("send", help="eth_sendTransaction (policy walletd berlaku)")
p.add_argument("--chain", required=True, choices=list(CHAINS))
p.add_argument("--to", required=True); p.add_argument("--value", default="0")
p.add_argument("--data", default=None, help="calldata kontrak (untuk contract call)")
p.add_argument("--gas", type=int, default=None)
p = sub.add_parser("sol", help="Solana via walletd")
sp = p.add_subparsers(dest="solcmd", required=True)
sp.add_parser("balance", help="sol_getBalance")
q = sp.add_parser("sign", help="sol_signMessage"); q.add_argument("--message", required=True)
p = sub.add_parser("sui", help="Sui via walletd")
sp = p.add_subparsers(dest="suicmd", required=True)
sp.add_parser("balance", help="sui_getBalance")
p = sub.add_parser("cosmos", help="Cosmos via walletd")
sp = p.add_subparsers(dest="cosmoscmd", required=True)
sp.add_parser("balance", help="cosmos_getBalance")

a = ap.parse_args()
{"chains": cmd_chains, "accounts": cmd_accounts, "chainid": cmd_chainid,
 "block": cmd_block, "balance": cmd_balance, "nonce": cmd_nonce,
 "call": cmd_call, "sign": cmd_sign, "sign-typed": cmd_sign_typed,
 "send": cmd_send,
 "sol": lambda x: {"balance": cmd_sol_balance, "sign": cmd_sol_sign}[x.solcmd](x),
 "sui": lambda x: cmd_sui_balance(x),
 "cosmos": lambda x: cmd_cosmos_balance(x)}[a.cmd](a)
