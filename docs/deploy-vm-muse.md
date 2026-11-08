# Deploying on the Muse VM (watchdog-supervised)

This is the mode used on the owner's Muse VM, where the agent and the
signer live on the **same machine**. It differs from the VPS topologies in
[deploy-relay.md](deploy-relay.md) / [self-host.md](self-host.md):

- The daemon runs under the **existing Hermes watchdog**, not systemd.
  The watchdog starts `walletd.py` with a fixed environment; to change
  any `BRAIND_*` variable below, change it in the watchdog's launch
  environment and restart the daemon.
- The **relay is NOT installed on this VM**. The relay belongs on a
  separate public VPS (see deploy-relay.md); a public proposal inbox on
  the same machine as the seed would defeat its purpose. If a relay is
  ever used, only the signer-side puller would run here.

## Environment parity with the current daemon

The defaults of this build are repo-oriented. On this VM the following
variables MUST be pinned, or the daemon will look in the wrong places:

| Variable | Value on this VM | Why |
|---|---|---|
| `BRAIND_STATE_DIR` | `~/workspace/custom-connectors/custom.walletd/state` | The repo default is `<tree>/state`. Brain's intents, policy and audit live in the path on the left; with the default the gate would see no intents and deny everything (fail-closed, but broken). |
| `BRAIND_LOG_FILE` | `~/workspace/superagentic-wallet/daemon/walletd.log` | This build's default log path is `~/.braind/walletd.log`; the VM's tooling reads the path on the left. |
| `BRAIND_SEED_FILE` | *(default)* `~/.burner/seed` | Already matches. |
| `BRAIND_TOKEN_FILE` | *(default)* `~/.burner/walletd.token` | Already matches. |
| `BRAIND_BIND` / ports | *(default)* `127.0.0.1`, WS 18923, HTTP 18924 | Unchanged; the forwarder (18888) and Brain tooling expect these. |

## Mode pairing (on/off)

Pairing (Fase 2 scoped sessions) is controlled by `BRAIND_PAIRING_ENABLED`:

- Exactly `1` = **ON**: `braind_pairBegin` / `braind_pairPoll` /
  `braind_pairApprove` work as documented in pairing-and-sessions.md.
- Unset or anything else = **OFF** (default, fail-closed): begin/poll are
  refused with JSON-RPC error **-32007 `pairing disabled`**, and
  pairApprove — the only method that mints a session — is refused too.
  No pairing or session is created.
- The switch closes **creation only**: sessions that already exist keep
  working while OFF, revocation still works, and the master token is
  unaffected.

Ashila flips this switch on the owner's word ("pairing on" / "pairing
off") by editing the watchdog launch environment and restarting the
daemon. At startup the daemon logs one explicit line: `PAIRING ENABLED`
or `PAIRING DISABLED`.

## Mode policy (on/off)

The Brain policy gate is controlled by `BRAIND_POLICY_ENABLED`:

- Unset or `1` = **ON** (default): owner-gated, exactly the long-standing
  behaviour — WS signing needs an approved Brain intent, approval
  signatures are verified when configured, `cap_wei` and the destination
  allowlist are enforced by the daemon, and session value/destination
  scope applies.
- Exactly `0` = **OFF**: agent-autonomous mode. The agent decides and
  executes itself: WS transactions and message signing proceed **without
  a Brain intent**, approval signatures are not demanded, caps/allowlist
  and session value/destination scope are skipped (session chain/method
  scope still applies).

Never skipped in either mode:

- caller-token authentication and session token validity / revocation;
- the `BLOCKED` address denylist;
- foreign-chain rejection (4902);
- the **Fase 3 pre-signing simulation** — it is an anti-revert
  pre-flight, not part of the approval policy. A transaction that would
  revert is still refused with **-32006** while policy is OFF.

At startup the daemon logs `POLICY MODE: ON (owner-gated)` or
`POLICY MODE: OFF (agent autonomous — approvals NOT required)`. While
OFF, every signing that proceeds without an intent is logged with a
`POLICY-OFF sign:` marker.

## Approval signature & simulation

- **Approval signature** (`BRAIND_APPROVAL_KEY_FILE`) stays **opt-in**:
  with the variable unset, behaviour is the legacy one (intents need no
  HMAC signature). Enable it deliberately, as a separate step.
- **Simulation** (Fase 3) is always on in both policy modes, on the WS
  and HTTP paths: the exact transaction is executed once via `eth_call`
  before any intent is consumed or anything is signed; outcomes are
  recorded per intent in `state/simulations.jsonl` and the Brain audit
  log. `personal_sign` / `eth_signTypedData_v4` are not simulated
  (off-chain payloads — nothing for `eth_call` to execute).
