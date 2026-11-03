// elysium_provider.js — EIP-1193 "Browser Wallet" provider for Elysium origins.
// Adapted from the Airdrop & NFT division's inpage.js (custom.browser-wallet),
// re-wired to the SuperAgentic bridge relay (__saw) so ALL signing goes
// through walletd — NO key is stored in this extension and NO local signing
// happens here.
//
// Differences vs the division's inpage.js:
//   - chainId is '0x185d9' (99801). The division wrote '0x185f1' (= 99825) — wrong.
//   - Requests go through the bridge relay (__saw) to walletd, not to a local signer.
//   - eth_chainId / net_version are answered locally (fast, works even while the
//     daemon is briefly down); everything else goes to walletd, which is the
//     single source of truth for the address and the chain.
//   - Guardrails (allowlist, chain 99801 only, 0.05 HYPE value cap, no contract
//     deployment, eth_sign refused) are enforced here AND in walletd; this file
//     is the user-facing check, walletd is the backstop.
(function () {
  'use strict';

  var CHAIN_ID_HEX = '0x185d9'; // Elysium testnet (99801) — 0x185f1 would be 99825, WRONG
  var CHAIN_ID_DEC = 99801;
  var NETWORK_VERSION = '99801';
  var VALUE_CAP_WEI = '50000000000000000'; // 0.05 HYPE, division cap

  var ALLOWLIST = [
    'elysiumeco.xyz',
    'www.elysiumeco.xyz',
    'app.oxley.fun',
    'oxley.fun',
    'dex-da-costa-elysium.vercel.app'
  ];

  // Only install on allowlisted Elysium origins (inject.js already gates this;
  // the check here is defense in depth).
  if (ALLOWLIST.indexOf(location.hostname) === -1) return;

  // Never clobber a provider we already installed (e.g. double injection).
  // (Marker is __sawElysium: this provider identifies as MetaMask so the
  // site treats it as a known wallet; isBrowserWallet is no longer set.)
  if (window.ethereum && window.ethereum.__sawElysium) {
    window.dispatchEvent(new Event('ethereum#initialized'));
    return;
  }

  var idCounter = 1000001; // distinct range from the other bridge providers
  var pending = {};
  var listeners = {};

  function emit(event) {
    var args = Array.prototype.slice.call(arguments, 1);
    (listeners[event] || []).slice().forEach(function (fn) {
      try { fn.apply(null, args); } catch (_) { /* listener error: ignore */ }
    });
  }

  function rpcError(code, message) {
    var err = new Error(message);
    err.code = code;
    return err;
  }

  function parseChainId(v) {
    if (typeof v === 'number') return v;
    if (typeof v === 'string') {
      var n = v.indexOf('0x') === 0 ? parseInt(v, 16) : parseInt(v, 10);
      return isNaN(n) ? null : n;
    }
    return null;
  }

  function forward(args) {
    return new Promise(function (resolve, reject) {
      var id = ++idCounter;
      var timer = setTimeout(function () {
        if (!pending[id]) return;
        delete pending[id];
        reject(rpcError(-32000, 'Wallet request timed out'));
      }, 60000);
      pending[id] = {
        resolve: function (v) { clearTimeout(timer); resolve(v); },
        reject: function (e) { clearTimeout(timer); reject(e); }
      };
      window.postMessage({
        __saw: 'req', id: id, method: args.method, params: args.params || [],
        chain: CHAIN_ID_HEX
      }, '*');
    }).then(function (result) {
      // Keep selectedAddress in sync for dApps that read it directly.
      if ((args.method === 'eth_requestAccounts' || args.method === 'eth_accounts') &&
          Array.isArray(result) && result.length > 0) {
        if (provider.selectedAddress !== result[0]) {
          provider.selectedAddress = result[0];
          emit('accountsChanged', [result[0]]);
        }
      }
      return result;
    });
  }

  window.addEventListener('message', function (e) {
    if (!e || e.source !== window || !e.data || e.data.__saw !== 'res') return;
    var cb = pending[e.data.id];
    if (!cb) return;
    delete pending[e.data.id];
    if (e.data.error) {
      var err = new Error(e.data.error.message || 'request failed');
      if (e.data.error.code !== undefined) err.code = e.data.error.code;
      cb.reject(err);
    } else {
      cb.resolve(e.data.result);
    }
  });

  function request(args) {
    var method = args && args.method;
    var params = (args && args.params) || [];

    // ---- local answers (fast, no daemon round-trip) ----
    if (method === 'eth_chainId') return Promise.resolve(CHAIN_ID_HEX);
    if (method === 'net_version') return Promise.resolve(NETWORK_VERSION);

    // ---- client-side guardrails (walletd enforces them again server-side) ----
    if (method === 'wallet_switchEthereumChain') {
      var target = params[0] && params[0].chainId;
      if (target && parseChainId(target) === CHAIN_ID_DEC) return forward(args);
      return Promise.reject(rpcError(4902, 'This wallet only supports Elysium testnet (chain 99801)'));
    }
    if (method === 'wallet_addEthereumChain') {
      var add = params[0] || {};
      if (parseChainId(add.chainId) === CHAIN_ID_DEC) return forward(args);
      return Promise.reject(rpcError(4902, 'This wallet only supports Elysium testnet (chain 99801)'));
    }
    if (method === 'eth_sign' || method === 'eth_signTransaction') {
      return Promise.reject(rpcError(4200, method + ' is not supported (use personal_sign / eth_sendTransaction)'));
    }
    if (method === 'eth_sendTransaction') {
      var tx = params[0] || {};
      if (!tx.to) {
        return Promise.reject(rpcError(-32602, 'Contract deployment is not supported by this wallet'));
      }
      if (tx.chainId !== undefined && tx.chainId !== null &&
          parseChainId(tx.chainId) !== CHAIN_ID_DEC) {
        return Promise.reject(rpcError(4902, 'This wallet only supports Elysium testnet (chain 99801)'));
      }
      if (tx.value) {
        try {
          if (BigInt(tx.value) > BigInt(VALUE_CAP_WEI)) {
            return Promise.reject(rpcError(4001, 'Transaction value exceeds the 0.05 HYPE cap for the Elysium path'));
          }
        } catch (_) { /* unparsable value: let walletd judge */ }
      }
      if (tx.from && provider.selectedAddress &&
          tx.from.toLowerCase() !== provider.selectedAddress.toLowerCase()) {
        return Promise.reject(rpcError(-32602, 'Transaction "from" does not match the wallet address'));
      }
    }
    if (method === 'personal_sign') {
      var psAddr = params[1];
      if (psAddr && provider.selectedAddress &&
          psAddr.toLowerCase() !== provider.selectedAddress.toLowerCase()) {
        return Promise.reject(rpcError(-32602, 'personal_sign address does not match the wallet address'));
      }
    }
    if (method === 'eth_signTypedData_v4') {
      var tdAddr = params[0];
      if (tdAddr && provider.selectedAddress &&
          tdAddr.toLowerCase() !== provider.selectedAddress.toLowerCase()) {
        return Promise.reject(rpcError(-32602, 'signTypedData address does not match the wallet address'));
      }
    }
    return forward(args);
  }

  var provider = {
    // Identifikasi sebagai MetaMask (perintah owner 28 Okt 22:15 WIB):
    // situs sebelumnya mendeteksi wallet ini sebagai "Browser Wallet"
    // generik (isMetaMask: false) dan menolak sign-in; sebagai MetaMask
    // situs memperlakukannya sebagai wallet yang dikenal.
    isMetaMask: true,
    isBrowserWallet: false,
    __sawElysium: true,
    chainId: CHAIN_ID_HEX,
    networkVersion: NETWORK_VERSION,
    selectedAddress: null,

    request: request,

    // Legacy MetaMask-style send (string form and object form).
    send: function (method, params) {
      if (typeof method === 'string') return request({ method: method, params: params });
      return request(method);
    },

    // Legacy callback-style sendAsync.
    sendAsync: function (payload, callback) {
      request(payload)
        .then(function (result) {
          callback(null, { jsonrpc: '2.0', id: payload.id, result: result });
        })
        .catch(function (err) { callback(err); });
    },

    on: function (event, fn) {
      (listeners[event] = listeners[event] || []).push(fn);
    },
    once: function (event, fn) {
      var self = this;
      function wrapper() {
        self.removeListener(event, wrapper);
        fn.apply(null, arguments);
      }
      this.on(event, wrapper);
    },
    removeListener: function (event, fn) {
      if (!listeners[event]) return;
      listeners[event] = listeners[event].filter(function (f) { return f !== fn; });
    },
    removeAllListeners: function (event) {
      if (event) delete listeners[event];
      else listeners = {};
    },

    _emit: emit
  };

  // Only take the window.ethereum slot when no other wallet claimed it.
  // (If MetaMask/Rabby is already there, dApps keep using it; ours stays
  // available as window.browserWallet.)
  if (!window.ethereum) {
    window.ethereum = provider;
  } else {
    window.browserWallet = provider;
  }
  window.dispatchEvent(new Event('ethereum#initialized'));
})();
