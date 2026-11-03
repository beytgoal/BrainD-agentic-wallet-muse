// test-background-relay.mjs — regression test for the service worker relay.
//
// Bug (2026-10-25): background.js re-numbered each page request with its own
// nextId before forwarding to walletd, but echoed THAT id back to the page.
// The page-side provider keys its pending map by ITS OWN id (elysium provider
// starts at 1000001), so the response never matched and every relayed request
// timed out after 60s — even though walletd answered in ~18ms.
// Fix: the worker keeps {tab, origId} per worker-id and sends the ORIGINAL
// provider id back to the tab.
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert';

const src = fs.readFileSync(new URL('../background.js', import.meta.url), 'utf8');

// ---- mocks ----
const sentToTab = [];
const sentToDaemon = [];
let onMessageHandler = null;
let wsInstance = null;

class MockWebSocket {
  static OPEN = 1;
  constructor(url) { this.url = url; this.readyState = 1; wsInstance = this; }
  send(data) { sentToDaemon.push(JSON.parse(data)); }
}
const chromeMock = {
  runtime: {
    onMessage: { addListener(fn) { onMessageHandler = fn; } },
  },
  tabs: { sendMessage(tabId, msg) { sentToTab.push({ tabId, msg }); } },
};

const sandbox = {
  WebSocket: MockWebSocket,
  chrome: chromeMock,
  console, setTimeout, clearTimeout,
  self: {},
};
vm.createContext(sandbox);
vm.runInContext(src, sandbox);

// the script calls connect() at load -> a daemon WebSocket is opened
assert.ok(wsInstance, 'background should open a daemon WebSocket on load');
assert.strictEqual(wsInstance.url, 'ws://127.0.0.1:18923');

// ---- request 1: Elysium provider id 1000002, tab 77 ----
onMessageHandler(
  { __saw: 'req', id: 1000002, method: 'eth_requestAccounts', params: [], chain: '0x185d9' },
  { tab: { id: 77 } },
  () => {}
);
assert.strictEqual(sentToDaemon.length, 1, 'one message forwarded to walletd');
assert.strictEqual(sentToDaemon[0].id, 1, 'daemon gets the worker-assigned id');
assert.strictEqual(sentToDaemon[0].method, 'eth_requestAccounts');
assert.strictEqual(sentToDaemon[0].chain, '0x185d9');

// walletd echoes worker id 1 -> the tab must get the ORIGINAL provider id back
wsInstance.onmessage({ data: JSON.stringify({ id: 1, result: ['0x0000000000000000000000000000000000000000'] }) });
assert.strictEqual(sentToTab.length, 1, 'one response sent to the tab');
assert.strictEqual(sentToTab[0].tabId, 77);
assert.strictEqual(sentToTab[0].msg.__saw, 'res');
assert.strictEqual(sentToTab[0].msg.id, 1000002, 'page gets its ORIGINAL request id back');
// (compare via JSON: the result array is built inside the vm realm, whose
// Array prototype differs from this realm's, so deepStrictEqual would fail)
assert.strictEqual(JSON.stringify(sentToTab[0].msg.result), JSON.stringify(['0x0000000000000000000000000000000000000000']));

// ---- request 2: another tab, provider id 5 -> ids stay independent ----
onMessageHandler(
  { __saw: 'req', id: 5, method: 'eth_chainId', params: [], chain: '0x185d9' },
  { tab: { id: 78 } },
  () => {}
);
assert.strictEqual(sentToDaemon[1].id, 2, 'second request gets worker id 2');
wsInstance.onmessage({ data: JSON.stringify({ id: 2, result: '0x185d9' }) });
assert.strictEqual(sentToTab[1].msg.id, 5, 'second response maps back to provider id 5');
assert.strictEqual(sentToTab[1].tabId, 78);

console.log('test-background-relay: ALL PASS');
