/**
 * DAGmate — wallet adapters. One normalized interface over the fragmented Kaspa wallet APIs, so the
 * app never branches on which extension is installed. Each adapter exposes:
 *   id, label, isInstalled() -> bool
 *   connect() -> { address, pubkey }        (x-only pubkey — the covenant bakes it)
 *   signMessage(msg) -> signatureHex        (verified server-side by the SDK's verifyMessage)
 *   sendKaspa(toAddress, sompi) -> txid     (funding a deposit)
 *   signSettle(txJson, inputIndexes) -> { signedTxJson }   (v1 settle / reclaim co-sign)
 *
 * Kasware and Kastle share the classic direct-method API (window.kasware.requestAccounts(), etc.).
 * Kaspire uses a request({method,params}) provider on window.kaspire with different arg shapes, so it
 * gets its own adapter. Enclave is NOT included yet — its docs are Igra/EVM-leaning and a Kaspa-L1
 * signing provider isn't confirmed; add an adapter here once verified rather than ship a dead button.
 *
 * Exposed as window.DAGWallets for the classic app.js script.
 */
(function () {
  // ── classic direct-method wallets (Kasware, Kastle) ──
  function directAdapter(id, label, globalName) {
    const g = () => window[globalName];
    let address = null;
    return {
      id, label,
      isInstalled: () => !!(g() && typeof g().requestAccounts === "function"),
      async connect() {
        const p = g();
        const accounts = await p.requestAccounts();
        address = accounts && accounts[0];
        if (!address) throw new Error("no account returned");
        if (typeof p.getPublicKey !== "function") throw new Error(label + " doesn't expose its public key, which DAGmate needs to build your escrow");
        const pubkey = await p.getPublicKey();
        return { address, pubkey };
      },
      async signMessage(msg) {
        const p = g();
        if (typeof p.signMessage !== "function") throw new Error(label + " can't sign messages, so it can't prove the address is yours");
        return p.signMessage(msg);
      },
      async sendKaspa(to, sompi) {
        const p = g();
        if (typeof p.sendKaspa !== "function") throw new Error(label + " can't send KAS from here — deposit the stake manually to the escrow address");
        return p.sendKaspa(to, Number(sompi));
      },
      async signSettle(txJson, indexes) {
        const p = g();
        if (typeof p.signPskt !== "function") throw new Error(label + " doesn't expose signPskt, which DAGmate needs to release the pot");
        const signed = await p.signPskt({ txJsonString: txJson, options: { signInputs: indexes.map((i) => ({ index: i, sighashType: 1 })) } });
        return { signedTxJson: signed };
      },
    };
  }

  // ── request({method,params}) provider (Kaspire) ──
  function kaspireAdapter() {
    const g = () => window.kaspire;
    let address = null;
    // After a page reload the session is restored but `address` is null; re-fetch it silently
    // (getAccounts needs no approval) so signing still works without a fresh connect prompt.
    async function ensureAddr() {
      if (address) return address;
      const a = await g().request({ method: "getAccounts" });
      address = (a && a[0]) || null;
      if (!address) { const r = await g().request({ method: "requestAccounts" }); address = r && r[0]; }
      if (!address) throw new Error("Kaspire is locked or disconnected — reconnect it");
      return address;
    }
    return {
      id: "kaspire", label: "Kaspire",
      isInstalled: () => !!(g() && g().isKaspire),
      async connect() {
        const k = g();
        const accounts = await k.request({ method: "requestAccounts" });
        address = accounts && accounts[0];
        if (!address) throw new Error("no account returned");
        const pubkey = await k.request({ method: "getPublicKey" });   // x-only
        return { address, pubkey };
      },
      async signMessage(msg) {
        await ensureAddr();
        const res = await g().request({ method: "signMessage", params: { address, message: msg } });
        return res && res.signature ? res.signature : res;   // adapter returns the bare signature
      },
      async sendKaspa(to, sompi) {
        await ensureAddr();
        return g().request({ method: "sendKaspa", params: { from: address, to, amountSompi: String(sompi) } });
      },
      async signSettle(txJson, indexes) {
        await ensureAddr();
        const signed = await g().request({ method: "signPskt", params: {
          sender: address, txJsonString: txJson,
          options: { signInputs: indexes.map((i) => ({ index: i, sighashType: 1 })) },
        } });
        return { signedTxJson: signed };
      },
    };
  }

  const ALL = [
    directAdapter("kasware", "Kasware", "kasware"),
    directAdapter("kastle", "Kastle", "kastle"),
    kaspireAdapter(),
  ];

  window.DAGWallets = {
    all: () => ALL,
    // Installed adapters, in preference order. Kaspire announces late via an init event, so callers
    // that run at load may miss it — detection is re-checked at connect time (a user click is always
    // well after injection).
    installed: () => ALL.filter((a) => { try { return a.isInstalled(); } catch { return false; } }),
    byId: (id) => ALL.find((a) => a.id === id) || null,
  };
})();
