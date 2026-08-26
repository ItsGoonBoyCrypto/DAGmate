/* DAGmate — frontend app (docs/DAGMATE_SPEC.md). Plain JS, no build step.
 * Wallet-connect uses the real window.kasware / window.kastle injected APIs
 * (requestAccounts()) when a browser extension is present. When none is
 * detected — e.g. this headless preview — it falls back to the backend's
 * dev-only demo wallet (POST /api/dev/demo-wallet), clearly labeled as such.
 * That fallback is NEVER part of the real signing/settlement path, and it
 * only appears when the server says the dev routes are on (GET /api/meta →
 * `devRoutes`, off by default and impossible on mainnet). The UI asks rather
 * than assuming, so nothing here has to be remembered at deploy time.
 *
 * Connecting is not the same as being signed in. Connecting only reveals an
 * address, which is public anyway; every call that changes something carries
 * a session token earned by SIGNING a server nonce with that wallet. So
 * `state.address` is for display, and `state.session` is what the server
 * actually believes — never send an address expecting it to be treated as
 * identity, because the backend now ignores it (see site/backend/auth.py).
 */
(() => {
  "use strict";

  const STANDARD_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
  const PIECE_NAME = { p: "pawn", n: "knight", b: "bishop", r: "rook", q: "queen", k: "king" };
  // Above this stake, creating a challenge asks for a confirmation naming the
  // amount (F12). A UX guard only — the backend enforces the real stake bounds.
  const STAKE_CONFIRM_ABOVE_KAS = 100;

  function displayName(obj) {
    if (!obj) return "?";
    return obj.knsName || obj.shortAddress || obj.address || "?";
  }

  /* HTML-escape anything before it goes into an innerHTML string. The one
   * value here that comes from OUTSIDE our own server is the KNS name — it's
   * fetched from a third-party naming API, attached to every account, and open
   * challenges (with their names) are broadcast to every signed-in player and
   * re-rendered every few seconds. So one hostile ".kas" name would otherwise
   * land in everyone's DOM with zero interaction, and the obvious payload reads
   * the session token out of localStorage. Every place a name (or any string we
   * didn't generate) is interpolated into innerHTML runs through esc(). Nodes
   * built with textContent/createElement don't need it — the browser doesn't
   * parse those as markup. */
  function esc(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  /* Format an integer sompi amount (as a decimal string) into a KAS string
   * WITHOUT floating point. Dividing sompi by 1e8 in JS gives things like
   * 4.999999999999999 on a halved pot, or 0.00001 / exponential notation on
   * dust — in the biggest number on the screen, next to a "no cut" promise.
   * BigInt math keeps it exact; trailing zeros are trimmed. Falls back to the
   * raw input if it isn't a clean integer string. 1 KAS = 100,000,000 sompi. */
  function kasFromSompi(sompi) {
    let n;
    try { n = BigInt(sompi); } catch (_) { return String(sompi == null ? "" : sompi); }
    const neg = n < 0n; if (neg) n = -n;
    const whole = (n / 100000000n).toString();
    const frac = (n % 100000000n).toString().padStart(8, "0").replace(/0+$/, "");
    return (neg ? "-" : "") + whole + (frac ? "." + frac : "");
  }

  const state = {
    address: null,
    knsName: null,
    isDemoWallet: false,
    // The proof, not the claim. Everything that changes state needs this.
    session: null,
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
    reclaim: null,
    reclaimFor: null,
    // True while a wallet signature popup is open for a payout/reclaim. The 4s
    // poll skips rebuilding the match card while it's set, so the money button
    // the signing handler is holding can't be torn out and silently re-enabled
    // underneath the wallet prompt (a click-through trap on a signing dialog).
    signing: false,
    // Monotonic id for match fetches: a slow poll that resolves after a newer
    // one must not stamp its stale snapshot (with a fresh clockRecvAt) over the
    // current board — that would show the player more time than they have (F9).
    pollSeq: 0,
    // True while a move POST is in flight, so a poll that started before the
    // move can't overwrite the post-move state with the position it's replacing.
    moveInFlight: false,
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
    if (state.session) opts.headers["Authorization"] = `Bearer ${state.session}`;
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const r = await fetch(path, opts);
    let data = null;
    try { data = await r.json(); } catch (_) { /* no body */ }
    // A dead session looks identical to never having signed in, so treat it
    // that way rather than leaving the UI showing a wallet the server has
    // stopped recognising.
    if (r.status === 401 && state.session) forgetSession();
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
    area.innerHTML = `<span class="wallet-badge">${state.isDemoWallet ? "🧪 demo · " : ""}${esc(label)}</span>
      <button class="btn" id="disconnectBtn">Disconnect</button>`;
    document.getElementById("disconnectBtn").addEventListener("click", disconnectWallet);
  }

  /* Sign in: prove the wallet is ours by signing a one-time server nonce.
   *
   * `signMessage` (rather than signing a transaction) is what both Kasware
   * and Kastle expose, and it's the honest thing to ask for — the popup says
   * a message is being signed, and the message itself says in plain words
   * that nothing is being spent. A login that asked players to sign a
   * transaction would be teaching them a habit that gets them robbed
   * somewhere else.
   */
  async function signIn(address, pubkey, isDemo, sign) {
    const challenge = await api("POST", "/api/auth/nonce", { address });
    const signature = await sign(challenge.message);
    const r = await api("POST", "/api/auth/verify",
                        { address, pubkey, nonce: challenge.nonce, signature });
    state.session = r.token;
    state.address = address;
    state.isDemoWallet = isDemo;
    state.knsName = (r.account && r.account.knsName) || null;
    localStorage.setItem("dagmate_session", r.token);
    localStorage.setItem("dagmate_demo", isDemo ? "1" : "0");
    renderWalletBadge();
    await refreshProfile();
    refreshAll();
  }

  /** Drop local sign-in state without telling the server (it already
   *  disagrees, or we're tearing down after a 401). */
  function forgetSession() {
    state.session = null;
    state.address = null;
    state.isDemoWallet = false;
    state.currentMatchId = null;
    state.currentMatch = null;
    state.settle = null;
    state.settleFor = null;
    state.reclaim = null;
    state.reclaimFor = null;
    localStorage.removeItem("dagmate_session");
    localStorage.removeItem("dagmate_demo");
    localStorage.removeItem("dagmate_demo_key");
    renderWalletBadge();
    document.getElementById("boardCard").style.display = "none";
    document.getElementById("challengeList").innerHTML = `<p class="muted">Connect a wallet to see challenges.</p>`;
    document.getElementById("matchList").innerHTML = `<p class="muted">Connect a wallet to see your matches.</p>`;
    document.getElementById("learnList").innerHTML = `<p class="muted">Connect a wallet to see learn levels.</p>`;
  }

  function disconnectWallet() {
    // Revoke server-side too, so a copied token doesn't outlive the click.
    api("POST", "/api/auth/logout").catch(() => {});
    forgetSession();
  }

  async function connectWallet() {
    const provider = window.kasware || window.kastle;
    if (provider && typeof provider.requestAccounts === "function") {
      try {
        const accounts = await provider.requestAccounts();
        const address = accounts && accounts[0];
        if (!address) throw new Error("no account returned");
        if (typeof provider.getPublicKey !== "function") {
          throw new Error("this wallet doesn't expose its public key, which DAGmate needs to build your escrow");
        }
        const pubkey = await provider.getPublicKey();
        if (typeof provider.signMessage !== "function") {
          throw new Error("this wallet can't sign messages, so it can't prove the address is yours");
        }
        await signIn(address, pubkey, false, (msg) => provider.signMessage(msg));
        toast("Signed in.");
      } catch (e) {
        toast(`Sign-in failed: ${e.message}`);
      }
      return;
    }
    // No extension. On a real deployment that's the end of the story — say so
    // plainly rather than letting the request 404 and reporting it as a
    // failure, because "you need a wallet" is the actual answer.
    if (!state.meta || !state.meta.devRoutes) {
      toast("No Kaspa wallet detected. Install Kasware or Kastle to play — DAGmate never holds your keys.");
      return;
    }
    toast("No Kasware/Kastle extension detected — using a local demo wallet for testing.");
    try {
      const kp = await api("POST", "/api/dev/demo-wallet");
      // Note this goes through the identical handshake — the demo wallet is a
      // stand-in for the extension, not a way around the login.
      localStorage.setItem("dagmate_demo_key", kp.privateKeyHex);
      await signIn(kp.address, kp.pubkey, true, async (msg) => {
        const r = await api("POST", "/api/dev/demo-sign",
                            { privateKeyHex: kp.privateKeyHex, message: msg });
        return r.signature;
      });
    } catch (e) {
      toast(`Couldn't create a demo wallet: ${e.message}`);
    }
  }

  async function restoreWallet() {
    const token = localStorage.getItem("dagmate_session");
    if (!token) { renderWalletBadge(); return; }
    state.session = token;
    state.isDemoWallet = localStorage.getItem("dagmate_demo") === "1";
    try {
      // The session itself is the source of truth for who we are — the
      // address is read back from the server rather than trusted from
      // localStorage, so an edited value can't mislabel the UI.
      const p = await api("GET", "/api/profile");
      state.address = p.address;
      state.knsName = p.knsName || null;
      state.profile = p;
      renderWalletBadge();
      document.getElementById("acceptChallengesToggle").checked = p.acceptChallenges;
      refreshAll();
    } catch (e) {
      forgetSession();
    }
  }

  async function refreshProfile() {
    if (!state.session) return;
    try {
      state.profile = await api("GET", "/api/profile");
      document.getElementById("acceptChallengesToggle").checked = state.profile.acceptChallenges;
    } catch (e) { /* not fatal */ }
  }

  document.getElementById("acceptChallengesToggle").addEventListener("change", async (e) => {
    if (!state.session) { e.target.checked = false; toast("Connect a wallet first."); return; }
    try {
      await api("POST", "/api/profile/accept-challenges", { enabled: e.target.checked });
    } catch (err) { toast(err.message); }
  });

  document.getElementById("telegramLinkForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!state.session) { toast("Connect a wallet first."); return; }
    const input = document.getElementById("tgLinkCode");
    const code = input.value.trim();
    if (!code) { toast("Paste the code the bot gave you."); return; }
    const btn = e.target.querySelector("button");
    btn.disabled = true;
    try {
      await api("POST", "/api/telegram/link", { code });
      toast("Telegram linked — you'll get match alerts now.");
      input.value = "";
    } catch (err) {
      toast(err.message);
    } finally {
      btn.disabled = false;
    }
  });

  // ── challenges ───────────────────────────────────────────────────────
  document.getElementById("challengeForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!state.session) { toast("Connect a wallet first."); return; }
    const toAddress = document.getElementById("chOpponent").value.trim() || null;
    const stakeKas = parseFloat(document.getElementById("chStake").value || "0");
    const mode = document.getElementById("chMode").value;
    // A staked challenge locks real KAS into escrow the moment it's accepted, so
    // a large one gets a confirm naming the amount — the backend caps it too,
    // this just stops a fat-fingered number before it's posted.
    if (stakeKas >= STAKE_CONFIRM_ABOVE_KAS &&
        !window.confirm(`Create a challenge staking ${stakeKas} KAS? You'll stake that amount to play if it's accepted.`)) {
      return;
    }
    try {
      await api("POST", "/api/challenges", { toAddress, stakeKas, mode });
      toast("Challenge created.");
      e.target.reset();
      document.getElementById("chStake").value = 1;
      refreshChallenges();
    } catch (err) { toast(err.message); }
  });

  async function refreshChallenges() {
    const el = document.getElementById("challengeList");
    if (!state.session) { el.innerHTML = `<p class="muted">Connect a wallet to see challenges.</p>`; return; }
    let list;
    try { list = await api("GET", "/api/challenges"); }
    catch (e) { el.innerHTML = `<p class="muted">${esc(e.message)}</p>`; return; }
    if (!list.length) { el.innerHTML = `<p class="muted">No open challenges right now.</p>`; return; }
    el.innerHTML = "";
    for (const ch of list) {
      const mine = ch.fromAddress === state.address;
      const div = document.createElement("div");
      div.className = "list-item";
      const fromLabel = esc(ch.fromKns || ch.fromShort);
      const toLabel = esc(ch.toKns || ch.toShort);
      const label = ch.toAddress ? `${fromLabel} → ${toLabel}` : `${fromLabel} (open challenge)`;
      div.innerHTML = `<div><div>${label}</div>
        <div class="meta">${esc(ch.stakeKas)} KAS · ${esc(ch.mode)}</div></div>
        <div class="actions"></div>`;
      const actions = div.querySelector(".actions");
      if (!mine) {
        const acceptBtn = document.createElement("button");
        acceptBtn.className = "btn btn-primary";
        acceptBtn.textContent = "Accept";
        acceptBtn.addEventListener("click", (e) => acceptChallenge(ch.id, e.currentTarget));
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

  async function acceptChallenge(id, btn) {
    if (state.profile && !state.profile.hasPubkey) {
      toast("This wallet has no pubkey on file — escrow can't be built. Use a demo wallet for local testing.");
    }
    // Disable immediately so a double-click can't fire two accepts. The server
    // now also guards this (one open->accepting transition wins; the loser gets
    // a 409), so a stale re-enabled button from a background poll can't mint a
    // second match either — this is the fast local half of that guard.
    if (btn) btn.disabled = true;
    try {
      const match = await api("POST", `/api/challenges/${id}/accept`);
      toast("Challenge accepted — match created.");
      refreshChallenges();
      refreshMatches();
      openMatch(match.id);
    } catch (e) {
      toast(e.message);
      if (btn) btn.disabled = false;
    }
  }

  async function declineChallenge(id) {
    try { await api("POST", `/api/challenges/${id}/decline`); refreshChallenges(); }
    catch (e) { toast(e.message); }
  }

  // ── matches ──────────────────────────────────────────────────────────
  async function refreshMatches() {
    const el = document.getElementById("matchList");
    if (!state.session) { el.innerHTML = `<p class="muted">Connect a wallet to see your matches.</p>`; return; }
    let list;
    try { list = await api("GET", "/api/matches"); }
    catch (e) { el.innerHTML = `<p class="muted">${esc(e.message)}</p>`; return; }
    if (!list.length) { el.innerHTML = `<p class="muted">No matches yet — create or accept a challenge.</p>`; return; }
    el.innerHTML = "";
    for (const m of list) {
      const div = document.createElement("div");
      div.className = "list-item";
      const opp = m.playerA && m.playerA.address === state.address ? m.playerB : m.playerA;
      div.innerHTML = `<div><div>vs ${esc(displayName(opp))}</div>
        <div class="meta">${esc(m.stakeKas)} KAS · ${esc(m.mode)} · ${esc(m.status)}${m.result ? " · " + esc(m.result) : ""}</div></div>
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

  // The id of the match the board is ACTUALLY showing right now. Every action
  // button reads this, never state.currentMatchId, so a move/resign/draw can
  // only ever fire at the game on screen — even if a fetch for a different
  // match is mid-flight.
  function shownMatchId() {
    return state.currentMatch ? state.currentMatch.id : null;
  }

  async function openMatch(id) {
    // Fetch the target BEFORE binding the board to it. If this fails we must
    // not switch: leaving the board showing the old match while the id points
    // at a new one is exactly how Resign/move/draw fire at the wrong game.
    // currentMatchId and currentMatch are only ever set together, on success.
    let m;
    try {
      m = await api("GET", `/api/matches/${id}`);
    } catch (e) {
      toast(`Couldn't open that match: ${e.message}`);
      return;
    }
    state.currentMatchId = id;
    state.currentMatch = m;
    state.selectedSquare = null;
    state.clockRecvAt = Date.now();
    // New match on screen — the payout/reclaim panels must recompute for it.
    state.settleFor = null;
    state.reclaimFor = null;
    renderMatchCard();
    document.getElementById("boardCard").style.display = "block";
    document.getElementById("boardCard").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function refreshCurrentMatch() {
    if (!state.currentMatchId) return;
    // Don't rebuild the board/panels while a wallet signature is in flight —
    // the poll would tear out the button the signing handler is holding (F5).
    if (state.signing) return;
    // Don't fetch a snapshot a move is about to supersede (F9).
    if (state.moveInFlight) return;
    const seq = ++state.pollSeq;
    try {
      const m = await api("GET", `/api/matches/${state.currentMatchId}`);
      // Drop this response if a newer fetch (or a move) has since superseded it,
      // or if we've navigated to a different match — either way it's stale and
      // must not stamp its clock snapshot over what's on screen (F9).
      if (seq !== state.pollSeq) return;
      if (m.id !== state.currentMatchId) return;
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
    // Absent entirely once the dev routes are off (see loadMeta).
    const fundBtn = document.getElementById("fundBtn");
    if (fundBtn) fundBtn.style.display = m.status === "awaiting_deposit" ? "inline-block" : "none";
    document.getElementById("resignBtn").style.display = m.status === "live" ? "inline-block" : "none";
    // Hidden while an offer is on the board: whoever made it can't make it
    // twice, and whoever received it should be answering the panel below
    // rather than firing a second offer back.
    document.getElementById("drawBtn").style.display =
      m.status === "live" && myColorFor(m) && !m.drawOffer ? "inline-block" : "none";

    renderDrawOffer(m);
    renderClocks(m);
    renderClaim(m);
    renderReclaim(m);
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

  // ── draw offers ──────────────────────────────────────────────────────
  // A draw splits the pot, so the panel says so out loud: Accept is a money
  // decision, not a UI convenience, and a player should never click it
  // thinking it only ends the game.
  function renderDrawOffer(m) {
    const el = document.getElementById("drawPanel");
    const mine = myColorFor(m);
    if (m.status !== "live" || !mine || !m.drawOffer) { el.innerHTML = ""; return; }
    if (m.drawOffer.byColor === mine) {
      el.innerHTML = `<div class="claim-title">Draw offered</div>
        <div class="claim-note">Waiting for your opponent. Playing a move withdraws it.</div>
        <button class="btn full" id="drawWithdrawBtn">Withdraw offer</button>`;
      document.getElementById("drawWithdrawBtn")
        .addEventListener("click", () => drawAction("decline", "Offer withdrawn."));
      return;
    }
    el.innerHTML = `<div class="claim-title">Draw offered</div>
      <div class="claim-note">Your opponent offers a draw. Accept and the match ends level —
        each stake goes back to whoever put it in, minus the network fee.
        <strong>Both players then sign to release their own stake;</strong> if your
        opponent doesn't sign, your stake is still yours but waits in escrow until
        the reclaim timelock.</div>
      <button class="btn btn-primary full" id="drawAcceptBtn">Accept draw</button>
      <button class="btn full" id="drawDeclineBtn">Decline</button>`;
    document.getElementById("drawAcceptBtn")
      .addEventListener("click", () => drawAction("accept", "Draw agreed."));
    document.getElementById("drawDeclineBtn")
      .addEventListener("click", () => drawAction("decline", "Draw declined."));
  }

  // Re-reads the match rather than keeping the POST's own body: offering and
  // declining leave the game LIVE, and the mutating endpoints don't return
  // `legalMoves` (only GET and /move do), so trusting the response would
  // freeze the board until the next poll.
  async function drawAction(what, okMsg) {
    const id = shownMatchId();
    if (!id) return;
    try {
      await api("POST", `/api/matches/${id}/draw/${what}`);
      await refreshCurrentMatch();
      refreshMatches();
      toast(okMsg);
    } catch (e) { toast(e.message); }
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
        <span class="clock-who">${esc(who(col))}${col === mine ? " (you)" : ""} · ${col}</span>
        <span class="clock-time" id="clockTime-${col}">--:--</span>
      </div>`).join("") + `<div class="clock-label">${esc(m.clock.label)}</div>`;
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
  // A prepare() (settle or reclaim) can fail on a transient sidecar blip. The
  // old code left the *For flag set and never retried, so one blip stranded the
  // winner's payout panel until a full page reload — which the UI never
  // suggested. This renders the error with a real "Try again" button and uses
  // textContent for the message (never innerHTML — a server error string is not
  // ours to trust as markup). The scoped querySelector avoids id collisions
  // between the settle and reclaim panels showing errors at once.
  function showPrepareError(el, title, err, onRetry, extraHtml) {
    el.innerHTML = `<div class="claim-title">${esc(title)}</div>
      <div class="claim-note prep-err"></div>
      <button class="btn full prep-retry">Try again</button>${extraHtml || ""}`;
    el.querySelector(".prep-err").textContent = err && err.message ? err.message : String(err);
    el.querySelector(".prep-retry").addEventListener("click", onRetry);
  }
  function retrySettle(m) { state.settleFor = null; renderClaim(m); }
  function retryReclaim(m) { state.reclaimFor = null; renderReclaim(m); }

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
      api("POST", `/api/matches/${m.id}/settle/prepare`)
        .then((s) => { if (state.settleFor === m.id) { state.settle = s; renderClaim(m); } })
        .catch((e) => { if (state.settleFor === m.id) showPrepareError(el, "Payout", e, () => retrySettle(m)); });
      return;
    }
    const s = state.settle;
    // settle is null but the match is still selected → a prepare that failed and
    // left the error panel up (see showPrepareError). Don't auto-retry every
    // poll; the "Try again" button drives it.
    if (!s) return;

    if (s.state === "broadcast") {
      el.innerHTML = `<div class="claim-title">Paid out</div>
        <div class="claim-amount">${kasFromSompi(s.payoutSompi)} KAS</div>
        <div class="claim-note">Released on chain. Transaction <code>${esc(s.txid)}</code>.</div>`;
      return;
    }
    const heading = s.isDraw ? "Draw — your half" : (s.youWon ? "You won the pot" : "Payout");
    const needsMe = s.mySignatureInputs && s.mySignatureInputs.length;
    el.innerHTML = `
      <div class="claim-title">${heading}</div>
      <div class="claim-amount">${kasFromSompi(s.payoutSompi)} KAS</div>
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
    const hasRake = s.platformFeeSompi != null ? s.platformFeeSompi !== "0" : s.platformFeeKas > 0;
    return `<div class="claim-fees">
      <div><span>Pot</span><span>${kasFromSompi(s.potSompi)} KAS</span></div>
      <div><span>Kaspa network fee</span><span>−${kasFromSompi(s.networkFeeSompi)} KAS</span></div>
      <div><span>DAGmate fee</span><span>${hasRake
        ? `−${kasFromSompi(s.platformFeeSompi)} KAS` : "none"}</span></div>
    </div>`;
  }

  async function claim(m) {
    const s = state.settle;
    const btn = document.getElementById("claimBtn");
    if (btn) { btn.disabled = true; btn.textContent = "Waiting for your wallet…"; }
    // Freeze the panel rebuild while the wallet popup is open (F5): otherwise
    // the 4s poll re-renders this panel, resurrects an ENABLED button, and the
    // handle held here goes stale — training the player to click through a
    // second signing prompt.
    state.signing = true;
    try {
      let sigs;
      try {
        sigs = await signSettleInputs(s.txJson, s.mySignatureInputs, s);
      } catch (e) {
        toast(`Signing failed: ${e.message}`);
        if (btn) { btn.disabled = false; btn.textContent = "Sign & release"; }
        return;
      }
      try {
        state.settle = await api("POST", `/api/matches/${m.id}/settle/submit`, { sigs });
        renderClaim(m);
        toast(state.settle.txid ? "Payout broadcast." : "Signed — waiting on your opponent.");
      } catch (e) {
        toast(`Payout failed: ${e.message}`);
        if (btn) { btn.disabled = false; btn.textContent = "Sign & release"; }
      }
    } finally {
      state.signing = false;
    }
  }

  // The one place a player's key is asked for anything. The escrow is a custom
  // P2SH script, so this needs a wallet's custom-script signing API (Kasware
  // `signPskt`, Kastle `signTx` with `scripts[]`) — a plain "send" API can't
  // produce these signatures. The demo wallet deliberately cannot do it: it
  // exists to click through the game locally, and letting it anywhere near a
  // real payout is exactly the shortcut that ends up shipping.
  // Verify a server-supplied tx pays what the panel says BEFORE the wallet
  // signs it (F7). We can't turn a scriptPublicKey into an address without the
  // Kaspa WASM SDK (not loaded here), but we can parse the tx and sum its
  // outputs: the total leaving the escrow must equal the amount on screen (pot
  // minus the miner fee for a settle; stake minus fee for a reclaim). This is
  // what makes "DAGmate can never move it alone" checkable instead of trusted.
  //
  // Fail-OPEN on any uncertainty (unparseable JSON, unexpected shape, values we
  // can't read as integers): a real payout must never be bricked by a
  // serialization detail we guessed wrong. Fail-CLOSED only on a definite
  // mismatch — outputs we CAN read that sum to something other than expected.
  function verifyOutputsTotal(txJson, expectedSompi) {
    if (expectedSompi == null) return;
    let expected;
    try { expected = BigInt(expectedSompi); } catch (_) { return; }
    let tx;
    try { tx = JSON.parse(txJson); } catch (_) { return; }
    const outs = tx && Array.isArray(tx.outputs) ? tx.outputs : null;
    if (!outs || !outs.length) return;  // shape we don't recognise — don't block
    let sum = 0n;
    for (const o of outs) {
      let v;
      try { v = BigInt(o.value); } catch (_) { return; }  // can't read a value — skip the check
      if (v < 0n) return;
      sum += v;
    }
    if (sum !== expected) {
      throw new Error("this transaction doesn't pay the amount shown — refusing to sign. "
        + "Reload and try again; if it persists, do not sign.");
    }
  }

  async function signSettleInputs(txJson, indexes, ctx) {
    verifyOutputsTotal(txJson, ctx && ctx.expectedOutputSompi);
    const provider = window.kasware || window.kastle;
    if (!provider) throw new Error("connect Kasware or Kastle to release the pot");
    console.log("[DAGmate] signing", { hasSignPskt: typeof provider.signPskt,
      hasSignTx: typeof provider.signTx, indexes, txJsonPreview: String(txJson).slice(0, 160) });
    const out = {};
    if (typeof provider.signPskt === "function") {
      // Kasware's real API: signPskt({ txJsonString, options: { signInputs:[{index,sighashType}] } })
      // → returns the FULLY SIGNED tx JSON (not a per-input signature). We sign
      // every one of our inputs in a single call and hand the signed tx back.
      try {
        const signed = await provider.signPskt({
          txJsonString: txJson,
          options: { signInputs: indexes.map((i) => ({ index: i, sighashType: 1 })) },
        });
        console.log("[DAGmate] signPskt returned:", String(signed).slice(0, 200));
        return { signedTxJson: signed };
      } catch (e) {
        console.error("[DAGmate] signPskt failed:", e, "message:", e && e.message);
        throw e;
      }
    }
    throw new Error("this wallet doesn't expose the signPskt method DAGmate needs to release the pot");
  }

  // ── reclaiming a stranded stake ──────────────────────────────────────
  // The match died with money in it: one side funded and the other never did,
  // or the pot is too small to release. After the 14-day timelock the escrow's
  // second branch opens and the depositor can walk their own stake back out
  // with nothing but their own signature.
  //
  // The escape-hatch note is not decoration. The whole non-custodial claim
  // rests on this working WITHOUT DAGmate, so the redeem script and the DAA
  // are printed where the player can copy them.
  function renderReclaim(m) {
    const el = document.getElementById("reclaimPanel");
    const r = m.reclaim;
    const color = myColorFor(m);
    // While a match is still awaiting its first deposit, the funding panel owns
    // the screen — showing "Reclaim your stake" there is premature and confusing
    // (there's nothing deposited to reclaim yet). Reclaim only makes sense once
    // the match has actually expired/failed, so hold it back until then.
    if (!r || !r.eligible || !color || m.status === "awaiting_deposit") {
      el.innerHTML = ""; state.reclaimFor = null; return;
    }

    const txid = color === "white" ? r.aTxid : r.bTxid;
    if (txid) {
      el.innerHTML = `<div class="claim-title">Stake reclaimed</div>
        <div class="claim-note">Returned to your wallet. Transaction <code>${esc(txid)}</code>.</div>`;
      return;
    }
    // Same once-per-match guard as renderClaim: prepare() builds a real
    // transaction against the live UTXO set, and the 4s refresh must not turn
    // an open panel into a poll loop against the Kaspa node.
    if (state.reclaimFor !== m.id) {
      state.reclaimFor = m.id;
      state.reclaim = null;
      el.innerHTML = `<div class="claim-title">Checking your escrow…</div>`;
      api("POST", `/api/matches/${m.id}/reclaim/prepare`)
        .then((s) => { if (state.reclaimFor === m.id) { state.reclaim = s; renderReclaim(m); } })
        .catch((e) => {
          if (state.reclaimFor !== m.id) return;
          // The escape hatch stays visible even on failure — its whole point is
          // to work when the server path doesn't (append it after the retry).
          showPrepareError(el, "Reclaim your stake", e, () => retryReclaim(m), escapeHatch(m, color));
        });
      return;
    }
    const s = state.reclaim;
    if (!s) return;

    if (s.state === "broadcast") {
      el.innerHTML = `<div class="claim-title">Stake reclaimed</div>
        <div class="claim-note">Returned to your wallet. Transaction <code>${esc(s.txid)}</code>.</div>`;
      return;
    }
    el.innerHTML = `
      <div class="claim-title">Reclaim your stake</div>
      <div class="claim-amount">${kasFromSompi(s.payoutSompi)} KAS</div>
      <div class="claim-fees">
        <div><span>In your escrow</span><span>${kasFromSompi(s.totalSompi)} KAS</span></div>
        <div><span>Kaspa network fee</span><span>−${kasFromSompi(s.networkFeeSompi)} KAS</span></div>
        <div><span>DAGmate fee</span><span>none</span></div>
      </div>
      <button class="btn btn-primary full" id="reclaimBtn">Sign &amp; reclaim</button>
      ${escapeHatch(m, color)}`;
    document.getElementById("reclaimBtn").addEventListener("click", () => doReclaim(m));
  }

  // Printed whether or not the button works, because its whole purpose is to
  // be useful when DAGmate isn't. With these two values a player can spend the
  // escrow's timelock branch from any Kaspa tooling, no server involved.
  function escapeHatch(m, color) {
    const redeem = color === "white" ? m.escrowARedeemHex : m.escrowBRedeemHex;
    const addr = color === "white" ? m.escrowA : m.escrowB;
    if (!redeem) return "";
    return `<div class="claim-note" style="margin-top:10px;">
      This stake is yours with or without DAGmate. After DAA
      <code>${esc(m.reclaim.reclaimDaa)}</code> the escrow's timelock branch is
      spendable by your key alone — escrow <code>${esc(addr)}</code>, redeem script
      <code>${esc(redeem)}</code>.</div>`;
  }

  async function doReclaim(m) {
    const s = state.reclaim;
    const btn = document.getElementById("reclaimBtn");
    if (btn) { btn.disabled = true; btn.textContent = "Waiting for your wallet…"; }
    state.signing = true;  // freeze the panel rebuild while the popup is open (F5)
    try {
      let sigMap;
      try {
        // Identical wallet call to a settlement: same custom-script signing API,
        // same P2SH input. Only the branch the signature ends up selecting
        // differs, and that's decided server-side when the witness is built.
        sigMap = await signSettleInputs(s.txJson, s.mySignatureInputs, s);
      } catch (e) {
        toast(`Signing failed: ${e.message}`);
        if (btn) { btn.disabled = false; btn.textContent = "Sign & reclaim"; }
        return;
      }
      try {
        state.reclaim = await api("POST", `/api/matches/${m.id}/reclaim/submit`,
                                  { txJson: s.txJson, sigs: s.mySignatureInputs.map((i) => sigMap[i]) });
        renderReclaim(m);
        toast("Stake reclaimed.");
      } catch (e) {
        toast(`Reclaim failed: ${e.message}`);
        if (btn) { btn.disabled = false; btn.textContent = "Sign & reclaim"; }
      }
    } finally {
      state.signing = false;
    }
  }

  // Escrow addresses + live deposit progress. The backend watches both
  // addresses on chain and starts the match itself once each holds its stake,
  // so this is a read-only view of that — there is nothing to click.
  function renderEscrowInfo(m) {
    const el = document.getElementById("escrowInfo");
    const f = m.funding;
    if (!f || m.status !== "awaiting_deposit") {
      el.innerHTML = `escrow A: ${esc(m.escrowA || "(pending)")}<br>escrow B: ${esc(m.escrowB || "(pending)")}`;
      return;
    }
    const side = (label, addr, paid, got) => `
      <div class="deposit-row">
        <span class="deposit-tick ${paid ? "ok" : ""}">${paid ? "✓" : "…"}</span>
        <div>
          <div>${label} — ${paid ? "stake received" : `${esc(got)} / ${esc(f.stakeKas)} KAS`}</div>
          <div class="deposit-addr">${esc(addr || "(pending)")}</div>
        </div>
      </div>`;
    el.innerHTML =
      side(`${esc(displayName(m.playerA))} (white)`, m.escrowA, f.aFunded, f.aKas) +
      side(`${esc(displayName(m.playerB))} (black)`, m.escrowB, f.bFunded, f.bKas) +
      `<div class="meta">The match starts automatically once both stakes confirm. If both sides
       haven't funded within ${f.windowMins} minutes the match is cancelled, and any stake you
       sent stays yours.</div>`;

    // One-tap fund: pop the wallet pre-filled to send this player's own stake to
    // their own escrow. Non-custodial — the player still approves in the wallet;
    // this just saves the copy-address-and-type dance. Only for a real wallet
    // (the demo wallet can't send) and only for the viewer's own unfunded side.
    const mine = myColorFor(m);
    const myEscrow = mine === "white" ? m.escrowA : (mine === "black" ? m.escrowB : null);
    const myFunded = mine === "white" ? f.aFunded : f.bFunded;

    // Already sent your deposit, just waiting for the chain to confirm it: show
    // a live spinner so it never looks like nothing happened. The 4s poll clears
    // this the moment the watcher marks your side funded.
    if (mine && myEscrow && !myFunded && !state.isDemoWallet && state.fundingSent === m.id) {
      const wait = document.createElement("div");
      wait.className = "meta";
      wait.style.marginTop = "12px";
      wait.style.fontSize = "15px";
      wait.innerHTML = `<span class="dm-spin">&#9822;</span> Confirming your deposit on-chain — `
        + `the match starts automatically once it lands (~15–30s)…`;
      el.appendChild(wait);
    } else if (mine && myEscrow && !myFunded && !state.isDemoWallet) {
      const wrap = document.createElement("div");
      wrap.style.marginTop = "10px";

      const btn = document.createElement("button");
      btn.className = "btn btn-primary full";
      // Game-native framing, not "fund escrow" — the escrow detail lives behind
      // the (i) toggle so the primary action reads as "get into the game".
      btn.textContent = `Stake ${f.stakeKas} KAS & play`;
      btn.addEventListener("click", () => fundEscrow(myEscrow, f.stakeSompi, f.stakeKas, btn));
      wrap.appendChild(btn);

      const info = document.createElement("button");
      info.className = "btn full";
      info.type = "button";
      info.style.marginTop = "6px";
      info.textContent = "ⓘ Where does my stake go?";
      const note = document.createElement("div");
      note.className = "meta";
      note.style.display = "none";
      note.textContent = "Your stake goes into a per-match escrow on Kaspa L1 that only you can "
        + "reclaim — DAGmate never holds it. Win and you take the whole pot (minus the network "
        + "fee). If the match never starts, your stake is always yours to take back.";
      info.addEventListener("click", () => {
        note.style.display = note.style.display === "none" ? "block" : "none";
      });
      wrap.appendChild(info);
      wrap.appendChild(note);
      el.appendChild(wrap);
    }
  }

  async function fundEscrow(address, stakeSompi, stakeKas, btn) {
    const provider = window.kasware || window.kastle;
    if (!provider || typeof provider.sendKaspa !== "function") {
      toast(`This wallet has no one-tap send — send ${stakeKas} KAS to ${address} yourself.`);
      return;
    }
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Waiting for your wallet…";
    try {
      // Kasware/Kastle sendKaspa takes an integer sompi amount and pops its own
      // confirmation. We pass the exact stake; the deposit watcher starts the
      // match once it confirms on-chain (~15s) and the opponent has funded too.
      await provider.sendKaspa(address, Number(stakeSompi));
      toast("Deposit sent — waiting for it to confirm on-chain.");
      // Flip the panel to the live "confirming…" spinner; the 4s poll clears it
      // once the deposit watcher marks this side funded.
      state.fundingSent = state.currentMatch ? state.currentMatch.id : null;
      if (state.currentMatch) renderEscrowInfo(state.currentMatch);
    } catch (e) {
      toast(`Deposit didn't go through: ${e && e.message ? e.message : e}`);
      btn.disabled = false;
      btn.textContent = label;
    }
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
    const id = shownMatchId();
    if (!id) return;
    state.moveInFlight = true;
    try {
      const m = await api("POST", `/api/matches/${id}/move`, { uci });
      // Guard against a move response landing after we've navigated away.
      if (shownMatchId() !== m.id) return;
      // Bump the poll sequence so any in-flight GET that started before this
      // move is discarded when it resolves, rather than reverting the board.
      state.pollSeq++;
      state.currentMatch = m;
      state.clockRecvAt = Date.now();
      renderMatchCard();
      refreshMatches();
    } catch (e) { toast(e.message); }
    finally { state.moveInFlight = false; }
  }

  document.getElementById("drawBtn").addEventListener("click",
    () => drawAction("offer", "Draw offered — your opponent has to agree."));

  document.getElementById("resignBtn").addEventListener("click", async (e) => {
    const m = state.currentMatch;
    if (!m) return;
    // Resigning hands the whole pot to the opponent and cannot be undone —
    // every other money decision in the app is spelled out, so this one is too.
    // The confirm names the stake at risk (and sits behind the browser's own
    // dialog, which a clickjacking frame can't drive).
    const stake = `your ${m.stakeKas} KAS stake`;
    if (!window.confirm(`Resign and forfeit ${stake} to your opponent? This can't be undone.`)) return;
    const btn = e.currentTarget;
    btn.disabled = true;
    try {
      const r = await api("POST", `/api/matches/${m.id}/resign`);
      if (shownMatchId() === r.id) { state.currentMatch = r; renderMatchCard(); }
      refreshMatches();
    } catch (err) { toast(err.message); }
    finally { btn.disabled = false; }
  });

  document.getElementById("fundBtn").addEventListener("click", async () => {
    const id = shownMatchId();
    if (!id) return;
    try {
      await api("POST", `/api/matches/${id}/dev-mark-funded`);
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
    catch (e) { el.innerHTML = `<p class="muted">${esc(e.message)}</p>`; return; }
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
    if (!state.session) { toast("Connect a wallet first."); return; }
    try {
      const r = await api("POST", `/api/tournaments/${tierKas}/join`);
      toast(r.alreadyJoined ? "Already in this lobby." : r.started ? "Lobby full — pairings created!" : "Joined lobby.");
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
    if (!state.session) { el.innerHTML = `<p class="muted">Connect a wallet to see learn levels.</p>`; return; }
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
        const priceNote = (lv.gas_kas && state.meta && state.meta.learnRequiresPayment)
          ? "· " + lv.gas_kas + " KAS gas" : "· free";
        div.innerHTML = `<div><div>${lv.title}</div>
          <div class="meta">${lv.summary} ${priceNote}</div></div>
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
      const r = await api("GET", `/api/learn/levels/${lv.id}/content`);
      body.innerHTML = r.body;
    } catch (e) {
      body.innerHTML = `<p class="muted">${esc(e.message)}</p>`;
    }
  }

  async function unlockLevel(lv) {
    try {
      await api("POST", `/api/learn/levels/${lv.id}/unlock`);
      toast("Unlocked.");
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
    if (!state.session) return;
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
  //
  // This also carries whether the dev routes are switched on, for the same
  // reason: the UI asks the server what exists rather than keeping its own
  // copy of the answer, so a real deployment loses the demo wallet and the
  // "mark funded" button without anyone remembering to edit the frontend.
  async function loadMeta() {
    let meta;
    try { meta = await api("GET", "/api/meta"); } catch (e) { return; }
    state.meta = meta;
    if (!meta.devRoutes) document.getElementById("fundBtn").remove();
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
    // Cap the stake field to the server's accepted range (F12) — the backend
    // still enforces it, this just stops a bad amount at the form.
    const stakeEl = document.getElementById("chStake");
    if (meta.minStakeKas != null) stakeEl.min = meta.minStakeKas;
    if (meta.maxStakeKas != null) stakeEl.max = meta.maxStakeKas;
    // One-tap link to the alerts bot, only when a handle is configured. When it
    // is, the link replaces the plain "open the DAGmate bot" text.
    const botLink = document.getElementById("tgBotLink");
    const botHint = document.getElementById("tgBotHint");
    if (meta.botUsername) {
      botLink.href = `https://t.me/${encodeURIComponent(meta.botUsername)}`;
      botLink.textContent = `open @${meta.botUsername},`;
      botLink.style.display = "";
      botHint.style.display = "none";
    } else {
      botLink.style.display = "none";
      botHint.style.display = "";
    }
  }

  // ── boot ─────────────────────────────────────────────────────────────
  renderWalletBadge();
  restoreWallet();
  refreshTournaments();
  loadMeta();
  initPracticeLevels().then(practiceInit);
})();
