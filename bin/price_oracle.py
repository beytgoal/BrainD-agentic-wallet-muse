#!/usr/bin/env python3
"""price_oracle — baca harga native/USD dari Chainlink price feeds (keyless,
on-chain) lewat walletd, untuk konteks fee + valuasi audit/report tumpukan
Super Agentic BrainD Wallet.

Perintah owner 2 Nov 2026 22:21 WIB: data market pakai oracle gratis
(Chainlink) — bukan angka hardcode. Feed dibaca via eth_call ke kontrak
AggregatorV3 lewat RPC yang sudah dipakai walletd (jembatan HTTP 18924,
token pemanggil seperti brain.py). decimals() dibaca dinamis dari feed;
staleness dari latestRoundData().updatedAt vs heartbeat feed.

Chain TANPA feed (elysium testnet dkk.) -> harga "tidak tersedia".
Modul ini TIDAK PERNAH mengarang harga dan TIDAK menandatangani apa pun.

Jalankan dengan venv wallet:
    ~/workspace/.wallet-venv/bin/python bin/price_oracle.py price --chain ethereum
    ~/workspace/.wallet-venv/bin/python bin/price_oracle.py price --all
    ~/workspace/.wallet-venv/bin/python bin/price_oracle.py gas --chain elysium
    ~/workspace/.wallet-venv/bin/python bin/price_oracle.py feeds

Dipakai sebagai modul:  from price_oracle import price_usd, gas_gwei
    price_usd("ethereum") -> {"available": True, "price": ..., "source":
    "chainlink", "updated_at": ..., "age_sec": ..., "stale": ...}
    price_usd("elysium")  -> {"available": False, "price": None, ...}
"""
import argparse, json, os, sys, time, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

WALLETD_HTTP = "http://127.0.0.1:18924/"
WALLETD_TOKEN_FILE = os.path.expanduser("~/.burner/walletd.token")
WIB = timezone(timedelta(hours=7))

# chain Brain -> chainId hex (cermin brain.py CHAINS).
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

# Registry feed Chainlink native/USD per chain. Semua alamat di bawah
# diverifikasi on-chain via description() pada 2 Nov 2026 (lihat uji
# oracle di log daemon/uji-brain-v2-revisi-2026-11-02.log) — tidak ada
# alamat karangan; chain tanpa feed terverifikasi tidak didaftarkan.
FEEDS = {
    "ethereum": {
        "feed": "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419",
        "pair": "ETH/USD", "heartbeat_sec": 3600, "verified_onchain": True,
    },
    "base": {
        "feed": "0x71041dddad3595F9CEd3DcCFBe3D1F4b0a16Bb70",
        "pair": "ETH/USD", "heartbeat_sec": 3600, "verified_onchain": True,
    },
    "bsc": {
        "feed": "0x0567F2323251f0Aab15c8dFb1967E4e8A7D42aeE",
        "pair": "BNB/USD", "heartbeat_sec": 3600, "verified_onchain": True,
    },
    "polygon": {
        # Feed bernama on-chain "MATIC / USD" (terverifikasi description()
        # 2 Nov 2026); pasca migrasi MATIC->POL (1:1, 4 Sep 2024) feed
        # yang sama melacak harga POL.
        "feed": "0xAB594600376Ec9fD91F8e885dADF0CE036862dE0",
        "pair": "POL/USD (feed MATIC/USD)", "heartbeat_sec": 3600,
        "verified_onchain": True,
    },
    "sepolia": {
        "feed": "0x694AA1769357215DE4FAC081bf1f309aDC325306",
        "pair": "ETH/USD", "heartbeat_sec": 86400, "verified_onchain": True,
    },
    # elysium (testnet Kinetiq) tidak punya feed Chainlink native/USD;
    # base-sepolia / bsc-testnet / amoy tidak didaftarkan di v1 modul ini.
}

SEL_DESCRIPTION = "0x7284e416"   # description()
SEL_DECIMALS = "0x313ce567"      # decimals()
SEL_LATEST = "0xfeaf968c"        # latestRoundData()


def _headers():
    try:
        with open(WALLETD_TOKEN_FILE) as f:
            tok = f.read().strip()
    except Exception:
        tok = ""
    if len(tok) < 32:
        raise RuntimeError("token pemanggil walletd tidak ditemukan/invalid")
    return {"Content-Type": "application/json", "X-Walletd-Token": tok}


