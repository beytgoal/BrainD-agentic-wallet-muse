/* test-elysium-provider.mjs — tests for the bridge's Elysium "Browser Wallet"
 * provider (extension/elysium_provider.js).
 *
 * Loads the provider in a fake page world (window/location/Event stubs) and
 * drives it through a mocked __saw relay. No network, no keys, no daemon.
 *
 * Run: npm test   (from this folder)
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PROVIDER_SRC = fs.readFileSync(path.join(__dirname, '..', 'elysium_provider.js'), 'utf8');

const TEST_ADDRESS = '0x0000000000000000000000000000000000000000'; // operational wallet address (public, from connector verification)
const ALLOW_HOST = 'elysiumeco.xyz';
const EVIL_HOST = 'evil.example';

function makeWorld({ hostname = ALLOW_HOST, existingEthereum = undefined } = {}) {
  const listeners = {};
  const posted = []; // messages the provider posts to the relay
  const dispatched = [];
  const window = {
    ethereum: existingEthereum,
    browserWallet: undefined,
    postMessage(msg) { posted.push(msg); },
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    dispatchEvent(ev) { dispatched.push(ev.type || ev); return true; },
  };
  // The provider reads bare `window` and `location` globals.
  globalThis.window = window;
  globalThis.location = { hostname, origin: 'https://' + hostname };
  // (Re)load the provider source fresh for each world.
  eval(PROVIDER_SRC); // eslint-disable-line no-eval
  return {
    window, posted, dispatched,
    provider: window.ethereum || window.browserWallet,
    // Simulate the relay answering a request.
    answer(id, result) {
      const ev = { source: window, data: { __saw: 'res', id, result } };
      (listeners['message'] || []).slice().forEach((fn) => fn(ev));
    },
    answerError(id, code, message) {
      const ev = { source: window, data: { __saw: 'res', id, error: { code, message } } };
      (listeners['message'] || []).slice().forEach((fn) => fn(ev));
    },
    cleanup() { delete globalThis.window; delete globalThis.location; },
  };
}

async function expectReject(promise, code, label) {
  let err = null;
  try { await promise; } catch (e) { err = e; }
  assert.ok(err, label + ': expected rejection');
  assert.equal(err.code, code, label + ': expected code ' + code + ', got ' + (err && err.code));
  console.log(label, '-> rejected code', err.code, 'OK');
}

function requestAndAnswer(world, args, result) {
  const p = world.provider.request(args);
  assert.equal(world.posted.length, 1, 'expected exactly one relayed request');
  const req = world.posted[0];
  world.answer(req.id, result);
  return p;
}

// 1. Install on an allowlisted origin: window.ethereum, flags, chainId.
{
  const world = makeWorld();
  const eth = world.window.ethereum;
  assert.ok(eth, 'window.ethereum should be set');
  assert.equal(eth.isBrowserWallet, true);
  assert.equal(eth.isMetaMask, false);
  assert.equal(eth.chainId, '0x185d9');
  assert.equal(eth.networkVersion, '99801');
  assert.ok(world.dispatched.includes('ethereum#initialized'), 'ethereum#initialized dispatched');
  console.log('install (allowlisted) -> window.ethereum isBrowserWallet, chainId 0x185d9 OK');
  world.cleanup();
}

// 2. REGRESSION: chainId must be 0x185d9 (= 99801), NOT the division's 0x185f1 (= 99825).
{
  const world = makeWorld();
  assert.equal(parseInt(world.window.ethereum.chainId, 16), 99801);
  assert.notEqual(world.window.ethereum.chainId, '0x185f1');
  console.log('chainId regression -> 0x185d9 = 99801, not 0x185f1 OK');
  world.cleanup();
}

// 3. Evil origin: provider does not install.
{
  const world = makeWorld({ hostname: EVIL_HOST });
  assert.equal(world.window.ethereum, undefined);
  assert.equal(world.window.browserWallet, undefined);
  assert.deepEqual(world.dispatched, []);
  console.log('install (evil origin) -> no provider installed OK');
  world.cleanup();
}

// 4. Existing non-SuperAgentic wallet: ours becomes window.browserWallet, no clobber.
{
  const existing = { isMetaMask: true };
  const world = makeWorld({ existingEthereum: existing });
  assert.equal(world.window.ethereum, existing);
  assert.ok(world.window.browserWallet, 'window.browserWallet should be set');
  assert.equal(world.window.browserWallet.isBrowserWallet, true);
  console.log('install (wallet already present) -> window.browserWallet, no clobber OK');
  world.cleanup();
}

// 5. eth_chainId / net_version are answered locally, no relay traffic.
{
  const world = makeWorld();
  assert.equal(await world.provider.request({ method: 'eth_chainId' }), '0x185d9');
  assert.equal(await world.provider.request({ method: 'net_version' }), '99801');
  assert.equal(world.posted.length, 0, 'local answers must not touch the relay');
  console.log('eth_chainId / net_version -> local answers OK');
  world.cleanup();
}

// 6. eth_requestAccounts forwards with the chain hint, syncs selectedAddress,
//    and emits accountsChanged.
{
  const world = makeWorld();
  let changed = null;
  world.provider.on('accountsChanged', (addrs) => { changed = addrs; });
  const p = world.provider.request({ method: 'eth_requestAccounts' });
  const req = world.posted[0];
  assert.equal(req.chain, '0x185d9');
  assert.equal(req.__saw, 'req');
  world.answer(req.id, [TEST_ADDRESS]);
  assert.deepEqual(await p, [TEST_ADDRESS]);
  assert.equal(world.provider.selectedAddress, TEST_ADDRESS);
  assert.deepEqual(changed, [TEST_ADDRESS]);
  console.log('eth_requestAccounts -> forwarded with chain 0x185d9, accountsChanged OK');
  world.cleanup();
}

// 7. eth_sendTransaction 0.01 HYPE (<= 0.05 cap) is forwarded with the chain hint.
{
  const world = makeWorld();
  const hash = await requestAndAnswer(world, {
    method: 'eth_sendTransaction',
    params: [{ from: TEST_ADDRESS, to: '0x0000000000000000000000000000000000000001', value: '0x2386f26fc10000' }],
  }, '0x' + 'ab'.repeat(32));
  assert.equal(hash, '0x' + 'ab'.repeat(32));
  assert.equal(world.posted[0].chain, '0x185d9');
  console.log('eth_sendTransaction 0.01 HYPE -> forwarded OK');
  world.cleanup();
}

// 8. eth_sendTransaction 0.06 HYPE (> 0.05 cap) is refused (4001), relay untouched.
{
  const world = makeWorld();
  await expectReject(
    world.provider.request({
      method: 'eth_sendTransaction',
      params: [{ from: TEST_ADDRESS, to: '0x0000000000000000000000000000000000000001', value: '0xd529ae9e860000' }],
    }),
    4001, 'eth_sendTransaction 0.06 HYPE (over cap)'
  );
  assert.equal(world.posted.length, 0, 'over-cap tx must not reach the relay');
  world.cleanup();
}

// 9. eth_sendTransaction naming another chain is refused (4902).
{
  const world = makeWorld();
  await expectReject(
    world.provider.request({
      method: 'eth_sendTransaction',
      params: [{ from: TEST_ADDRESS, to: '0x0000000000000000000000000000000000000001', value: '0x0', chainId: '0x1' }],
    }),
    4902, 'eth_sendTransaction chainId 0x1'
  );
  assert.equal(world.posted.length, 0);
  world.cleanup();
}

// 10. Contract deployment (no `to`) is refused (-32602).
{
  const world = makeWorld();
  await expectReject(
    world.provider.request({
      method: 'eth_sendTransaction',
      params: [{ from: TEST_ADDRESS, value: '0x0', data: '0x6000' }],
    }),
    -32602, 'eth_sendTransaction deployment'
  );
  assert.equal(world.posted.length, 0);
  world.cleanup();
}

// 11. wallet_switchEthereumChain: 0x185d9 forwards, anything else -> 4902.
{
  const world = makeWorld();
  const res = await requestAndAnswer(world, {
    method: 'wallet_switchEthereumChain', params: [{ chainId: '0x185d9' }],
  }, null);
  assert.equal(res, null);
  console.log('wallet_switchEthereumChain 0x185d9 -> forwarded OK');
  world.cleanup();
}
{
  const world = makeWorld();
  await expectReject(
    world.provider.request({
      method: 'wallet_switchEthereumChain', params: [{ chainId: '0x1' }],
    }),
    4902, 'wallet_switchEthereumChain 0x1'
  );
  assert.equal(world.posted.length, 0);
  world.cleanup();
}

// 12. wallet_addEthereumChain: 99801 (int) forwards, 0x1 -> 4902.
{
  const world = makeWorld();
  const res = await requestAndAnswer(world, {
    method: 'wallet_addEthereumChain', params: [{ chainId: 99801 }],
  }, null);
  assert.equal(res, null);
  console.log('wallet_addEthereumChain 99801 -> forwarded OK');
  world.cleanup();
}
{
  const world = makeWorld();
  await expectReject(
    world.provider.request({
      method: 'wallet_addEthereumChain', params: [{ chainId: '0x1' }],
    }),
    4902, 'wallet_addEthereumChain 0x1'
  );
  assert.equal(world.posted.length, 0);
  world.cleanup();
}

// 13. eth_signTypedData_v4 is forwarded with the chain hint.
{
  const world = makeWorld();
  const sig = await requestAndAnswer(world, {
    method: 'eth_signTypedData_v4',
    params: [TEST_ADDRESS, { domain: { name: 'Elysium', chainId: 99801 }, types: {}, message: {} }],
  }, '0x' + 'cd'.repeat(65));
  assert.equal(sig, '0x' + 'cd'.repeat(65));
  assert.equal(world.posted[0].chain, '0x185d9');
  console.log('eth_signTypedData_v4 -> forwarded OK');
  world.cleanup();
}

// 14. eth_sign / eth_signTransaction are refused (4200), relay untouched.
{
  const world = makeWorld();
  await expectReject(
    world.provider.request({ method: 'eth_sign', params: [TEST_ADDRESS, '0x1234'] }),
    4200, 'eth_sign'
  );
  await expectReject(
    world.provider.request({ method: 'eth_signTransaction', params: [{}] }),
    4200, 'eth_signTransaction'
  );
  assert.equal(world.posted.length, 0);
  world.cleanup();
}

// 15. A walletd error code survives the relay (provider preserves err.code).
{
  const world = makeWorld();
  const p = world.provider.request({ method: 'eth_requestAccounts' });
  const req = world.posted[0];
  world.answerError(req.id, 4902, 'chain not supported by this wallet');
  await expectReject(p, 4902, 'relay error code preserved');
  world.cleanup();
}

// 16. Legacy sendAsync still works.
{
  const world = makeWorld();
  const done = new Promise((resolve, reject) => {
    world.provider.sendAsync({ method: 'eth_requestAccounts', id: 1 }, (err, res) => {
      if (err) reject(err);
      else resolve(res);
    });
  });
  const req = world.posted[0];
  world.answer(req.id, [TEST_ADDRESS]);
  const res = await done;
  assert.deepEqual(res.result, [TEST_ADDRESS]);
  console.log('sendAsync -> OK');
  world.cleanup();
}

console.log('test-elysium-provider: ALL PASS');
