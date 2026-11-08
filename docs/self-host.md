# Self-hosting BrainD Wallet

BrainD is built to run on infrastructure you control. There is no central
server, no hosted key service, and no account to sign up for.

## Topology

Two roles, which can live on one machine or two:

```
┌─ Agent machine ────────────┐        ┌─ Signer machine ───────────────┐
│ your AI agent / scripts    │        │ walletd (signing daemon)       │
│                            │  SSH   │ Brain (intent gate, CLI+state) │
│ brain.py propose ──────────┼───────►│ seed (0600) · caller token     │
│ brain.py approve (you)     │ tunnel │ policy + intent/audit logs     │
│ wd.py (status, balances)   │        │ ports 18923/18924: private only│
└────────────────────────────┘        └────────────────────────────────┘
```

- **Signer** — the only place the seed exists. Runs `walletd` and holds the
  Brain state (policy, intents, audit log). Brain and walletd are coupled
  through that state directory and the caller token, so they always run
  together on this machine.
- **Agent** — proposes intents. It never holds the seed and never holds the
  walletd caller token for gated signing: it invokes `brain.py` **on the
  signer** (over SSH, see below) or, in single-machine setups, locally.

## Mode A — single machine (simplest)

Everything on one VM. Follow `install.sh` (native) or `docker compose up -d`
(Docker). Agent tools run locally; Brain's propose → approve → deliver gate
is the security boundary.

## Mode B — split signer / agent (recommended)

Put the signer on its own small VM (it can be tiny and mostly idle) and keep
the agent environment separate, so a compromised agent machine yields no keys
and no signing ability — only the ability to *propose* intents that still
need approval at the gate.

1. Install on the signer with `install.sh --systemd` or Docker.
2. Connect the two machines privately — any of:
   - **SSH** (simplest): the agent runs
     `ssh signer "cd ~/braind-wallet && BRAIND_STATE_DIR=$PWD/state BRAIND_TOKEN_FILE=~/.braind/walletd.token .venv/bin/python bin/brain.py propose ..."`
     with a dedicated, key-only SSH user on the signer.
   - **WireGuard / Tailscale**: put both machines on one private network and
     set `BRAIND_WALLETD_URL=http://<signer-private-ip>:18924/` on the agent
     for read/status calls. Gated signing still goes through Brain on the
     signer.
3. Firewall the signer: ports 18923/18924 must never be reachable from the
   public internet. Localhost or private-network interfaces only.

## Configuration (environment)

| Variable | Default | Meaning |
|---|---|---|
| `BRAIND_SEED_FILE` | `~/.burner/seed` | BIP-39 mnemonic file (0600), signer only |
| `BRAIND_TOKEN_FILE` | `~/.burner/walletd.token` | caller token for walletd HTTP |
| `BRAIND_STATE_DIR` | `<repo>/state` | Brain policy + intents/audit (shared by walletd & brain.py) |
| `BRAIND_BIND` | `127.0.0.1` | walletd bind address |
| `BRAIND_HTTP_PORT` | `18924` | walletd HTTP port |
| `BRAIND_WS_PORT` | `18923` | walletd WebSocket port (browser extension) |
| `BRAIND_LOG_FILE` | `~/.braind/walletd.log` | daemon log file (directory is created) |
| `BRAIND_APPROVAL_KEY_FILE` | unset | HMAC key; when set, intents need a valid approval signature |
| `BRAIND_ALLOWED_ORIGINS` | unset | comma list of WS origins (default: any `chrome-extension://`) |
| `BRAIND_PAIRING_ENABLED` | unset (OFF) | pairing/session creation switch; exactly `1` = ON, anything else = OFF (fail-closed; existing sessions unaffected) |
| `BRAIND_POLICY_ENABLED` | unset (ON) | Brain policy gate switch; exactly `0` = OFF (agent autonomous, no per-tx owner approval), anything else = ON |
| `BRAIND_WALLETD_URL` | `http://127.0.0.1:18924/` | where brain.py / wd.py reach walletd |

## Security rules that are not optional

- The seed file exists on the signer only, mode `0600`, outside the repo.
- Treat the caller token like a password: it authenticates local callers to
  the daemon. Brain's intent gate exists so that token alone is never enough
  to move funds without an approved intent on gated paths.
- Approve intents on the signer side (or over your SSH session to it), and
  review the exact intent hash — approval is bound to it and consumed once.
- Start with testnets and a small burner wallet. This is experimental
  software; see the warning in the README.
