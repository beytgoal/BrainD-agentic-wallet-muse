# Elysium "Browser Wallet" integration (2026-10-25)

The Airdrop & NFT division's browser-wallet connector
(`custom.browser-wallet`: EIP-1193 injector, allowlist, 0.05 HYPE cap, low
gas) is integrated into this bridge extension. Signing stays in walletd —
no key is stored in the extension and no local signing happens here.

## How it works

- `inject.js` routes by hostname: on the 5 Elysium allowlisted origins
  (`elysiumeco.xyz`, `www.elysiumeco.xyz`, `app.oxley.fun`, `oxley.fun`,
  `dex-da-costa-elysium.vercel.app`) it injects `elysium_provider.js`
  instead of `provider.js`. Every other origin is unchanged (BSC provider
  + Solana + multichain).
- `elysium_provider.js` sets `window.ethereum` with `isBrowserWallet: true`,
  `isMetaMask: false`, `chainId: '0x185d9'` (99801), dispatches
  `ethereum#initialized` at `document_start`. If another wallet already
  claimed `window.ethereum`, ours is exposed as `window.browserWallet`.
- Every relayed request carries a `chain: '0x185d9'` hint (`inject.js` /
  `background.js` forward it). walletd routes reads to the Elysium RPC and
  answers `eth_chainId` / `net_version` from the hint. Requests without a
  hint behave exactly as before (default chain 0x38).
- walletd additions (`daemon/walletd.py`, WS + HTTP bridge):
  `wallet_switchEthereumChain`, `wallet_addEthereumChain`,
  `eth_signTypedData_v4`; per-chain value cap (Elysium 0.05 HYPE, others
  keep 0.5 native); Elysium gas clamped to 0.1–0.2 gwei (owner rule: lowest
  gas for farming — never the aggressive 2.2x fee); EIP-1193 error codes
  (4902/4001) survive the relay.
- Guardrails are enforced twice: in `elysium_provider.js` (user-facing,
  rejects before the relay) and in walletd (backstop).

## chainId bug in the division's copy (NOT fixed here — division must fix it)

The division's `inpage.js` / `rpc.js` write `chainId: '0x185f1'` with the
comment "(99801)". That is wrong: `0x185f1` = **99825**, not 99801. The
correct hex for 99801 is **`0x185d9`** (as in walletd's chain map). The
integrated provider uses `0x185d9`; the division's own connector files
were left untouched and their tests even assert the wrong value.

## Tests

- `extension/tests/` — `npm test` (no dependencies, mocked relay):
  `test-elysium-provider.mjs` (install, chainId regression, allowlist,
  cap, switch/add, typed-data, error codes) and `test-inject-routing.mjs`
  (hostname routing, chain-hint relay).
- `daemon/test_walletd_elysium.py` — `~/workspace/.wallet-venv/bin/python
  test_walletd_elysium.py` (throwaway in-memory account, fake RPC):
  chain hint, new methods, per-chain cap, gas clamp both directions
  (5 gwei → 0.2, 0.05 gwei → 0.1), signed-tx chain check via `v`.

## Load in Chrome desktop + verify detection (user step)

The agent cannot install extensions into its own browser from this VM, so
this is a manual step in desktop Chrome:

1. `chrome://extensions` → turn on **Developer mode** → **Load unpacked** →
   select `~/workspace/superagentic-wallet/extension/`.
   (Reload the extension here after every update to this folder.)
2. Make sure walletd is running (it is under the watchdog now; check
   `curl http://127.0.0.1:18924/` returns 501 from the VM).
3. Open https://elysiumeco.xyz/me/ — it must show the wallet instead of
   "No wallet in this browser". Click **Connect → Browser Wallet** and sign
   the free `personal_sign` login (no transaction, no funds move).
4. In DevTools console on that page, verify:
   `window.ethereum.isBrowserWallet === true`,
   `window.ethereum.chainId === '0x185d9'`,
   `await window.ethereum.request({method:'eth_chainId'}) === '0x185d9'`.
5. Quest transactions are signed by walletd within the 0.05 HYPE cap at
   0.1–0.2 gwei. Claim XP the same day, before 00:00 UTC.

## Known limits

- The production walletd only picks up new `walletd.py` code when the
  watchdog restarts it (next runtime restart / crash). Until then the old
  code serves requests and the new methods answer "unsupported method".
- `eth_chainId` / `net_version` are answered locally by the provider, so
  detection works even while the daemon is briefly down; signing and reads
  need the daemon up.
- Pre-existing (not fixed here): the HTTP bridge's `eth_sendTransaction`
  broadcasts `signed.raw_transaction.hex()` without the `0x` prefix (the WS
  path has the prefix). HTTP sends are therefore broken; use the WS path.
