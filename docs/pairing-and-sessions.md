# Pairing & scoped sessions (Fase 2)

The master caller token is the owner's root credential: whoever holds it
controls the wallet. Agents should never hold it. Fase 2 adds the Binance
Agentic Wallet pattern on top: **pairing** issues an agent a **scoped
session token** instead.

## The flow

```text
agent                          signer (walletd)
  |  braind_pairBegin            |
  | ---------------------------> |  creates a pending pairing
  |  pairing_id + secret (+ QR)  |  (10-minute expiry, max 3 pending)
  |                              |
  |        owner approves        |  pair.py approve  /  web approver
  |                              |  -> session created (chains, methods,
  |                              |     expiry chosen by the owner)
  |  braind_pairPoll             |
  | ---------------------------> |  returns the session token ONCE
```

CLI (agent side and owner side are the same tool, `bin/pair.py`):

```bash
# agent: request a pairing, proposing a scope
python3 bin/pair.py begin --label my-agent --chains 11155111,99801

# owner (on the signer host, master token): approve it
python3 bin/pair.py approve --pairing-id brd_xxxxxxxx --ttl-hours 48

# agent: collect the session token (saved 0600)
python3 bin/pair.py poll --pairing-id brd_xxxxxxxx --secret <secret>

# agent: use the session from here on — never the master token
BRAIND_TOKEN_FILE=state/session.token python3 bin/wd.py balance sepolia

# owner: inspect / revoke at any time
python3 bin/pair.py list
python3 bin/pair.py revoke --session-id sess_xxxxxxxx
```

Chain ids in `--chains` are numeric chain ids (e.g. `11155111` Sepolia,
`99801` Elysium); `*` allows every chain the signer supports.

## What a session can and cannot do

Enforced by the signer on every HTTP request, before any other gate:

- **Methods** — read methods (`eth_call`, balances, `eth_chainId`, …) are
  always allowed. Signing methods must be in the session's method list
  (default: `eth_sendTransaction`, `personal_sign`,
  `eth_signTypedData_v4`).
- **Chains** — a signing request naming a chain outside the session's
  chain list is rejected with `-32003 session scope denied`.
- **Expiry & revocation** — expired or revoked sessions get `401`, exactly
  like a bad token. Management methods (`braind_pairApprove`,
  `braind_listSessions`, `braind_revokeSession`, …) reject session tokens.
- Only SHA-256 hashes of session tokens and pairing secrets are stored
  (`<state>/sessions.json`).

**Trust model, stated plainly:** the session scope *is* the limit. A
session whose methods include `eth_sendTransaction` can send transactions
on its chains without a per-transaction Brain intent — that is the point
of a session (an autonomous agent inside a fence). If you want
per-transaction owner approval instead, do not issue a session: let the
agent work through Brain intents (`bin/brain.py propose`) with the master
token held only on the signer side. Fase 3 adds optional per-session value
caps and pre-signing simulation.

Pairing endpoints are authenticated by the pairing secret itself
(`braind_pairBegin` / `braind_pairPoll` need no wallet token), which is
why a pairing is worthless until the owner approves it, expires in 10
minutes, and hands out its token exactly once. Sessions apply to the HTTP
API; the WebSocket path keeps its existing Brain gating.

## Web approver

`bin/approver.py` serves one page (default `http://127.0.0.1:18926`):

- pending **pairings** — approve with editable chain scope and TTL;
- **sessions** — list and revoke;
- pending **Brain intents** — approve/reject with the exact intent hash
  (executed through `bin/brain.py`, never by editing state directly).

Login is the master token (a SHA-256 cookie is set; the token is never
stored server-side). Keep the bind on localhost and reach it through SSH
port-forwarding or your private network — the same rule as the signer
ports. With Docker: `docker compose --profile approver up -d`.
