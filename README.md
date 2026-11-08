# Super Agentic BrainD Wallet

A local, agent-operated multi-chain wallet. An AI agent (or any program) proposes
transactions and signatures through an intent gate; a local daemon holds the keys
and does the signing. Keys never leave the daemon, and nothing is signed without
an approved intent.

## How it works

```
agent / CLI ──► Brain (intent gate) ──► walletd (signing daemon) ──► chain RPC
                     │                        │
                     │                        ├─ HTTP  127.0.0.1:18924
                     │                        └─ WS    127.0.0.1:18923
                     └─ propose → approve (exact hash, one-time) → consume → sign
```

- **`daemon/walletd.py`** — the signing daemon. Derives EVM, Solana, Sui and
  Cosmos accounts from a single BIP-39 seed stored locally (never in this repo),
  serves JSON-RPC over HTTP/WebSocket on localhost, and enforces a caller token
  on every POST.
- **`bin/brain.py`** — the Brain intent gate. Callers *propose* an intent
  (transaction or message signature); the intent must be *approved* against its
  exact hash and is *consumed* before signing, so a signature can never be
  replayed or swapped. Policy lives in a JSON policy file (see
  `config/brain_policy.example.json`): allowed methods, per-chain settings and a
  destination allowlist for sends.
- **`bin/wd.py`** — direct CLI for balances, sends, swaps and signing through
  the daemon, without a browser.
- **`bin/price_oracle.py`** — keyless USD prices from on-chain Chainlink feeds.
- **`extension/`** — a Chrome MV3 extension that injects an EIP-1193 provider
  (`window.ethereum`, plus Solana/Sui providers) backed by the daemon, so dApps
  detect the wallet like any browser wallet. The provider is injected on every
  site (`<all_urls>`); only the Elysium-specific provider is limited to an
  allowlist of hosts. Signing stays in the daemon and is gated by Brain.

## Chains

- EVM: Ethereum, Base, BSC, Polygon (+ Sepolia, Base Sepolia, BSC testnet,
  Amoy) and Elysium testnet
- Solana, Sui, Cosmos (one seed, BIP-44 derivation)

## Security model

- The seed exists in exactly one local file with `0600` permissions and is read
  only by the daemon. Brain and the CLI never read it and never sign.
- Every daemon POST requires the caller token (`X-Walletd-Token`); the daemon
  fails closed when the token file is missing.
- Message signing (`personal_sign`, `eth_signTypedData_v4`) goes through the
  same propose → approve → consume gate as transactions. There is no free
  signing path.
- Fees follow the chain's own gas price; when the chain suggestion is below the
  pending block's base fee, the daemon lifts it to exactly the base fee.

## Hardening (read before using real funds)

- **Value cap**: mainnet chains ship with `cap_wei` = 0.01 native token per
  transaction. It is enforced by `brain.py propose` **and** by the daemon
  (including scoped sessions, which also get an optional
  `pair.py approve --max-value-wei`). Raise it deliberately in
  `state/brain_policy.json`. Sessions are also limited to the policy's
  destination allowlist.
- **Approval signature (strongly recommended on mainnet)**: set
  `BRAIND_APPROVAL_KEY_FILE` (>= 32 random chars, mode 0600) for the signer
  and for whoever runs `brain.py approve` / the approver — **not** for the
  agent. Intents without a valid `approval_sig` are then refused, so an agent
  that can write to `state/` cannot forge or self-approve an intent.
- **The agent must not be able to write `state/`, `brain_policy.json`, the
  token file or the key file.** Run the signer as a separate OS user or on a
  separate machine (docs/self-host.md, Mode B). If the agent can edit the
  policy, the caps are decoration.
- **WebSocket/HTTP**: the WS server refuses web-page Origins (only
  `chrome-extension://` or origins in `BRAIND_ALLOWED_ORIGINS`; pin your
  extension id there). The HTTP API refuses any request with an `Origin`
  header and sends no CORS headers.
- Not covered: typed-data/message signatures can authorise token spending
  (e.g. permits) without moving native value, so do not give agents sessions
  with signing methods on mainnet; the code is still unaudited.

## Cloud agents (Meta Muse) — relay + puller

Cloud agents cannot reach the signer and cannot load the extension. `relay/`
adds a public proposal inbox (`relay_server.py`, holds no keys) and a
signer-side `puller.py` that fetches proposals over outbound HTTPS and feeds
them into Brain. The agent can only *propose*; you still approve. Setup,
the Muse connector prompt and the mainnet checklist:
[docs/muse-connector.md](docs/muse-connector.md).

## Status

Experimental software, built for testnet-first operation with a small burner
wallet. **Do not use it as your only copy of anything valuable, and do not point
it at funds you cannot afford to lose.** Audit the code before trusting it —
that is why it is open.

## Getting started

```bash
# 1. Create your own seed file OUTSIDE this repo, permissions 0600,
#    and point SEED_FILE in daemon/walletd.py at it.
# 2. Create a caller token file the same way.
# 3. Copy config/brain_policy.example.json, fill in your own addresses,
#    and ONLY then start the daemon:
python3 daemon/walletd.py
# 4. Talk to it through the gate:
python3 bin/wd.py balance --chain elysium
```

## Self-hosting

BrainD runs entirely on your own infrastructure — there is no central server.
Two ways to install on a fresh VM:

```bash
# Docker
mkdir -p braind-data/state
umask 077; printf '%s' "<your BIP-39 mnemonic>" > braind-data/seed
openssl rand -hex 32 > braind-data/walletd.token
docker compose up -d

# Native (Python 3.11+), with an interactive seed setup
./install.sh            # add --systemd for a service
```

For anything beyond a single test machine, run the **signer on its own VM**
and let agents propose intents to it over SSH or a private network — the seed
then never touches the agent machine. Topology, configuration reference
(everything is environment-driven: `BRAIND_SEED_FILE`, `BRAIND_TOKEN_FILE`,
`BRAIND_STATE_DIR`, `BRAIND_BIND`, ports) and the security rules live in
[docs/self-host.md](docs/self-host.md).

Running inside the Muse VM (watchdog-supervised, no systemd, pairing and
policy on/off switches): [docs/deploy-vm-muse.md](docs/deploy-vm-muse.md).

## Pairing & scoped sessions (Fase 2)

Agents don't need the master token. Following the Binance Agentic Wallet
pattern, an agent **pairs** with the signer: it requests a pairing, the owner
approves it (CLI `bin/pair.py` or the web approver `bin/approver.py`), and the
agent receives a **scoped session token** — restricted to specific chains,
methods, and an expiry, revocable at any time. The web approver also handles
Brain intent approvals from the browser. Full guide:
[docs/pairing-and-sessions.md](docs/pairing-and-sessions.md).

## License

MIT — see [LICENSE](LICENSE).
