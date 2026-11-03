#!/usr/bin/env bash
# BrainD Wallet — native installer for a fresh VM (no Docker).
# Run from the repository checkout:  ./install.sh [--systemd]
#
# What it does:
#   1. creates a Python venv in .venv and installs requirements
#   2. creates ~/.braind/ with a caller token (0600)
#   3. seed setup: generate a new BIP-39 mnemonic or import yours
#      (input is hidden, the seed is written to ~/.braind/seed, mode 0600,
#       and never printed again after the one-time generation display)
#   4. initialises the Brain policy with your derived EVM address as "self"
#   5. optionally installs a systemd unit (with --systemd, needs sudo)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRAIND_HOME="${BRAIND_HOME:-$HOME/.braind}"
SEED_FILE="$BRAIND_HOME/seed"
TOKEN_FILE="$BRAIND_HOME/walletd.token"
STATE_DIR="$REPO_DIR/state"
PY="$REPO_DIR/.venv/bin/python"

echo "== BrainD Wallet installer =="
echo "repo:  $REPO_DIR"
echo "home:  $BRAIND_HOME"

command -v python3 >/dev/null || { echo "python3 not found — install Python 3.11+ first."; exit 1; }

# 1. venv + dependencies
if [ ! -x "$PY" ]; then
  python3 -m venv "$REPO_DIR/.venv"
fi
"$REPO_DIR/.venv/bin/pip" install -q -r "$REPO_DIR/requirements.txt"
echo "[ok] dependencies installed"

# 2. runtime home + caller token
mkdir -p "$BRAIND_HOME" "$STATE_DIR"
chmod 700 "$BRAIND_HOME"
if [ ! -s "$TOKEN_FILE" ]; then
  "$PY" -c "import secrets; print(secrets.token_hex(32))" > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
  echo "[ok] caller token created at $TOKEN_FILE"
fi

# 3. seed
if [ -s "$SEED_FILE" ]; then
  echo "[ok] seed already exists at $SEED_FILE — kept as is"
else
  echo
  echo "Seed setup — choose:"
  echo "  1) generate a NEW mnemonic (shown ONCE — write it down offline)"
  echo "  2) import an existing mnemonic (hidden input)"
  echo "  3) skip — I will place the seed file myself"
  read -rp "choice [1/2/3]: " choice
  case "$choice" in
    1)
      MNEMONIC="$("$PY" -c "from bip_utils import Bip39MnemonicGenerator, Bip39WordsNum; print(Bip39MnemonicGenerator().FromWordsNumber(Bip39WordsNum.WORDS_NUM_24))")"
      echo
      echo "YOUR NEW MNEMONIC (shown once, never stored anywhere else):"
      echo "  $MNEMONIC"
      read -rp "Type YES after you have written it down offline: " ok
      [ "$ok" = "YES" ] || { echo "aborted — nothing written."; exit 1; }
      ( umask 077; printf '%s' "$MNEMONIC" > "$SEED_FILE" )
      unset MNEMONIC
      echo "[ok] seed written to $SEED_FILE"
      ;;
    2)
      read -rsp "paste mnemonic: " M1; echo
      read -rsp "paste again:   " M2; echo
      [ "$M1" = "$M2" ] || { echo "mismatch — aborted, nothing written."; exit 1; }
      ( umask 077; printf '%s' "$M1" > "$SEED_FILE" )
      unset M1 M2
      echo "[ok] seed written to $SEED_FILE"
      ;;
    *)
      echo "[skip] place your mnemonic at $SEED_FILE (mode 0600) before starting."
      ;;
  esac
fi

# 4. Brain policy: set destinations.self to the derived EVM address
if [ -s "$SEED_FILE" ]; then
  BRAIND_STATE_DIR="$STATE_DIR" "$PY" "$REPO_DIR/bin/brain.py" check >/dev/null 2>&1 || true
  BRAIND_SEED_FILE="$SEED_FILE" "$PY" - "$STATE_DIR/brain_policy.json" <<'PYEOF'
import json, os, sys
from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins
seed = open(os.environ["BRAIND_SEED_FILE"]).read().strip()
acct = Bip44.FromSeed(Bip39SeedGenerator(seed).Generate(), Bip44Coins.ETHEREUM) \
           .Purpose().Coin().Account(0).Change(0).AddressIndex(0)
addr = acct.PublicKey().ToAddress()
path = sys.argv[1]
policy = json.load(open(path))
policy.setdefault("destinations", {})["self"] = addr
json.dump(policy, open(path, "w"), indent=1)
print("[ok] brain policy self =", addr)
PYEOF
fi

# 5. systemd (optional)
if [ "${1:-}" = "--systemd" ]; then
  UNIT=/etc/systemd/system/braind.service
  sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=Super Agentic BrainD Wallet (walletd signer)
After=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$REPO_DIR
Environment=BRAIND_SEED_FILE=$SEED_FILE
Environment=BRAIND_TOKEN_FILE=$TOKEN_FILE
Environment=BRAIND_STATE_DIR=$STATE_DIR
ExecStart=$PY $REPO_DIR/daemon/walletd.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
  sudo systemctl daemon-reload
  sudo systemctl enable --now braind.service
  echo "[ok] systemd service 'braind' enabled and started"
else
  echo
  echo "Start the signer with:"
  echo "  BRAIND_SEED_FILE=$SEED_FILE BRAIND_TOKEN_FILE=$TOKEN_FILE BRAIND_STATE_DIR=$STATE_DIR \\"
  echo "    $PY $REPO_DIR/daemon/walletd.py"
  echo "or re-run with --systemd to install a service."
fi

echo
echo "Verify (with the daemon running):"
echo "  BRAIND_TOKEN_FILE=$TOKEN_FILE $PY $REPO_DIR/bin/wd.py balance --chain sepolia"
echo "  BRAIND_STATE_DIR=$STATE_DIR BRAIND_TOKEN_FILE=$TOKEN_FILE $PY $REPO_DIR/bin/brain.py check"
echo
echo "Golden rules: the seed lives ONLY on this signer machine; ports 18923/18924"
echo "stay on localhost or a private network — never expose them to the internet."
