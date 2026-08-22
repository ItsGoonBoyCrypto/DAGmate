/* DAGmate — frontend app (docs/DAGMATE_SPEC.md). Plain JS, no build step.
 * Wallet-connect uses the real window.kasware / window.kastle injected APIs
 * (requestAccounts()) when a browser extension is present. When none is
 * detected — e.g. this headless preview — it falls back to the backend's
 * dev-only demo wallet (POST /api/dev/demo-wallet), clearly labeled as such.
 * That fallback is NEVER part of the real signing/settlement path.
 */
(() => {
  "use strict";

  const STANDARD_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
  const PIECE_NAME = { p: "pawn", n: "knight", b: "bishop", r: "rook", q: "queen", k: "king" };

  function displayName(obj) {
    if (!obj) return "?";
    return obj.knsName || obj.shortAddress || obj.address || "?";
  }

  const state = {
    address: null,
    pubkey: null,
    knsName: null,
    isDemoWallet: false,
    profile: null,
    currentMatchId: null,
    currentMatch: null,
    // When the current match's clock snapshot arrived, by this device's own
    // Date.now(). Countdowns are driven off the ELAPSED time since then, never
    // off the device's absolute clock, so a machine set to the wrong date still
    // shows the right numbers.
    clockRecvAt: 0,
    // Settlement is prepared once per match (it builds a real tx), so the
    // match it belongs to is tracked alongside it.
    settle: null,
    settleFor: null,
    selectedSquare: null,
    practiceFen: STANDARD_FEN,
    practiceLegalMoves: [],
    practiceSelected: null,
    practiceTurnLock: false,
    practiceLevels: [],
    practiceLevel: null,
  };

  // ── tiny helpers ─────────────────────────────────────────────────────
  async function api(method, path, body) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const r = await fetch(path, opts);
    let data = null;
    try { data = await r.json(); } catch (_) { /* no body */ }
    if (!r.ok) throw new Error((data && data.detail) || r.statusText);
    return data;
  }

  let toastTimer = null;
  function toast(msg) {
    const el = document.getElementById("toast");
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 3500);
  }

  function fenToBoard(fen) {
    const placement = fen.split(" ")[0];
    return placement.split("/").map((row) => {
      const cells = [];
      for (const ch of row) {
        if (/\d/.test(ch)) { for (let i = 0; i < Number(ch); i++) cells.push(null); }
        else cells.push(ch);
      }
      return cells;
    });
  }

  function squareName(rowIdx, colIdx) {
    return `${String.fromCharCode(97 + colIdx)}${8 - rowIdx}`;
  }

  function pieceAt(board, sq) {
    const colIdx = sq.charCodeAt(0) - 97;
    const rowIdx = 8 - Number(sq[1]);
    return board[rowIdx][colIdx];
  }

  function isMyPiece(piece, color) {
    if (!piece) return false;
    return color === "white" ? piece === piece.toUpperCase() : piece === piece.toLowerCase();
  }

  function findUci(legalMoves, from, to) {
    const matches = (legalMoves || []).filter((m) => m.slice(0, 2) === from && m.slice(2, 4) === to);
    if (!matches.length) return null;
    return matches.find((m) => m.length === 5 && m[4] === "q") || matches[0];
  }

  // ── generic board renderer ──────────────────────────────────────────
  function renderBoard(el, fen, { legalMoves = [], selected = null, onClick = null, flipped = false } = {}) {
    const board = fenToBoard(fen);
    const cells = [];
    for (let r = 0; r < 8; r++) {
      for (let c = 0; c < 8; c++) {
        cells.push({ row: r, col: c, sq: squareName(r, c), piece: board[r][c] });
      }
    }
    if (flipped) cells.reverse();

    el.innerHTML = "";
    const legalTargets = selected ? (legalMoves || []).filter((m) => m.slice(0, 2) === selected).map((m) => m.slice(2, 4)) : [];

    for (const cell of cells) {
      const div = document.createElement("div");
      const light = (cell.row + cell.col) % 2 === 0;
      div.className = `sq ${light ? "light" : "dark"}`;
      if (cell.sq === selected) div.classList.add("selected");
      if (legalTargets.includes(cell.sq)) {
        div.classList.add("legal");
        if (cell.piece) div.classList.add("has-piece");
      }
      const isEdgeFile = flipped ? cell.col === 7 : cell.col === 0;
      const isEdgeRank = flipped ? cell.row === 0 : cell.row === 7;
      if (isEdgeRank) {
        const f = document.createElement("span");
        f.className = "coord file";
        f.textContent = cell.sq[0];
        div.appendChild(f);
      }
      if (isEdgeFile) {
        const r = document.createElement("span");
        r.className = "coord rank";
        r.textContent = cell.sq[1];
        div.appendChild(r);
      }
      if (cell.piece) {
        const isWhite = cell.piece === cell.piece.toUpperCase();
        const img = document.createElement("img");
        img.className = "piece";
        img.draggable = false;
        img.alt = PIECE_NAME[cell.piece.toLowerCase()];
        img.src = `assets/pieces/${isWhite ? "w" : "b"}${cell.piece.toUpperCase()}.svg`;
        div.appendChild(img);
      }
      if (onClick) div.addEventListener("click", () => onClick(cell.sq));
      el.appendChild(div);
    }
  }

  // ── tabs ─────────────────────────────────────────────────────────────
  document.getElementById("tabs").addEventListener("click", (e) => {
    const btn = e.target.closest(".tab");
    if (!btn) return;
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === btn));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    document.getElementById(`panel-${btn.dataset.tab}`).classList.add("active");
    if (btn.dataset.tab === "tournaments") refreshTournaments();
    if (btn.dataset.tab === "learn") refreshLearn();
  });

  // ── wallet ───────────────────────────────────────────────────────────
  function renderWalletBadge() {
    const area = document.getElementById("walletArea");
    if (!state.address) {
      area.innerHTML = `<button class="btn btn-primary" id="connectBtn">Connect wallet</button>`;
      document.getElementById("connectBtn").addEventListener("click", connectWallet);
      return;
    }
    const short = state.address.length > 14 ? `${state.address.slice(0, 8)}…${state.address.slice(-6)}` : state.address;
    const label = state.knsName || short;
    area.innerHTML = `<span class="wallet-badge">${state.isDemoWallet ? "🧪 demo · " : ""}${label}</span>
      <button class="btn" id="disconnectBtn">Disconnect</button>`;
    document.getElementById("disconnectBtn").addEventListener("click", disconnectWallet);
  }

  async function setWallet(address, pubkey, isDemo) {
    state.address = address;
    state.pubkey = pubkey || null;
    state.isDemoWallet = isDemo;
    localStorage.setItem("dagmate_address", address);
    if (pubkey) localStorage.setItem("dagmate_pubkey", pubkey); else localStorage.removeItem("dagmate_pubkey");
    localStorage.setItem("dagmate_demo", isDemo ? "1" : "0");
    const acct = await api("POST", "/api/wallet/connect", { address, pubkey });
    state.knsName = acct.knsName || null;
    renderWalletBadge();
    await refreshProfile();
    refreshAll();
  }

  function disconnectWallet() {
    state.address = null;
    state.pubkey = null;
    state.isDemoWallet = false;
    state.currentMatchId = null;
    state.currentMatch = null;
    localStorage.removeItem("dagmate_address");
    localStorage.removeItem("dagmate_pubkey");
    localStorage.removeItem("dagmate_demo");
    renderWalletBadge();
    document.getElementById("boardCard").style.display = "none";
    document.getElementById("challengeList").innerHTML = `<p class="muted">Connect a wallet to see challenges.</p>`;
    document.getElementById("matchList").innerHTML = `<p class="muted">Connect a wallet to see your matches.</p>`;
    document.getElementById("learnList").innerHTML = `<p class="muted">Connect a wallet to see learn levels.</p>`;
  }

  async function connectWallet() {
    const provider = window.kasware || window.kastle;
    if (provider && typeof provider.requestAccounts === "function") {
      try {
        const accounts = await provider.requestAccounts();
        const address = accounts && accounts[0];
        if (!address) throw new Error("no account returned");
        let pubkey = null;
        if (typeof provider.getPublicKey === "function") {
          try { pubkey = await provider.getPublicKey(); } catch (_) { /* not all wallets expose this */ }
        }
        await setWallet(address, pubkey, false);
        toast(pubkey ? "Wallet connected." : "Wallet connected — no pubkey exposed, escrow-building will be skipped.");
      } catch (e) {
        toast(`Wallet connect failed: ${e.message}`);
      }
      return;
    }
    toast("No Kasware/Kastle extension detected — using a local demo wallet for testing.");
    try {
      const kp = await api("POST", "/api/dev/demo-wallet");
      await setWallet(kp.address, kp.pubkey, true);
    } catch (e) {
      toast(`Couldn't create a demo wallet: ${e.message}`);
    }
  }

  async function restoreWallet() {
    const address = localStorage.getItem("dagmate_address");
    if (!address) { renderWalletBadge(); return; }
    const pubkey = localStorage.getItem("dagmate_pubkey");
    const isDemo = localStorage.getItem("dagmate_demo") === "1";
    try {
      const acct = await api("POST", "/api/wallet/connect", { address, pubkey });
      state.address = address;
      state.pubkey = pubkey;
      state.isDemoWallet = isDemo;
      state.knsName = acct.knsName || null;
      renderWalletBadge();
      await refreshProfile();
      refreshAll();
    } catch (e) {
      renderWalletBadge();
    }
  }

  async function refreshProfile() {
    if (!state.address) return;
    try {
      state.profile = await api("GET", `/api/profile?address=${encodeURIComponent(state.address)}`);
      document.getElementById("acceptChallengesToggle").checked = state.profile.acceptChallenges;
    } catch (e) { /* not fatal */ }
  }

  document.getElementById("acceptChallengesToggle").addEventListener("change", async (e) => {
    if (!state.address) { e.target.checked = false; toast("Connect a wallet first."); return; }
    try {
      await api("POST", "/api/profile/accept-challenges", { address: state.address, enabled: e.target.checked });
    } catch (err) { toast(err.message); }
  });

  // ── challenges ───────────────────────────────────────────────────────
  document.getElementById("challengeForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!state.address) { toast("Connect a wallet first."); return; }
    const toAddress = document.getElementById("chOpponent").value.trim() || null;
    const gasOnly = document.getElementById("chGasOnly").checked;
    const stakeKas = gasOnly ? 0 : parseFloat(document.getElementById("chStake").value || "0");
    const mode = document.getElementById("chMode").value;
    try {
      await api("POST", "/api/challenges", { fromAddress: state.address, toAddress, stakeKas, mode, gasOnly });
      toast("Challenge created.");
      e.target.reset();
      document.getElementById("chStake").value = 1;
      refreshChallenges();
    } catch (err) { toast(err.message); }
  });

  async function refreshChallenges() {
    const el = document.getElementById("challengeList");
    if (!state.address) { el.innerHTML = `<p class="muted">Connect a wallet to see challenges.</p>`; return; }
    let list;
    try { list = await api("GET", `/api/challenges?address=${encodeURIComponent(state.address)}`); }
    catch (e) { el.innerHTML = `<p class="muted">${e.message}</p>`; return; }
    if (!list.length) { el.innerHTML = `<p class="muted">No open challenges right now.</p>`; return; }
    el.innerHTML = "";
    for (const ch of list) {
      const mine = ch.fromAddress === state.address;
      const div = document.createElement("div");
      div.className = "list-item";
      const fromLabel = ch.fromKns || ch.fromShort;
      const toLabel = ch.toKns || ch.toShort;
      const label = ch.toAddress ? `${fromLabel} → ${toLabel}` : `${fromLabel} (open challenge)`;
      div.innerHTML = `<div><div>${label}</div>
        <div class="meta">${ch.gasOnly ? "gas-only" : `${ch.stakeKas} KAS`} · ${ch.mode}</div></div>
        <div class="actions"></div>`;
      const actions = div.querySelector(".actions");
      if (!mine) {
        const acceptBtn = document.createElement("button");
        acceptBtn.className = "btn btn-primary";
        acceptBtn.textContent = "Accept";
        acceptBtn.addEventListener("click", () => acceptChallenge(ch.id));
        actions.appendChild(acceptBtn);
      }
      const declineBtn = document.createElement("button");
      declineBtn.className = "btn btn-danger";
      declineBtn.textContent = mine ? "Cancel" : "Decline";
      declineBtn.addEventListener("click", () => declineChallenge(ch.id));
      actions.appendChild(declineBtn);
      el.appendChild(div);
    }
  }

  async function acceptChallenge(id) {
    if (!state.pubkey) {
      toast("This wallet has no pubkey on file — escrow can't be built. Use a demo wallet for local testing.");
    }
    try {
      const match = await api("POST", `/api/challenges/${id}/accept`, { address: state.address, pubkey: state.pubkey });
      toast("Challenge accepted — match created.");
      refreshChallenges();
      refreshMatches();
      openMatch(match.id);
    } catch (e) { toast(e.message); }
  }

  async function declineChallenge(id) {
    try { await api("POST", `/api/challenges/${id}/decline`); refreshChallenges(); }
    catch (e) { toast(e.message); }
  }

  // ── matches ──────────────────────────────────────────────────────────
  async function refreshMatches() {
    const el = document.getElementById("matchList");
    if (!state.address) { el.innerHTML = `<p class="muted">Connect a wallet to see your matches.</p>`; return; }
    let list;
    try { list = await api("GET", `/api/matches?address=${encodeURIComponent(state.address)}`); }
    catch (e) { el.innerHTML = `<p class="muted">${e.message}</p>`; return; }
    if (!list.length) { el.innerHTML = `<p class="muted">No matches yet — create or accept a challenge.</p>`; return; }
    el.innerHTML = "";
    for (const m of list) {
      const div = document.createElement("div");
      div.className = "list-item";
      const opp = m.playerA && m.playerA.address === state.address ? m.playerB : m.playerA;
      div.innerHTML = `<div><div>vs ${displayName(opp)}</div>
        <div class="meta">${m.stakeKas} KAS · ${m.mode} · ${m.status}${m.result ? " · " + m.result : ""}</div></div>
        <div class="actions"><button class="btn">Open</button></div>`;
      div.querySelector("button").addEventListener("click", () => openMatch(m.id));
      el.appendChild(div);
    }
  }

  function myColorFor(match) {
    if (!state.address || !match) return null;
    if (match.playerA && match.playerA.address === state.address) return "white";
    if (match.playerB && match.playerB.address === state.address) return "black";
    return null;
  }

  async function openMatch(id) {
    state.currentMatchId = id;
    state.selectedSquare = null;
    await refreshCurrentMatch();
    document.getElementById("boardCard").style.display = "block";
    document.getElementById("boardCard").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function refreshCurrentMatch() {
    if (!state.currentMatchId) return;
    try {
      const m = await api("GET", `/api/matches/${state.currentMatchId}`);
      if (state.currentMatch && state.currentMatch.fen !== m.fen) state.selectedSquare = null;
      state.currentMatch = m;
      state.clockRecvAt = Date.now();
      renderMatchCard();
    } catch (e) { /* keep last known state on transient error */ }
  }

  function renderMatchCard() {
    const m = state.currentMatch;
    if (!m) return;
    document.getElementById("boardTitle").textContent =
      `${displayName(m.playerA)} (white) vs ${displayName(m.playerB)} (black)`;
    document.getElementById("boardMeta").textContent =
      `${m.stakeKas} KAS · ${m.mode} · ${m.status}${m.status === "live" ? ` · ${m.turn} to move` : ""}${m.result ? " · " + m.result : ""}`;
    document.getElementById("fundBtn").style.display = m.status === "awaiting_deposit" ? "inline-block" : "none";
    document.getElementById("resignBtn").style.display = m.status === "live" ? "inline-block" : "none";

    renderClocks(m);
    renderClaim(m);
    renderEscrowInfo(m);

    const moveLog = document.getElementById("moveLog");
    moveLog.textContent = m.winnerAccountId !== undefined && m.result ? `Result: ${m.result}` : "";

    const color = myColorFor(m);
    renderBoard(document.getElementById("board"), m.fen, {
      legalMoves: m.status === "live" && m.turn === color ? m.legalMoves : [],
      selected: state.selectedSquare,
      flipped: color === "black",
      onClick: onBoardSquareClick,
    });
  }

  // Both clocks, laid out like a real board: opponent above, you below.
  // Structure is built here and only the digits are touched by the tick, so a
  // countdown doesn't rebuild DOM four times a second.
  function renderClocks(m) {
    const el = document.getElementById("clocks");
    if (!m.clock) { el.innerHTML = ""; return; }
    const mine = myColorFor(m);
    const order = mine === "black" ? ["white", "black"] : ["black", "white"];
    const who = (col) => (col === "white" ? displayName(m.playerA) : displayName(m.playerB));
    el.innerHTML = order.map((col) => `
      <div class="clock" id="clockRow-${col}">
        <span class="clock-who">${who(col)}${col === mine ? " (you)" : ""} · ${col}</span>
        <span class="clock-time" id="clockTime-${col}">--:--</span>
      </div>`).join("") + `<div class="clock-label">${m.clock.label}</div>`;
    tickClocks();
  }

  function tickClocks() {
    const m = state.currentMatch;
    if (!m || !m.clock) return;
    const c = m.clock;
    const since = c.running ? Math.max(0, Date.now() - state.clockRecvAt) : 0;
    for (const col of ["white", "black"]) {
      const time = document.getElementById(`clockTime-${col}`);
      if (!time) continue;
      const ticking = c.running && c.turn === col;
      const ms = Math.max(0, (col === "white" ? c.whiteMs : c.blackMs) - (ticking ? since : 0));
      time.textContent = fmtClock(ms);
      const row = document.getElementById(`clockRow-${col}`);
      row.classList.toggle("ticking", ticking);
      // Cosmetic only. The server decides the flag; this just stops a player
      // being surprised by it.
      row.classList.toggle("low", ticking && ms <= 30000);
    }
  }

  function fmtClock(ms) {
    const s = Math.ceil(ms / 1000);
    const pad = (n) => String(n).padStart(2, "0");
    if (s >= 86400) return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
    if (s >= 3600) return `${Math.floor(s / 3600)}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
    return `${Math.floor(s / 60)}:${pad(s % 60)}`;
  }

  // ── claiming the pot ─────────────────────────────────────────────────
  // The game is over and the money is still sitting in escrow. Releasing it
  // needs a signature from a player's own wallet, so this panel is the only
  // place funds ever move.
  function renderClaim(m) {
    const el = document.getElementById("claimPanel");
    if (m.status !== "settled" || !myColorFor(m)) { el.innerHTML = ""; return; }
    // Prepared once per match, not once per poll: prepare() builds a real
    // transaction the first time it's called, and the 4s refresh would
    // otherwise hammer the Kaspa sidecar for the whole time the panel is open.
    if (state.settleFor !== m.id) {
      state.settleFor = m.id;
      state.settle = null;
      el.innerHTML = `<div class="claim-title">Checking the pot…</div>`;
      api("POST", `/api/matches/${m.id}/settle/prepare`, { address: state.address })
        .then((s) => { if (state.settleFor === m.id) { state.settle = s; renderClaim(m); } })
        .catch((e) => { el.innerHTML = `<div class="claim-title">Payout</div>
          <div class="claim-note">${e.message}</div>`; });
      return;
    }
    const s = state.settle;
    if (!s) return;

    if (s.state === "broadcast") {
      el.innerHTML = `<div class="claim-title">Paid out</div>
        <div class="claim-amount">${s.payoutKas} KAS</div>
        <div class="claim-note">Released on chain. Transaction <code>${s.txid}</code>.</div>`;
      return;
    }
    const heading = s.isDraw ? "Draw — your half" : (s.youWon ? "You won the pot" : "Payout");
    const needsMe = s.mySignatureInputs && s.mySignatureInputs.length;
    el.innerHTML = `
      <div class="claim-title">${heading}</div>
      <div class="claim-amount">${s.payoutKas} KAS</div>
      ${feeBreakdown(s)}
      ${needsMe ? `<button class="btn btn-primary full" id="claimBtn">Sign &amp; release</button>`
                : `<div class="claim-note">${s.waitingOnOpponent
                     ? "Waiting for your opponent to sign their side of the draw."
                     : "Nothing for you to sign here."}</div>`}`;
    if (needsMe) document.getElementById("claimBtn").addEventListener("click", () => claim(m));
  }

  // Stated every time money is quoted, because "the pot minus a network fee"
  // is a promise and a player should be able to check it against the number
  // above rather than take it on trust. The platform line is driven by the
  // server's own fee config, so it can never say "no cut" while taking one.
  function feeBreakdown(s) {
    return `<div class="claim-fees">
      <div><span>Pot</span><span>${s.potKas} KAS</span></div>
      <div><span>Kaspa network fee</span><span>−${s.networkFeeKas} KAS</span></div>
      <div><span>DAGmate fee</span><span>${s.platformFeeKas > 0
        ? `−${s.platformFeeKas} KAS` : "none"}</span></div>
    </div>`;
  }

  async function claim(m) {
    const s = state.settle;
    const btn = document.getElementById("claimBtn");
    if (btn) { btn.disabled = true; btn.textContent = "Waiting for your wallet…"; }
    let sigs;
    try {
      sigs = await signSettleInputs(s.txJson, s.mySignatureInputs);
    } catch (e) {
      toast(`Signing failed: ${e.message}`);
      if (btn) { btn.disabled = false; btn.textContent = "Sign & release"; }
      return;
    }
    try {
      state.settle = await api("POST", `/api/matches/${m.id}/settle/submit`,
                               { address: state.address, sigs });
      renderClaim(m);
      toast(state.settle.txid ? "Payout broadcast." : "Signed — waiting on your opponent.");
    } catch (e) {
      toast(`Payout failed: ${e.message}`);
      if (btn) { btn.disabled = false; btn.textContent = "Sign & release"; }
    }
  }

  // The one place a player's key is asked for anything. The escrow is a custom
  // P2SH script, so this needs a wallet's custom-script signing API (Kasware
  // `signPskt`, Kastle `signTx` with `scripts[]`) — a plain "send" API can't
  // produce these signatures. The demo wallet deliberately cannot do it: it
  // exists to click through the game locally, and letting it anywhere near a
  // real payout is exactly the shortcut that ends up shipping.
  async function signSettleInputs(txJson, indexes) {
    const provider = window.kasware || window.kastle;
    if (!provider) throw new Error("connect Kasware or Kastle to release the pot");
    const out = {};
    if (typeof provider.signPskt === "function") {
      for (const i of indexes) out[i] = await provider.signPskt(txJson, { inputIndex: i });
      return out;
    }
    if (typeof provider.signTx === "function") {
      for (const i of indexes) out[i] = await provider.signTx(txJson, { inputIndex: i });
      return out;
    }
    throw new Error("this wallet doesn't expose custom-script signing yet");
  }

  // Escrow addresses + live deposit progress. The backend watches both
  // addresses on chain and starts the match itself once each holds its stake,
  // so this is a read-only view of that — there is nothing to click.
  function renderEscrowInfo(m) {
    const el = document.getElementById("escrowInfo");
    const f = m.funding;
    if (!f || m.status !== "awaiting_deposit") {
      el.innerHTML = `escrow A: ${m.escrowA || "(pending)"}<br>escrow B: ${m.escrowB || "(pending)"}`;
      return;
    }
    const side = (label, addr, paid, got) => `
      <div class="deposit-row">
        <span class="deposit-tick ${paid ? "ok" : ""}">${paid ? "✓" : "…"}</span>
        <div>
          <div>${label} — ${paid ? "stake received" : `${got} / ${f.stakeKas} KAS`}</div>
          <div class="deposit-addr">${addr || "(pending)"}</div>
        </div>
      </div>`;
    el.innerHTML =
      side(`${displayName(m.playerA)} (white)`, m.escrowA, f.aFunded, f.aKas) +
      side(`${displayName(m.playerB)} (black)`, m.escrowB, f.bFunded, f.bKas) +
      `<div class="meta">Send at least ${f.stakeKas} KAS to your own escrow address. The match
       starts automatically once both stakes confirm. If both sides haven't funded within
       ${f.windowMins} minutes the match is cancelled, and any stake you sent stays yours.</div>`;
  }

  function onBoardSquareClick(sq) {
    const m = state.currentMatch;
    if (!m || m.status !== "live") return;
    const color = myColorFor(m);
    if (!color) { toast("You're not a player in this match."); return; }
    if (m.turn !== color) { toast("Not your move."); return; }
    const board = fenToBoard(m.fen);
    const piece = pieceAt(board, sq);

    if (state.selectedSquare) {
      const uci = findUci(m.legalMoves, state.selectedSquare, sq);
      if (uci) { state.selectedSquare = null; submitMove(uci); return; }
      if (isMyPiece(piece, color)) { state.selectedSquare = sq; renderMatchCard(); return; }
      state.selectedSquare = null; renderMatchCard(); return;
    }
    if (isMyPiece(piece, color)) { state.selectedSquare = sq; renderMatchCard(); }
  }

  async function submitMove(uci) {
    try {
      const m = await api("POST", `/api/matches/${state.currentMatchId}/move`, { address: state.address, uci });
      state.currentMatch = m;
      renderMatchCard();
      refreshMatches();
    } catch (e) { toast(e.message); }
  }

  document.getElementById("resignBtn").addEventListener("click", async () => {
    if (!state.currentMatchId) return;
    try {
      const m = await api("POST", `/api/matches/${state.currentMatchId}/resign`, { address: state.address });
      state.currentMatch = m;
      renderMatchCard();
      refreshMatches();
    } catch (e) { toast(e.message); }
  });

  document.getElementById("fundBtn").addEventListener("click", async () => {
    if (!state.currentMatchId) return;
    try {
      await api("POST", `/api/matches/${state.currentMatchId}/dev-mark-funded`);
      toast("Marked funded (dev-only — no real deposit check).");
      refreshCurrentMatch();
      refreshMatches();
    } catch (e) { toast(e.message); }
  });

  // ── tournaments ──────────────────────────────────────────────────────
  async function refreshTournaments() {
    const el = document.getElementById("tournamentList");
    let list;
    try { list = await api("GET", "/api/tournaments"); }
    catch (e) { el.innerHTML = `<p class="muted">${e.message}</p>`; return; }
    el.innerHTML = "";
    for (const t of list) {
      const div = document.createElement("div");
      div.className = "list-item";
      div.innerHTML = `<div><div>${t.tierKas} KAS tier</div>
        <div class="meta">${t.entrants}/${t.minEntrants} entrants · ${t.status}</div></div>
        <div class="actions"><button class="btn btn-primary">Join</button></div>`;
      div.querySelector("button").addEventListener("click", () => joinTournament(t.tierKas));
      el.appendChild(div);
    }
  }

  async function joinTournament(tierKas) {
    if (!state.address) { toast("Connect a wallet first."); return; }
    try {
      const r = await api("POST", `/api/tournaments/${tierKas}/join`, { address: state.address });
      toast(r.alreadyJoined ? "Already in this lobby." : r.started ? "Lobby full — tournament started!" : "Joined lobby.");
      refreshTournaments();
      if (r.started) refreshMatches();
    } catch (e) { toast(e.message); }
  }

  // ── learn ────────────────────────────────────────────────────────────
  // Level bodies deliberately do NOT live here — they're fetched per level from
  // /api/learn/levels/{id}/content, so a locked level can't be read from source.
  async function refreshLearn() {
    const el = document.getElementById("learnList");
    document.getElementById("learnContentCard").style.display = "none";
    if (!state.address) { el.innerHTML = `<p class="muted">Connect a wallet to see learn levels.</p>`; return; }
    await refreshProfile();
    if (!state.profile) { el.innerHTML = `<p class="muted">Couldn't load your profile.</p>`; return; }
    el.innerHTML = "";
    for (const tier of state.profile.learnTiers) {
      const levels = state.profile.learnLevels.filter((lv) => lv.tier === tier.key);
      if (!levels.length) continue;
      const done = levels.filter((lv) => lv.unlocked).length;
      const head = document.createElement("div");
      head.className = "tier-head";
      head.innerHTML = `<div><div class="tier-label">${tier.label}</div>
        <div class="meta">${tier.blurb}</div></div>
        <span class="badge">${done}/${levels.length}</span>`;
      el.appendChild(head);
      for (const lv of levels) {
        const div = document.createElement("div");
        div.className = "list-item";
        div.innerHTML = `<div><div>${lv.title}</div>
          <div class="meta">${lv.summary} ${lv.gas_kas ? "· " + lv.gas_kas + " KAS gas" : "· free"}</div></div>
          <div class="actions"><span class="badge ${lv.unlocked ? "unlocked" : "locked"}">${lv.unlocked ? "unlocked" : "locked"}</span>
            <button class="btn">${lv.unlocked ? "Open" : "Unlock"}</button></div>`;
        div.querySelector("button").addEventListener("click", () => lv.unlocked ? openLevel(lv) : unlockLevel(lv));
        el.appendChild(div);
      }
    }
  }

  async function openLevel(lv) {
    const card = document.getElementById("learnContentCard");
    card.style.display = "block";
    document.getElementById("learnContentTitle").textContent = lv.title;
    const body = document.getElementById("learnContentBody");
    body.innerHTML = `<p class="muted">Loading…</p>`;
    card.scrollIntoView({ behavior: "smooth", block: "nearest" });
    try {
      const r = await api("GET", `/api/learn/levels/${lv.id}/content?address=${encodeURIComponent(state.address)}`);
      body.innerHTML = r.body;
    } catch (e) {
      body.innerHTML = `<p class="muted">${e.message}</p>`;
    }
  }

  async function unlockLevel(lv) {
    try {
      await api("POST", `/api/learn/levels/${lv.id}/unlock`, { address: state.address });
      toast(lv.gas_kas ? `Unlocked (${lv.gas_kas} KAS gas — recorded, not yet chain-verified in this test build).` : "Unlocked.");
      await refreshLearn();
    } catch (e) { toast(e.message); }
  }

  // ── practice ─────────────────────────────────────────────────────────
  async function practiceInit() {
    state.practiceFen = STANDARD_FEN;
    state.practiceSelected = null;
    state.practiceTurnLock = false;
    await refreshPracticeLegalMoves();
    renderPracticeBoard();
    document.getElementById("practiceStatus").textContent = "Your move — you're playing white.";
  }

  async function initPracticeLevels() {
    const sel = document.getElementById("practiceLevel");
    let levels, def;
    try {
      const r = await api("GET", "/api/practice/levels");
      levels = r.levels; def = r.default;
    } catch (e) { return; }
    state.practiceLevels = levels;
    // A saved level is only honoured if the backend still offers it.
    const saved = localStorage.getItem("dagmate.practiceLevel");
    state.practiceLevel = levels.some((l) => l.key === saved) ? saved : def;
    sel.innerHTML = "";
    for (const lv of levels) {
      const opt = document.createElement("option");
      opt.value = lv.key;
      opt.textContent = lv.label;
      sel.appendChild(opt);
    }
    sel.value = state.practiceLevel;
    renderPracticeLevelBlurb();
    sel.addEventListener("change", () => {
      state.practiceLevel = sel.value;
      localStorage.setItem("dagmate.practiceLevel", sel.value);
      renderPracticeLevelBlurb();
    });
  }

  function renderPracticeLevelBlurb() {
    const lv = (state.practiceLevels || []).find((l) => l.key === state.practiceLevel);
    document.getElementById("practiceLevelBlurb").textContent = lv ? lv.blurb : "";
  }

  async function refreshPracticeLegalMoves() {
    try {
      const r = await api("GET", `/api/practice/legal-moves?fen=${encodeURIComponent(state.practiceFen)}`);
      state.practiceLegalMoves = r.legalMoves;
    } catch (e) { state.practiceLegalMoves = []; }
  }

  function renderPracticeBoard() {
    const turn = state.practiceFen.split(" ")[1] === "w" ? "white" : "black";
    renderBoard(document.getElementById("practiceBoard"), state.practiceFen, {
      legalMoves: turn === "white" && !state.practiceTurnLock ? state.practiceLegalMoves : [],
      selected: state.practiceSelected,
      onClick: practiceSquareClick,
    });
  }

  function practiceSquareClick(sq) {
    if (state.practiceTurnLock) return;
    const turn = state.practiceFen.split(" ")[1] === "w" ? "white" : "black";
    if (turn !== "white") return;
    const board = fenToBoard(state.practiceFen);
    const piece = pieceAt(board, sq);
    if (state.practiceSelected) {
      const uci = findUci(state.practiceLegalMoves, state.practiceSelected, sq);
      if (uci) { state.practiceSelected = null; practiceApplyMove(uci); return; }
      if (isMyPiece(piece, "white")) { state.practiceSelected = sq; renderPracticeBoard(); return; }
      state.practiceSelected = null; renderPracticeBoard(); return;
    }
    if (isMyPiece(piece, "white")) { state.practiceSelected = sq; renderPracticeBoard(); }
  }

  async function practiceApplyMove(uci) {
    try {
      const status = await api("POST", "/api/practice/apply-move", { fen: state.practiceFen, uci });
      state.practiceFen = status.fen;
      updatePracticeStatus(status);
      await refreshPracticeLegalMoves();
      renderPracticeBoard();
      if (!status.game_over) {
        state.practiceTurnLock = true;
        document.getElementById("practiceStatus").textContent = "Bot thinking…";
        setTimeout(async () => {
          try {
            const r = await api("POST", "/api/practice/bot-move", { fen: state.practiceFen, level: state.practiceLevel });
            if (r.uci) { state.practiceFen = r.status.fen; updatePracticeStatus(r.status); }
          } catch (e) { toast(e.message); }
          state.practiceTurnLock = false;
          await refreshPracticeLegalMoves();
          renderPracticeBoard();
        }, 350);
      }
    } catch (e) { toast(e.message); }
  }

  function updatePracticeStatus(status) {
    const el = document.getElementById("practiceStatus");
    if (status.game_over) {
      el.textContent = status.result === "checkmate"
        ? `Checkmate — ${status.winner_color === "white" ? "you win!" : "bot wins."}`
        : `Draw (${status.result}).`;
    } else {
      el.textContent = status.turn === "white" ? "Your move." : "Bot thinking…";
    }
  }

  document.getElementById("practiceResetBtn").addEventListener("click", practiceInit);

  // ── global refresh ──────────────────────────────────────────────────
  function refreshAll() {
    refreshChallenges();
    refreshMatches();
    if (document.getElementById("panel-tournaments").classList.contains("active")) refreshTournaments();
    if (document.getElementById("panel-learn").classList.contains("active")) refreshLearn();
  }

  setInterval(() => {
    if (!state.address) return;
    refreshChallenges();
    refreshMatches();
    if (state.currentMatchId) refreshCurrentMatch();
  }, 4000);

  // The clock redraws far more often than the match is re-fetched — the poll
  // above only corrects it, it doesn't drive it.
  setInterval(tickClocks, 250);

  // Browsers freeze timers in a backgrounded tab, so both the countdown and the
  // 4s poll stop dead there. The server clock never stopped, so on the way back
  // the page would show time the player no longer has — in a wagered game that
  // is the difference between "I have two minutes" and "I already lost". Resync
  // immediately rather than waiting for the next tick.
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && state.currentMatchId) refreshCurrentMatch();
  });

  // The fee promise, stated in the player's own words rather than a policy
  // page nobody opens. Both strings are generated from the server's fee config
  // so that turning a rake on would change the wording automatically instead of
  // leaving a claim on the page that is no longer true.
  async function renderFeeDisclaimer() {
    let meta;
    try { meta = await api("GET", "/api/meta"); } catch (e) { return; }
    const f = meta.fees;
    document.getElementById("feeDisclaimer").textContent = f.takesCut
      ? `DAGmate takes ${(f.platformFeeBps / 100).toFixed(2)}% of each pot. `
        + "You also pay the Kaspa network fee to release it."
      : "DAGmate takes no cut of any pot. You pay only the stake you agreed, the "
        + "tournament entry you chose, or gas — the winner receives the whole pot "
        + "minus the Kaspa network fee to release it.";
    document.getElementById("challengeFeeNote").textContent = f.takesCut
      ? `You pay your stake. DAGmate takes ${(f.platformFeeBps / 100).toFixed(2)}% of the pot on settlement.`
      : "You pay your stake and nothing else — DAGmate takes no cut of the pot.";
  }

  // ── boot ─────────────────────────────────────────────────────────────
  renderWalletBadge();
  restoreWallet();
  refreshTournaments();
  renderFeeDisclaimer();
  initPracticeLevels().then(practiceInit);
})();
