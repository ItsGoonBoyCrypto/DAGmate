/**
 * DAGmate — wallet adapters. One normalized interface over Kaspa's fragmented wallet APIs, so the app
 * never branches on which wallet is connected. Each adapter exposes:
 *   id, label, isInstalled() -> bool
 *   connect() -> { address, pubkey }        (pubkey may be x-only 32B or compressed 33B — the backend
 *                                            normalizes to x-only before baking it into the covenant)
 *   signMessage(msg) -> signatureHex        (KIP-5 personal message; verified server-side by verifyMessage)
 *   sendKaspa(to, sompi) -> txid            (funding a deposit)
 *   signSettle(txJson, inputIndexes) -> { signedTxJson }   (v1/v2 settle / reclaim co-sign)
 *
 * Four sources of wallets, because they do NOT share one API (verified from each wallet's own source):
 *  - Kasware  — window.kasware, classic direct methods (requestAccounts/getPublicKey/signMessage/
 *               sendKaspa/signPskt). The de-facto original; the Kaspa Wallet Standard was modeled on it.
 *  - Kastle   — window.kastle, its OWN Kaspa API: connect() + getAccount()->{address,publicKey},
 *               signMessage, sendKaspa. NO requestAccounts. Settle uses signTx()+scripts (needs the
 *               per-input redeem scriptHex, not plumbed here) — see signSettle below.
 *  - Kaspire  — window.kaspire.request({method,params}). Two traps: signMessage returns an OBJECT
 *               (.signature), and EVERY method binds to the wallet's selectedAddress and rejects a
 *               mismatched `address` (-32602) — so we pass NO address and let it default.
 *  - Kaspa Wallet Standard (KIP-12) — wallets announce a { info, provider } via a `kaspa:provider`
 *               window event (this is how Enclave, and any future standard wallet, appears). We listen,
 *               dispatch `kaspa:requestProvider` to trigger (re)announces, and wrap the provider.
 *
 * Exposed as window.DAGWallets: ready() (async — kicks + awaits standard discovery), all(), installed(),
 * byId(). Detection is re-checked at connect time (a user click is always well after injection/announce).
 */
