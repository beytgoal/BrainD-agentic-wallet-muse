// multichain_provider.js — Sui (window.sui, detected as Slush) + Cosmos/IBC (window.keplr).
// Forwards to daemon via window.postMessage (same relay as EVM/Solana).
(function () {
  let nextId = 2000001;
  const pending = {};
  window.addEventListener('message', (e) => {
    if (e.source !== window || !e.data || e.data.__saw !== 'res') return;
    const cb = pending[e.data.id];
    if (cb) { delete pending[e.data.id]; cb(e.data); }
  });
  function daemonReq(prefix, method, params) {
    return new Promise((resolve, reject) => {
      const id = nextId++;
      pending[id] = (res) => {
        if (res.error) reject(new Error(res.error.message || 'request failed'));
        else resolve(res.result);
      };
      window.postMessage({ __saw: 'req', id, method: prefix + '_' + method, params: params || [] }, '*');
      setTimeout(() => {
        if (pending[id]) { delete pending[id]; reject(new Error('request timeout')); }
      }, 60000);
    });
  }

  // ---- Sui: detected as Slush (new Sui wallet name) ----
  if (!window.sui || !window.sui.isSuperAgentic) {
    const sui = {
      isSuperAgentic: true,
      isSlush: true,
      name: 'Slush',
      _listeners: {},
      async connect() {
        const r = await daemonReq('sui', 'connect');
        sui._addr = r.address;
        (sui._listeners['connect'] || []).forEach((cb) => cb(r.address));
        return { address: r.address };
      },
      async disconnect() {
        sui._addr = null;
        (sui._listeners['disconnect'] || []).forEach((cb) => cb());
      },
      async getAddress() { return sui._addr || (await sui.connect()).address; },
      on(evt, cb) { (sui._listeners[evt] = sui._listeners[evt] || []).push(cb); return sui; },
      removeListener(evt, cb) {
        sui._listeners[evt] = (sui._listeners[evt] || []).filter((f) => f !== cb);
      },
    };
    window.sui = sui;
  }

  // ---- Cosmos/IBC: detected as Keplr ----
  if (!window.keplr || !window.keplr.isSuperAgentic) {
    const keplr = {
      isSuperAgentic: true,
      isKeplr: true,
      _listeners: {},
      async enable(chainId) {
        const r = await daemonReq('cosmos', 'connect');
        keplr._addr = r.address;
        return;
      },
      async getKey(chainId) {
        const addr = keplr._addr || (await daemonReq('cosmos', 'connect')).address;
        return { bech32Address: addr, address: addr, name: 'SuperAgentic', algo: 'secp256k1' };
      },
      async getBalance(chainId, bech32Addr) {
        return daemonReq('cosmos', 'getBalance');
      },
      // stub: full tx signing via Keplr offline signer not yet wired
      getOfflineSigner(chainId) {
        return {
          getAccounts: async () => [{ address: keplr._addr, algo: 'secp256k1', pubkey: new Uint8Array() }],
          signDirect: async () => { throw new Error('cosmos signDirect not yet supported'); },
          signAmino: async () => { throw new Error('cosmos signAmino not yet supported'); },
        };
      },
      on(evt, cb) { (keplr._listeners[evt] = keplr._listeners[evt] || []).push(cb); return keplr; },
      removeListener(evt, cb) {
        keplr._listeners[evt] = (keplr._listeners[evt] || []).filter((f) => f !== cb);
      },
    };
    window.keplr = keplr;
  }
})();
