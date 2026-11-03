// background.js — service worker. Bridges page <-> wallet daemon over WebSocket.
self.__saw_bg_version = 2; // bump on every background.js change (diagnostic + cache-busting)
const DAEMON = 'ws://127.0.0.1:18923';
let ws = null, nextId = 1;
const pending = {};

function connect() {
  ws = new WebSocket(DAEMON);
  ws.onopen = () => console.log('[saw] daemon connected');
  ws.onclose = () => { console.log('[saw] daemon closed, retry in 3s'); setTimeout(connect, 3000); };
  ws.onerror = () => {};
  ws.onmessage = (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    // walletd echoes OUR id; map it back to the provider's original id so
    // the page-side pending lookup (keyed by the provider's id) matches.
    const entry = pending[msg.id];
    if (entry) { delete pending[msg.id]; chrome.tabs.sendMessage(entry.tab, { __saw: 'res', id: entry.origId, result: msg.result, error: msg.error }); }
  };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.__saw !== 'req' || !sender.tab) return;
  const id = nextId++;
  pending[id] = { tab: sender.tab.id, origId: msg.id };
  const send = () => {
    // Forward the chain hint (elysium provider sends "0x185d9") so walletd
    // routes reads to the right chain. JSON.stringify drops `chain: undefined`
    // from older providers, so their messages are unchanged.
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ id, method: msg.method, params: msg.params, chain: msg.chain }));
    else setTimeout(send, 200);
  };
  send();
});

// inject.js posts to window; content script relays window <-> background
// (this listener lives in the service worker; the tab-side relay is in relay.js)
connect();