(function () {
  const SIGHASH_ALL = 1;

  // signMessage returns a bare hex string on Kasware/Kastle/the standard, but an OBJECT on Kaspire.
  function asSigHex(res) {
    if (res && typeof res === "object") return res.signature || res.signedMessage || res;
    return res;
  }
  // signPskt returns the signed tx JSON string, but some wallets wrap it in an object key.
  function asSignedJson(res) {
    if (res && typeof res === "object") return res.signedTxJson || res.psktTransactionJson || res;
    return res;
  }

  // ── Kasware (classic direct methods) ──
  function kaswareAdapter() {
    const g = () => window.kasware;
    return {
      id: "kasware", label: "Kasware",
      isInstalled: () => !!(g() && typeof g().requestAccounts === "function"),
      async connect() {
        const p = g();
        const accounts = await p.requestAccounts();
        const address = accounts && accounts[0];
        if (!address) throw new Error("no account returned");
        if (typeof p.getPublicKey !== "function") throw new Error("Kasware doesn't expose its public key, which DAGmate needs to build your escrow");
        return { address, pubkey: await p.getPublicKey() };
      },
      async signMessage(msg) { return asSigHex(await g().signMessage(msg)); },
      async sendKaspa(to, sompi) { return g().sendKaspa(to, Number(sompi)); },
      async signSettle(txJson, indexes) {
        const signed = await g().signPskt({ txJsonString: txJson, options: { signInputs: indexes.map((i) => ({ index: i, sighashType: SIGHASH_ALL })) } });
        return { signedTxJson: asSignedJson(signed) };
      },
    };
  }

  // ── Kastle (its own Kaspa API: connect() + getAccount(), kas:* bridge) ──
  function kastleAdapter() {
    const g = () => window.kastle;
    return {
      id: "kastle", label: "Kastle",
      // Kastle's Kaspa provider has no requestAccounts — detect its real entry points instead.
      isInstalled: () => !!(g() && (typeof g().connect === "function" || typeof g().getAccount === "function")),
      async connect() {
        const p = g();
        if (typeof p.connect === "function") { const ok = await p.connect(); if (ok === false) throw new Error("Kastle connection was rejected"); }
        const acct = (typeof p.getAccount === "function") ? await p.getAccount() : await p.request("kas:get_account");
        const address = acct && (acct.address || (Array.isArray(acct) && acct[0]));
        const pubkey = acct && (acct.publicKey || acct.pubkey);
        if (!address) throw new Error("Kastle didn't return an account");
        if (!pubkey) throw new Error("Kastle didn't return a public key, which DAGmate needs to build your escrow");
        return { address, pubkey };
      },
      async signMessage(msg) {
        const p = g();
        if (typeof p.signMessage !== "function") throw new Error("Kastle can't sign messages here, so it can't prove the address is yours");
        return asSigHex(await p.signMessage(msg));
      },
      async sendKaspa(to, sompi) {
        const p = g();
        if (typeof p.sendKaspa !== "function") throw new Error("Kastle can't send KAS from here — deposit the stake to the escrow address manually");
        return p.sendKaspa(to, Number(sompi));
      },
      async signSettle() {
        // Kastle releases via signTx(networkId, txJson, scripts[{inputIndex, scriptHex, signType}]) — it
        // needs the per-input redeem scriptHex, which this settle flow doesn't yet plumb through. v3 staked
        // matches settle trustlessly on-chain (no player signature), so this only bites the v1/v2 co-sign
        // path — fail with a clear message instead of producing a bad signature.
        throw new Error("Releasing a pot with Kastle isn't wired up yet — for that step connect Kasware or Kaspire. (Connecting, funding and play all work with Kastle.)");
      },
    };
  }

  // ── Kaspire (request({method,params}); binds to selectedAddress — pass NO address) ──
  function kaspireAdapter() {
    const g = () => window.kaspire;
    // After a reload the session restores but nothing is selected until we touch it; getAccounts needs no
    // prompt. This also guarantees selectedAddress is set before signMessage/sendKaspa/signPskt (which key
    // off it). We never pass an explicit address — Kaspire rejects a mismatch with -32602.
    async function ensureConnected() {
      let a = await g().request({ method: "getAccounts" });
      if (!a || !a[0]) a = await g().request({ method: "requestAccounts" });
      if (!a || !a[0]) throw new Error("Kaspire is locked or disconnected — reconnect it");
      return a[0];
    }
    return {
      id: "kaspire", label: "Kaspire",
      isInstalled: () => !!(g() && g().isKaspire),
      async connect() {
        const k = g();
        const accounts = await k.request({ method: "requestAccounts" });
        const address = accounts && accounts[0];
        if (!address) throw new Error("no account returned");
        return { address, pubkey: await k.request({ method: "getPublicKey" }) };  // x-only
      },
      async signMessage(msg) {
        await ensureConnected();
        return asSigHex(await g().request({ method: "signMessage", params: { message: msg } }));
      },
      async sendKaspa(to, sompi) {
        await ensureConnected();
        return g().request({ method: "sendKaspa", params: { to, amountSompi: String(sompi) } });
      },
      async signSettle(txJson, indexes) {
        await ensureConnected();
        const signed = await g().request({ method: "signPskt", params: {
          txJsonString: txJson, options: { signInputs: indexes.map((i) => ({ index: i, sighashType: SIGHASH_ALL })) },
        } });
        return { signedTxJson: asSignedJson(signed) };
      },
    };
  }

  // ── Kaspa Wallet Standard (KIP-12) — discovered providers (Enclave + any future standard wallet) ──
  const standardProviders = new Map();   // key: rdns || uuid || name  →  { info, provider }
  function providerKey(info) { return info.rdns || info.uuid || info.name; }
  try {
    window.addEventListener("kaspa:provider", function (e) {
      const d = e && e.detail;
      if (!d || !d.info || !d.provider || typeof d.provider.requestAccounts !== "function") return;
      standardProviders.set(providerKey(d.info), d);
    });
  } catch (_) { /* no window (shouldn't happen in a page) */ }
  function kickDiscovery() { try { window.dispatchEvent(new Event("kaspa:requestProvider")); } catch (_) {} }
  kickDiscovery();   // announce-on-load; wallets also re-announce on the request event

  function standardAdapter(detail) {
    const info = detail.info;
    const provider = detail.provider;
    const key = providerKey(info);
    return {
      id: "std:" + key, label: info.name || "Kaspa wallet", icon: info.icon || null, standard: true,
      isInstalled: () => standardProviders.has(key),
      async connect() {
        const p = standardProviders.get(key).provider;   // freshest provider for this wallet
        const accounts = await p.requestAccounts();
        const address = accounts && accounts[0];
        if (!address) throw new Error("no account returned");
        let pubkey = null;
        if (typeof p.getPublicKey === "function") pubkey = await p.getPublicKey();
        else if (typeof p.request === "function") { try { pubkey = await p.request("kaspa:getPublicKey"); } catch (_) {} }
        if (!pubkey) throw new Error(`${info.name} didn't return a public key, which DAGmate needs to build your escrow`);
        return { address, pubkey };
      },
      async signMessage(msg) {
        const p = standardProviders.get(key).provider;
        if (typeof p.signMessage === "function") return asSigHex(await p.signMessage(msg));
        if (typeof p.request === "function") return asSigHex(await p.request("kaspa:signPersonal", msg));
        throw new Error(`${info.name} can't sign messages, so it can't prove the address is yours`);
      },
      async sendKaspa(to, sompi) {
        const p = standardProviders.get(key).provider;
        if (typeof p.sendKaspa === "function") return p.sendKaspa(to, Number(sompi));
        if (typeof p.request === "function") return p.request("kaspa:sendTransaction", { to, amountSompi: String(sompi) });
        throw new Error(`${info.name} can't send KAS from here — deposit the stake to the escrow address manually`);
      },
      async signSettle(txJson, indexes) {
        const p = standardProviders.get(key).provider;
        const arg = { txJsonString: txJson, options: { signInputs: indexes.map((i) => ({ index: i, sighashType: SIGHASH_ALL })) } };
        let signed;
        if (typeof p.signPskt === "function") signed = await p.signPskt(arg);
        else if (typeof p.request === "function") signed = await p.request("kaspa:signPskt", arg);
        else throw new Error(`${info.name} can't sign the release transaction`);
        return { signedTxJson: asSignedJson(signed) };
      },
    };
  }

  const CLASSIC = [kaswareAdapter(), kastleAdapter(), kaspireAdapter()];
  const allAdapters = () => CLASSIC.concat([...standardProviders.values()].map(standardAdapter));

  window.DAGWallets = {
    // Kick standard discovery and give wallets a beat to announce, then resolve. Call before reading
    // installed() from a user click so a standard wallet (Enclave) has answered.
    ready: async function () { kickDiscovery(); await new Promise((r) => setTimeout(r, 200)); return true; },
    all: () => allAdapters(),
    // Installed adapters in preference order (classic first), deduped by label so a wallet that appears
    // both classic and via the standard is only offered once.
    installed: () => {
      const out = []; const seen = new Set();
      for (const a of allAdapters()) {
        let ok; try { ok = a.isInstalled(); } catch (_) { ok = false; }
        if (!ok) continue;
        const key = (a.label || a.id).toLowerCase();
        if (seen.has(key)) continue;
        seen.add(key); out.push(a);
      }
      return out;
    },
    byId: (id) => allAdapters().find((a) => a.id === id) || null,
  };
})();
