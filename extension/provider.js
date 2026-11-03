// provider.js — EIP-1193 provider in page context. Forwards to background via window.postMessage.
(function () {
  if (window.ethereum && window.ethereum.isSuperAgentic) return;
  let nextId = 1;
  const pending = {};
  // Chain-aware (2026-10-28): track the current chain and send it as the
  // hint so walletd routes reads to the right chain. Default BSC (0x38),
  // unchanged from before. walletd is stateless: a switch only succeeds
  // when target == hint, so the hint is set BEFORE forwarding the switch.
  let currentChain = '0x38';
  function normChainHex(v) {
    if (typeof v === 'number') return '0x' + v.toString(16);
    if (typeof v === 'string') {
      if (v.startsWith('0x') || v.startsWith('0X')) return v.toLowerCase();
      const n = parseInt(v, 10);
      if (!isNaN(n)) return '0x' + n.toString(16);
    }
    return null;
  }
  function emit(evt, data) {
    (provider._listeners[evt] || []).forEach((cb) => { try { cb(data); } catch (e) {} });
  }
  function setChain(hex) {
    currentChain = hex;
    provider.chainId = hex;
    provider.networkVersion = String(parseInt(hex, 16));
    emit('chainChanged', hex);
  }
  window.addEventListener('message', (e) => {
    if (e.source !== window || !e.data || e.data.__saw !== 'res') return;
    const cb = pending[e.data.id];
    if (cb) { delete pending[e.data.id]; cb(e.data); }
  });
  function request(args) {
    return new Promise((resolve, reject) => {
      const id = nextId++;
      pending[id] = (res) => {
        if (res.error) reject(new Error(res.error.message || 'request failed'));
        else resolve(res.result);
      };
      window.postMessage({ __saw: 'req', id, method: args.method, params: args.params || [], chain: currentChain }, '*');
      setTimeout(() => {
        if (pending[id]) { delete pending[id]; reject(new Error('request timeout')); }
      }, 60000);
    });
  }
  const provider = {
    isSuperAgentic: true,
    isMetaMask: true,
    _metamask: { isUnlocked: () => Promise.resolve(true) },
    chainId: '0x38',
    networkVersion: '56',
    selectedAddress: null,
    _listeners: {},
    request: async (args) => {
      // Intercept chain switch/add so the hint is set before walletd sees it.
      if (args.method === 'wallet_switchEthereumChain') {
        const target = normChainHex((args.params && args.params[0] && args.params[0].chainId) || null);
        if (!target) throw new Error('wallet_switchEthereumChain: missing chainId');
        if (target === currentChain) return request(args);
        const prev = currentChain;
        currentChain = target; // hint must equal target for walletd to accept
        try {
          const res = await request(args);
          setChain(target);
          return res;
        } catch (e) {
          currentChain = prev; // revert on 4902 / failure
          throw e;
        }
      }
      if (args.method === 'wallet_addEthereumChain') {
        const res = await request(args);
        return res; // add only; Uniswap calls switch afterwards
      }
      const res = await request(args);
      if (args.method === 'eth_requestAccounts' || args.method === 'eth_accounts') {
        if (res && res[0]) {
          provider.selectedAddress = res[0];
          (provider._listeners['accountsChanged'] || []).forEach((cb) => cb(res));
        }
      }
      return res;
    },
    on(evt, cb) { (provider._listeners[evt] = provider._listeners[evt] || []).push(cb); },
    removeListener(evt, cb) {
      provider._listeners[evt] = (provider._listeners[evt] || []).filter((f) => f !== cb);
    },
    // legacy compat
    sendAsync: (req, cb) => provider.request(req).then((r) => cb(null, { result: r })).catch((e) => cb(e)),
    send: (req, cb) => { if (cb) provider.sendAsync(req, cb); return provider.request(req); },
    enable: () => provider.request({ method: 'eth_requestAccounts' }),
  };
  window.ethereum = provider;
  // EIP-6963 provider discovery (2026-10-28): announce so modern dApps
  // (Uniswap, etc.) list this wallet in their connect panel. Without it,
  // dApps that discover wallets only via EIP-6963 never see us, and their
  // "Other wallets" list stays empty. Re-announce on request.
  const EIP6963_INFO = {
    uuid: 'superagentic-wallet-001',
    name: 'SuperAgentic Wallet',
    icon: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='64' height='64'%3E%3Ccircle cx='32' cy='32' r='30' fill='%23e6007a'/%3E%3Ctext x='32' y='43' font-size='32' text-anchor='middle' fill='white' font-family='sans-serif'%3ES%3C/text%3E%3C/svg%3E",
    rdns: 'xyz.superagentic.wallet',
  };
  function announceEIP6963() {
    window.dispatchEvent(new CustomEvent('eip6963:announceProvider', {
      detail: { info: EIP6963_INFO, provider },
    }));
  }
  window.addEventListener('eip6963:requestProvider', announceEIP6963);
  announceEIP6963();
  window.dispatchEvent(new Event('ethereum#initialized'));
})();
