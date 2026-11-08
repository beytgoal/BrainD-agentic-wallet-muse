# Connecting a cloud agent (Meta Muse) through the relay

A cloud agent such as Muse runs on the vendor's servers. It cannot reach your
signer (which listens on `127.0.0.1` and must never be exposed), and its
browser cannot load the Chrome extension. So the agent never talks to the
wallet directly. It talks to a **relay**; your signer **pulls** from the relay.

```
Muse (cloud) ──HTTPS+agent key──► relay (public VPS, no keys)
                                      ▲
                                      │ outbound HTTPS only, puller key
signer machine:  puller.py ───────────┘
      └─► brain.py propose ─► policy (cap, allowlist) ─► queued
                              YOU: brain.py approve (exact hash)
      └─► (optional --auto-deliver) brain.py deliver ─► walletd signs
```

What a hijacked or prompt-injected agent can do: **propose** transactions
(limited by relay + policy caps) and read their status. It cannot sign,
approve, read keys, sign messages/typed data, or reach the signer.

What this is **not**: it does not make a wallet appear inside dApps opened by
Muse's browser (`window.ethereum` stays absent). Muse sends instructions
through the connector; it does not click "Connect Wallet".

Step-by-step VPS deployment with systemd and Caddy: [deploy-relay.md](deploy-relay.md).

## 1. Run the relay (on a separate machine from the signer)

```bash
# keys: two DIFFERENT random secrets, >= 32 chars
python3 -c "import secrets;print(secrets.token_urlsafe(32))"   # agent key  -> give to Muse
python3 -c "import secrets;print(secrets.token_urlsafe(32))"   # puller key -> signer only

export RELAY_AGENT_KEY=...  RELAY_PULLER_KEY=...
export RELAY_CHAINS=sepolia,base-sepolia      # add mainnets deliberately, e.g. ,base
export RELAY_MAX_VALUE_ETHER=0.01
export RELAY_PUBLIC_URL=https://relay.example.com
python3 relay/relay_server.py                  # listens on 127.0.0.1:18930
```

Put HTTPS in front (Caddy example, automatic certificates):

```
relay.example.com {
    reverse_proxy 127.0.0.1:18930
}
```

Optional env: `RELAY_RATE_PER_MIN=10`, `RELAY_MAX_PENDING=20`,
`RELAY_TTL_SECONDS=3600` (unapproved proposals expire), `RELAY_DB`.
Never run the relay in the same container/volume as the signer's seed.

## 2. Run the puller (on the signer machine)

```bash
umask 077; printf '%s' '<puller key>' > ~/.braind/relay_puller.key
export RELAY_URL=https://relay.example.com
export RELAY_PULLER_KEY_FILE=~/.braind/relay_puller.key
# same env the signer uses: BRAIND_STATE_DIR, BRAIND_TOKEN_FILE, BRAIND_APPROVAL_KEY_FILE ...
python3 relay/puller.py                 # propose only; YOU approve and deliver
python3 relay/puller.py --auto-deliver  # after YOU approve, execute automatically
```

The puller refuses a non-HTTPS relay URL, re-validates every proposal, and
never approves anything. With `BRAIND_APPROVAL_KEY_FILE` set (recommended),
only a holder of that key can produce an approval the signer will accept.

Approve on the signer as usual:

```bash
python3 bin/brain.py list
python3 bin/brain.py approve --id <intent_id> --hash <intent_hash> --by me
```

## 3. Add it to Muse

Ask Muse to create a custom connector (Meta's connector builder; see
platform.muse.ai/docs for the current rules). Example prompt:

> Create a custom connector named "BrainD wallet". Its OpenAPI document is
> https://relay.example.com/openapi.json . Auth: API key sent as
> `Authorization: Bearer <key>` (the relay also accepts `X-API-Key`).
> Use `proposeTransaction` to propose a transaction and `getProposal` to check
> its status. Never claim a transaction was sent until the status is `executed`.

Paste the **agent key** into Muse's secure credential prompt. Do not give Muse
the puller key. Keep Muse's own approval prompts on: Meta's guidelines default
sensitive writes to asking the user first, and proposals are a sensitive write.
I could not verify Muse's connector-builder behaviour against a live account;
if the OpenAPI import fails, ask Muse to build the connector from the
endpoint list below instead.

## API

| Call | Key | Purpose |
|---|---|---|
| `POST /v1/proposals` `{chain,to,value,data,reason}` | agent | propose (value = ether decimal string) |
| `GET /v1/proposals`, `GET /v1/proposals/{id}` | agent | status: pending, claimed, queued, approved, executed, rejected, failed, expired |
| `GET /openapi.json`, `GET /healthz` | none | schema / liveness |
| `POST /v1/puller/claim`, `GET /v1/puller/active`, `POST /v1/puller/proposals/{id}/status` | puller | signer-side only |

Rejected at the relay: any method other than `eth_sendTransaction`, chains not
in `RELAY_CHAINS`, value above `RELAY_MAX_VALUE_ETHER`, malformed addresses,
oversized calldata, control characters, unknown fields. Brain's policy then
applies its own cap and destination allowlist.

## Mainnet checklist

- Separate signer machine; the agent/relay hosts never see the seed or tokens.
- `BRAIND_APPROVAL_KEY_FILE` set; policy and state dir not writable by anything the agent controls.
- Policy `allowlist` contains only addresses you want the agent to pay; caps stay low.
- Relay `RELAY_CHAINS` lists only chains you intend; start on testnet.
- Review every intent hash and recipient before approving. The `reason` text
  comes from the agent: treat it as untrusted.
- Rotate the agent key if it may have leaked (restart the relay with a new one).
