// inject.js — content-script context: injects providers into page, relays page <-> background.
(function () {
  // Elysium origins get the division's "Browser Wallet" provider (signing via
  // walletd); every other origin keeps the existing multichain providers.
  var ELYSIUM_HOSTS = [
    'elysiumeco.xyz',
    'www.elysiumeco.xyz',
    'app.oxley.fun',
    'oxley.fun',
    'dex-da-costa-elysium.vercel.app'
  ];
  var evmProvider = ELYSIUM_HOSTS.indexOf(location.hostname) !== -1
    ? 'elysium_provider.js'
    : 'provider.js';
  for (const f of [evmProvider, 'solana_provider.js', 'multichain_provider.js']) {
    const s = document.createElement('script');
    s.src = chrome.runtime.getURL(f);
    s.onload = function () { this.remove(); };
    (document.head || document.documentElement).appendChild(s);
  }

  // page -> background (forward the chain hint so walletd routes reads correctly)
  window.addEventListener('message', (e) => {
    if (e.source !== window || !e.data || e.data.__saw !== 'req') return;
    chrome.runtime.sendMessage({ __saw: 'req', id: e.data.id, method: e.data.method, params: e.data.params, chain: e.data.chain });
  });
  // background -> page
  chrome.runtime.onMessage.addListener((msg) => {
    if (!msg || msg.__saw !== 'res') return;
    window.postMessage(msg, '*');
  });
})();