_id = 0

def call_walletd(method, params=None, chain_hex=None):
    """Satu request EIP-1193 ke walletd via HTTP bridge. Tanpa retry."""
    global _id
    _id += 1
    msg = {"id": _id, "method": method, "params": params or []}
    if chain_hex:
        msg["chain"] = chain_hex
    req = urllib.request.Request(WALLETD_HTTP, data=json.dumps(msg).encode(),
                                 headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read())
        except Exception:
            raise RuntimeError("walletd HTTP %s" % e.code)
    if data.get("error"):
        err = data["error"]
        raise RuntimeError("walletd error %s: %s"
                           % (err.get("code"), err.get("message")))
    return data.get("result")


def _eth_call(to, data, chain_hex):
    return call_walletd("eth_call", [{"to": to, "data": data}, "latest"],
                        chain_hex)


_decimals_cache = {}

def feed_decimals(chain):
    """decimals() feed, dibaca dinamis dari kontrak (cache per proses)."""
    if chain not in _decimals_cache:
        info = FEEDS[chain]
        raw = _eth_call(info["feed"], SEL_DECIMALS, CHAINS[chain])
        _decimals_cache[chain] = int(raw, 16)
    return _decimals_cache[chain]


def price_usd(chain):
    """Harga native/USD satu chain dari Chainlink.

    Return dict selalu berisi 'available'. Bila chain tidak punya feed:
    available False + price None (TIDAK dikarang). Bila bacaan RPC gagal:
    RuntimeError diteruskan ke pemanggil (jujur gagal, bukan fallback
    angka). 'stale' = umur data melewati heartbeat feed."""
    info = FEEDS.get(chain)
    if info is None:
        return {"chain": chain, "available": False, "price": None,
                "source": None, "pair": None,
                "note": "tidak ada feed Chainlink native/USD untuk chain "
                        "ini — harga tidak tersedia (tidak dikarang)"}
    dec = feed_decimals(chain)
    raw = _eth_call(info["feed"], SEL_LATEST, CHAINS[chain])
    b = bytes.fromhex(raw[2:])
    words = [int.from_bytes(b[i * 32:(i + 1) * 32], "big", signed=(i == 1))
             for i in range(5)]
    round_id, answer, _started, updated_at, _answered_in = words
    now = int(time.time())
    age = now - updated_at
    return {
        "chain": chain, "available": True,
        "pair": info["pair"], "price": answer / 10 ** dec,
        "source": "chainlink", "feed": info["feed"],
        "verified_onchain": info.get("verified_onchain", False),
        "decimals": dec, "round_id": round_id,
        "updated_at": updated_at,
        "updated_at_wib": datetime.fromtimestamp(updated_at, WIB).isoformat(),
        "read_at_wib": datetime.fromtimestamp(now, WIB).isoformat(),
        "age_sec": age, "heartbeat_sec": info["heartbeat_sec"],
        "stale": age > info["heartbeat_sec"],
    }


def gas_gwei(chain):
    """eth_gasPrice chain apa adanya, dalam gwei (konteks fee — fee ikut
    chain sejak revisi owner 2 Nov 2026 22:18 WIB)."""
    wei = int(call_walletd("eth_gasPrice", [], CHAINS[chain]), 16)
    return {"chain": chain, "gas_price_wei": wei,
            "gas_price_gwei": wei / 1e9, "source": "eth_gasPrice via walletd"}


def main():
    ap = argparse.ArgumentParser(prog="price_oracle",
                                 description="Oracle harga Chainlink via walletd")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("price", help="harga native/USD satu chain")
    p.add_argument("--chain", required=True)
    sub.add_parser("all", help="harga semua chain terdaftar + yang kosong")
    p = sub.add_parser("gas", help="eth_gasPrice chain (gwei)")
    p.add_argument("--chain", required=True)
    sub.add_parser("feeds", help="registry feed")
    a = ap.parse_args()
    if a.cmd == "price":
        out = price_usd(a.chain)
    elif a.cmd == "all":
        out = {}
        for name in CHAINS:
            try:
                out[name] = price_usd(name)
            except Exception as e:
                out[name] = {"chain": name, "available": False,
                             "price": None, "source": "chainlink",
                             "error": str(e)}
    elif a.cmd == "gas":
        out = gas_gwei(a.chain)
    else:
        out = FEEDS
    print(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
