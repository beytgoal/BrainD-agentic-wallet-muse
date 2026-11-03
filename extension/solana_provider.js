// solana_provider.js — Solana wallet provider (window.solana / window.phantom).
// Detected as Phantom. Forwards to daemon via window.postMessage (same relay as EVM).
(function () {
  if (window.solana && window.solana.isSuperAgentic) return;
  let nextId = 1000001;
  const pending = {};
  window.addEventListener('message', (e) => {
    if (e.source !== window || !e.data || e.data.__saw !== 'res') return;
    const cb = pending[e.data.id];
    if (cb) { delete pending[e.data.id]; cb(e.data); }
  });
  function req(method, params) {
    return new Promise((resolve, reject) => {
      const id = nextId++;
      pending[id] = (res) => {
        if (res.error) reject(new Error(res.error.message || 'request failed'));
        else resolve(res.result);
      };
      window.postMessage({ __saw: 'req', id, method: 'sol_' + method, params: params || [] }, '*');
      setTimeout(() => {
        if (pending[id]) { delete pending[id]; reject(new Error('request timeout')); }
      }, 60000);
    });
  }
  const provider = {
    isSuperAgentic: true,
    isPhantom: true, // detected as Phantom by dApps
    publicKey: null,
    isConnected: false,
    _listeners: {},
    async connect() {
      const r = await req('connect');
      provider.publicKey = { toString: () => r.publicKey, toBase58: () => r.publicKey };
      provider.isConnected = true;
      (provider._listeners['connect'] || []).forEach((cb) => cb(r.publicKey));
      return { publicKey: provider.publicKey };
    },
    async disconnect() {
      provider.publicKey = null;
      provider.isConnected = false;
      (provider._listeners['disconnect'] || []).forEach((cb) => cb());
    },
    async signMessage(msg) {
      const b64 = btoa(String.fromCharCode(...msg));
      const r = await req('signMessage', [b64]);
      return { signature: Uint8Array.from(atob(r.signature), (c) => c.charCodeAt(0)) };
    },
    async signTransaction(tx) { return tx; }, // dApp should use signAndSendTransaction
    async signAndSendTransaction(tx) {
      const b64 = btoa(String.fromCharCode(...tx.serialize()));
      const r = await req('signAndSendTransaction', [b64]);
      return { signature: r.signature };
    },
    on(evt, cb) { (provider._listeners[evt] = provider._listeners[evt] || []).push(cb); return provider; },
    removeListener(evt, cb) {
      provider._listeners[evt] = (provider._listeners[evt] || []).filter((f) => f !== cb);
    },
  };
  window.solana = provider;
  window.phantom = { solana: provider }; // Phantom-style namespace
  window.dispatchEvent(new Event('solana#initialized'));
})();
