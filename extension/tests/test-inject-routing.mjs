/* test-inject-routing.mjs — tests for extension/inject.js hostname routing.
 *
 * Verifies: Elysium allowlisted origins get elysium_provider.js (not
 * provider.js); every other origin gets provider.js (unchanged behaviour);
 * the page->background relay forwards the chain hint.
 *
 * Loads inject.js in a fake content-script world (chrome/document/window
 * stubs). No network, no daemon.
 *
 * Run: npm test   (from this folder)
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const INJECT_SRC = fs.readFileSync(path.join(__dirname, '..', 'inject.js'), 'utf8');

function makeWorld(hostname) {
  const injected = []; // script srcs appended to the page
  const sentToBackground = [];
  const pageListeners = {};
  const bgListeners = [];
  const window = {
    postMessage(msg) { sentToBackground.push(['page->window', msg]); },
    addEventListener(type, fn) { (pageListeners[type] = pageListeners[type] || []).push(fn); },
  };
  const document = {
    head: {
      appendChild(el) { injected.push(el.src); if (el.onload) el.onload.call(el); },
    },
    documentElement: null,
    createElement(tag) {
      if (tag !== 'script') throw new Error('unexpected element: ' + tag);
      return { src: '', onload: null, remove() {} };
    },
  };
  globalThis.window = window;
  globalThis.document = document;
  globalThis.location = { hostname };
  globalThis.chrome = {
    runtime: {
      getURL(f) { return 'chrome-extension://fake/' + f; },
      sendMessage(msg) { sentToBackground.push(['content->background', msg]); },
      onMessage: { addListener(fn) { bgListeners.push(fn); } },
    },
  };
  eval(INJECT_SRC); // eslint-disable-line no-eval
  return {
    injected, sentToBackground,
    // Simulate a page message event (provider -> relay).
    pageMessage(data) {
      const ev = { source: window, data };
      (pageListeners['message'] || []).slice().forEach((fn) => fn(ev));
    },
    // Simulate a background message (background -> page).
    backgroundMessage(msg) {
      bgListeners.slice().forEach((fn) => fn(msg));
    },
    cleanup() {
      delete globalThis.window;
      delete globalThis.document;
      delete globalThis.location;
      delete globalThis.chrome;
    },
  };
}

// 1. Elysium origin injects the Browser Wallet provider, not the BSC one.
for (const host of ['elysiumeco.xyz', 'www.elysiumeco.xyz', 'app.oxley.fun', 'oxley.fun', 'dex-da-costa-elysium.vercel.app']) {
  const world = makeWorld(host);
  assert.ok(world.injected.some((s) => s.endsWith('elysium_provider.js')), host + ': elysium_provider.js injected');
  assert.ok(!world.injected.some((s) => s.endsWith('/provider.js')), host + ': provider.js must NOT be injected');
  assert.ok(world.injected.some((s) => s.endsWith('solana_provider.js')), host + ': solana provider still injected');
  assert.ok(world.injected.some((s) => s.endsWith('multichain_provider.js')), host + ': multichain provider still injected');
  console.log('inject (' + host + ') -> elysium_provider.js OK');
  world.cleanup();
}

// 2. Any other origin keeps the existing multichain behaviour.
for (const host of ['app.uniswap.org', 'pancakeswap.finance', 'example.com']) {
  const world = makeWorld(host);
  assert.ok(world.injected.some((s) => s.endsWith('/provider.js')), host + ': provider.js injected');
  assert.ok(!world.injected.some((s) => s.endsWith('elysium_provider.js')), host + ': elysium_provider.js must NOT be injected');
  console.log('inject (' + host + ') -> provider.js (unchanged) OK');
  world.cleanup();
}

// 3. The relay forwards the chain hint to the background.
{
  const world = makeWorld('elysiumeco.xyz');
  world.pageMessage({ __saw: 'req', id: 7, method: 'eth_chainId', params: [], chain: '0x185d9' });
  const relayed = world.sentToBackground.find(([dir]) => dir === 'content->background');
  assert.ok(relayed, 'message relayed to background');
  assert.equal(relayed[1].chain, '0x185d9');
  assert.equal(relayed[1].method, 'eth_chainId');
  console.log('relay page->background -> chain hint forwarded OK');
  world.cleanup();
}

// 4. The relay forwards daemon responses back to the page.
{
  const world = makeWorld('elysiumeco.xyz');
  world.backgroundMessage({ __saw: 'res', id: 7, result: '0x185d9' });
  const toPage = world.sentToBackground.find(([dir, msg]) => dir === 'page->window' && msg.__saw === 'res');
  assert.ok(toPage, 'response relayed to page');
  assert.equal(toPage[1].result, '0x185d9');
  console.log('relay background->page -> response forwarded OK');
  world.cleanup();
}

console.log('test-inject-routing: ALL PASS');
