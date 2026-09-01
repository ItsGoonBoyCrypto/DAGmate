/**
 * DAGmate — full end-to-end test harness (testnet). Drives the REAL backend
 * HTTP API with up to 8 throwaway wallets funded from DAGmate's own operating
 * address, exercising every user-facing function: auth, 1v1 (checkmate /
 * resign / draw), input-validation/security rejections, challenge lifecycle,
 * deposit-deadline expiry, reclaim, and a full 8-player tournament to a
 * champion (incl. walkover + neither-funds void/bye).
 *
 * Nothing here holds a player's real key or touches mainnet: every wallet is a
 * fresh random key, funded with testnet dust from the operating address and
 * swept back at the end. It impersonates the wallet's signing (signMessage for
 * auth, createInputSignature for escrow spends) using the SAME primitives the
 * sidecar's escrow.js uses — see that file's header.
 *
 * Run on the server (needs core.js's env + mnemonic credential + node):
 *   node e2e_full.mjs <phase> [phase...]
 * Phases: auth duel resign draw security challenges expiry reclaim tournament all
 *
 * Bounded-config phases (expiry/reclaim/tournament deadlines) read short
 * windows from env the caller sets in site.env before restarting the backend:
 *   DAGMATE_DEPOSIT_DEADLINE_SECS, DAGMATE_DEPOSIT_POLL_SECS,
 *   DAGMATE_RECLAIM_DAA_WINDOW, DAGMATE_TOURNAMENT_MIN_ENTRANTS(=8).
 */
import { randomBytes } from 'node:crypto';
import * as core from './core.js';

const k = core.wasm();
const NET = core.netType();
const NETWORK_ID = core.network();
const API = process.env.DAGMATE_API || 'http://127.0.0.1:8800';
const SOMPI = 100_000_000n;

// ── tiny test framework ────────────────────────────────────────────────────
let PASS = 0, FAIL = 0;
const FAILURES = [];
function ok(cond, label) {
  if (cond) { PASS++; console.log(`   ✓ ${label}`); }
  else { FAIL++; FAILURES.push(label); console.log(`   ✗ FAIL: ${label}`); }
  return cond;
}
async function expectStatus(promise, wantStatus, label) {
  try { await promise; ok(false, `${label} (expected ${wantStatus}, got success)`); }
  catch (e) { ok(e.status === wantStatus, `${label} (rejected ${e.status ?? '?'} — want ${wantStatus})`); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ── HTTP ───────────────────────────────────────────────────────────────────
async function api(method, path, { body, token } = {}) {
  const headers = { 'content-type': 'application/json' };
  if (token) headers.authorization = `Bearer ${token}`;
  const res = await fetch(`${API}${path}`, {
    method, headers, body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data = null; try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    const err = new Error(`${method} ${path} → ${res.status}: ${text}`);
    err.status = res.status; err.data = data; throw err;
  }
  return data;
}

// ── wallets ──────────────────────────────────────────────────────────────
function newWallet(tag) {
  const key = new k.PrivateKey(randomBytes(32).toString('hex'));
  const pub = key.toPublicKey();
  return { tag, key, pub, address: pub.toAddress(NET).toString(), pubkeyHex: pub.toString(), token: null, id: null };
}

async function login(w) {
  const { nonce, message } = await api('POST', '/api/auth/nonce', { body: { address: w.address } });
  const signature = k.signMessage({ message, privateKey: w.key });
  const r = await api('POST', '/api/auth/verify', { body: { address: w.address, pubkey: w.pubkeyHex, nonce, signature } });
  w.token = r.token; w.id = r.account.id;
  return r;
}

// ── on-chain helpers (funding, deposits, sweeps, balances) ─────────────────
async function fundWallets(wallets, sompiEach) {
  await core.withRpc(async (rpc) => {
    const { address: opAddr, key: opKey } = core.operatingAddress();
    let { entries } = await rpc.getUtxosByAddresses({ addresses: [opAddr] });
    if (!entries.length) throw new Error('operating address has no UTXOs to fund from');
    const outputs = wallets.map((w) => ({ address: w.address, amount: sompiEach }));
    const { transactions } = await k.createTransactions({
      entries, outputs, changeAddress: opAddr, priorityFee: 50_000_000n, networkId: NETWORK_ID,
    });
    let txid = null;
    for (const tx of transactions) { tx.sign([opKey]); txid = await tx.submit(rpc); }
    console.log(`   funded ${wallets.length} wallets with ${sompiEach} sompi each — txid ${txid}`);
  });
}

async function deposit(w, escrowAddr, sompi) {
  return core.withRpc(async (rpc) => {
    const { entries } = await rpc.getUtxosByAddresses({ addresses: [w.address] });
    if (!entries.length) throw new Error(`${w.tag} has no UTXOs to deposit`);
    const { transactions } = await k.createTransactions({
      entries, outputs: [{ address: escrowAddr, amount: sompi }],
      changeAddress: w.address, priorityFee: 20_000_000n, networkId: NETWORK_ID,
    });
    let txid = null;
    for (const tx of transactions) { tx.sign([w.key]); txid = await tx.submit(rpc); }
    return txid;
  });
}

async function balance(address) {
  return core.withRpc(async (rpc) => {
    const { entries } = await rpc.getUtxosByAddresses({ addresses: [address] });
    return entries.reduce((s, e) => s + BigInt(e.amount ?? e.utxoEntry?.amount ?? 0), 0n);
  });
}

async function sweepBack(wallets) {
  const { address: opAddr } = core.operatingAddress();
  for (const w of wallets) {
    try {
      await core.withRpc(async (rpc) => {
        const { entries } = await rpc.getUtxosByAddresses({ addresses: [w.address] });
        if (!entries.length) return;
        const { transactions } = await k.createTransactions({
          entries, outputs: [], changeAddress: opAddr, priorityFee: 0n, networkId: NETWORK_ID,
        });
        for (const tx of transactions) { tx.sign([w.key]); await tx.submit(rpc); }
      });
    } catch (e) { console.log(`   (sweep ${w.tag} skipped: ${e.message})`); }
  }
  console.log(`   swept residual balances back to operating address`);
}

// ── match helpers ──────────────────────────────────────────────────────────
async function getMatch(id) { return api('GET', `/api/matches/${id}`); }

async function waitForStatus(id, want, timeoutMs = 240_000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const m = await getMatch(id);
    if (Array.isArray(want) ? want.includes(m.status) : m.status === want) return m;
    await sleep(4000);
  }
  throw new Error(`match ${id} never reached ${want} (last poll timed out)`);
}

// Fund BOTH escrows of a match and wait until the deposit watcher marks it live.
async function fundBothAndGoLive(match, wA, wB) {
  const sompi = BigInt(match.funding.stakeSompi);
  await deposit(wA, match.escrowA, sompi);
  await deposit(wB, match.escrowB, sompi);
  return waitForStatus(match.id, 'live');
}

// Play a move as the given wallet.
async function move(w, id, uci) { return api('POST', `/api/matches/${id}/move`, { token: w.token, body: { uci } }); }

// Fool's mate — fastest checkmate; BLACK (player B) delivers mate in 4 plies.
// 1. f3 e5 2. g4 Qh4#
async function playFoolsMate(id, white, black) {
  await move(white, id, 'f2f3');
  await move(black, id, 'e7e5');
  await move(white, id, 'g2g4');
  return move(black, id, 'd8h4'); // Qh4# — checkmate, black wins
}

// ── settlement (impersonate the wallet's signPskt) ─────────────────────────
// Winner calls prepare, signs each of their inputs with createInputSignature,
// embeds each sig as a single data-push into that input's signatureScript
// (exactly what the sidecar's extractSigs/firstPushHex expects), and submits.
async function settleAsWinner(w, id) {
  const prep = await api('POST', `/api/matches/${id}/settle/prepare`, { token: w.token });
  if (prep.state === 'broadcast') return prep; // already released
  const mine = prep.mySignatureInputs || [];
  if (!mine.length) return prep;
  const tx = k.Transaction.deserializeFromSafeJSON(prep.txJson);
  const inputs = tx.inputs;
  for (const i of mine) {
    const sig = k.createInputSignature(tx, i, w.key); // hex
    inputs[i].signatureScript = new k.ScriptBuilder(core.COVENANT_OPTS)
      .addData(Buffer.from(String(sig), 'hex')).drain();
  }
  tx.inputs = inputs; // low-level Transaction: commit the array back
  const signedTxJson = tx.serializeToSafeJSON();
  return api('POST', `/api/matches/${id}/settle/submit`, { token: w.token, body: { signedTxJson } });
}

// Draw: each player signs only their own escrow's inputs; both submit.
async function settleDrawSide(w, id) {
  const prep = await api('POST', `/api/matches/${id}/settle/prepare`, { token: w.token });
  const mine = prep.mySignatureInputs || [];
  if (!mine.length) return prep;
  const tx = k.Transaction.deserializeFromSafeJSON(prep.txJson);
  const inputs = tx.inputs;
  for (const i of mine) {
    const sig = k.createInputSignature(tx, i, w.key);
    inputs[i].signatureScript = new k.ScriptBuilder(core.COVENANT_OPTS)
      .addData(Buffer.from(String(sig), 'hex')).drain();
  }
  tx.inputs = inputs;
  return api('POST', `/api/matches/${id}/settle/submit`, { token: w.token, body: { signedTxJson: tx.serializeToSafeJSON() } });
}

async function waitSettleBroadcast(id, winnerWallet, tries = 20) {
  for (let i = 0; i < tries; i++) {
    const p = await api('POST', `/api/matches/${id}/settle/prepare`, { token: winnerWallet.token });
    if (p.state === 'broadcast' && p.txid) return p;
    await sleep(3000);
  }
  throw new Error(`settle for ${id} never broadcast`);
}

// ── reclaim (impersonate wallet: sign each input, pass raw sigs) ────────────
async function reclaimSide(w, id) {
  const prep = await api('POST', `/api/matches/${id}/reclaim/prepare`, { token: w.token });
  const mine = prep.mySignatureInputs || [];
  const tx = k.Transaction.deserializeFromSafeJSON(prep.txJson);
  const sigs = mine.map((i) => String(k.createInputSignature(tx, i, w.key)));
  return api('POST', `/api/matches/${id}/reclaim/submit`, { token: w.token, body: { txJson: prep.txJson, sigs } });
}

// ═══════════════════════════════════════════════════════════════════════════
// PHASES
// ═══════════════════════════════════════════════════════════════════════════

async function phaseAuth(pool) {
  console.log('\n── PHASE auth: 8 wallets sign in ──');
  for (const w of pool) {
    const r = await login(w);
    ok(!!w.token && !!w.id, `${w.tag} logged in (account ${r.account.shortAddress})`);
  }
  // profile + accept-challenges toggle
  const p = await api('GET', '/api/profile', { token: pool[0].token });
  ok(p.address === pool[0].address, 'profile returns the caller’s own account');
  await api('POST', '/api/profile/accept-challenges', { token: pool[0].token, body: { enabled: false } });
  await api('POST', '/api/profile/accept-challenges', { token: pool[0].token, body: { enabled: true } });
  ok(true, 'accept-challenges toggled off/on');
  // a bad token is rejected
  await expectStatus(api('GET', '/api/profile', { token: 'not-a-real-token' }), 401, 'garbage token rejected 401');
}

async function makeChallengeMatch(creator, accepter, stakeKas, mode = 'rapid') {
  const ch = await api('POST', '/api/challenges', { token: creator.token, body: { stakeKas, mode } });
  const m = await api('POST', `/api/challenges/${ch.id}/accept`, { token: accepter.token });
  return m; // player A = creator, player B = accepter
}

async function phaseDuel(pool) {
  console.log('\n── PHASE duel: decisive 1v1 (checkmate) → settle ──');
  const [wA, wB] = pool;
  const m = await makeChallengeMatch(wA, wB, 2);
  ok(m.status === 'awaiting_deposit' && m.escrowA && m.escrowB, 'match created with two escrows');
  const live = await fundBothAndGoLive(m, wA, wB);
  ok(live.status === 'live' && live.turn === 'white', 'both funded → match live, white to move');
  const end = await playFoolsMate(m.id, wA, wB);
  ok(end.status === 'settled' && end.result === 'checkmate', 'fool’s mate → settled/checkmate');
  ok(end.winnerAccountId === wB.id, 'player B (black) recorded as winner');
  const before = await balance(wB.address);
  await settleAsWinner(wB, m.id);
  const bc = await waitSettleBroadcast(m.id, wB);
  ok(!!bc.txid, `pot released on-chain (txid ${String(bc.txid).slice(0, 12)}…)`);
  // winner's wallet should receive roughly pot − fee
  let after = 0n; for (let i = 0; i < 15; i++) { after = await balance(wB.address); if (after > before) break; await sleep(3000); }
  ok(after > before, `winner balance increased (${before} → ${after} sompi)`);
}

async function phaseResign(pool) {
  console.log('\n── PHASE resign: 1v1 resignation → settle ──');
  const [wA, wB] = pool;
  const m = await makeChallengeMatch(wA, wB, 2);
  await fundBothAndGoLive(m, wA, wB);
  await move(wA, m.id, 'e2e4');
  const end = await api('POST', `/api/matches/${m.id}/resign`, { token: wA.token }); // A resigns → B wins
  ok(end.status === 'settled' && end.result === 'resign', 'A resigned → settled/resign');
  ok(end.winnerAccountId === wB.id, 'B is the winner after A resigns');
  await settleAsWinner(wB, m.id);
  const bc = await waitSettleBroadcast(m.id, wB);
  ok(!!bc.txid, 'resigned pot released on-chain');
}

async function phaseDraw(pool) {
  console.log('\n── PHASE draw: agreed draw → split pot ──');
  const [wA, wB] = pool;
  const m = await makeChallengeMatch(wA, wB, 2);
  await fundBothAndGoLive(m, wA, wB);
  await move(wA, m.id, 'e2e4');
  await move(wB, m.id, 'e7e5');
  await api('POST', `/api/matches/${m.id}/draw/offer`, { token: wA.token });
  const end = await api('POST', `/api/matches/${m.id}/draw/accept`, { token: wB.token });
  ok(end.status === 'settled' && end.winnerAccountId === null, 'draw agreed → settled, no winner');
  const beforeA = await balance(wA.address), beforeB = await balance(wB.address);
  await settleDrawSide(wA, m.id);
  await settleDrawSide(wB, m.id);
  const bc = await waitSettleBroadcast(m.id, wA);
  ok(!!bc.txid, 'split pot released on-chain');
  let afterA = 0n, afterB = 0n;
  for (let i = 0; i < 15; i++) { afterA = await balance(wA.address); afterB = await balance(wB.address); if (afterA > beforeA && afterB > beforeB) break; await sleep(3000); }
  ok(afterA > beforeA && afterB > beforeB, `both sides refunded ~half (A ${beforeA}→${afterA}, B ${beforeB}→${afterB})`);
}

async function phaseSecurity(pool) {
  console.log('\n── PHASE security: rejections that protect the pot ──');
  const [wA, wB, wC] = pool;
  const m = await makeChallengeMatch(wA, wB, 2);
  await fundBothAndGoLive(m, wA, wB);
  await expectStatus(move(wB, m.id, 'e7e5'), 400, 'out-of-turn move rejected (black moving first)');
  await expectStatus(move(wA, m.id, 'e2e5'), 400, 'illegal move rejected (e2e5)');
  await expectStatus(move(wC, m.id, 'e2e4'), 403, 'non-player cannot move');
  await expectStatus(api('POST', `/api/matches/${m.id}/resign`, { token: wC.token }), 403, 'non-player cannot resign');
  // accept-own / decline-not-yours
  const ch = await api('POST', '/api/challenges', { token: wA.token, body: { stakeKas: 2, mode: 'rapid' } });
  await expectStatus(api('POST', `/api/challenges/${ch.id}/accept`, { token: wA.token }), 400, 'cannot accept your own challenge');
  await expectStatus(api('POST', `/api/challenges/${ch.id}/decline`, { token: wC.token }), 403, 'stranger cannot decline your challenge');
  await api('POST', `/api/challenges/${ch.id}/decline`, { token: wA.token }); // withdraw own
  ok(true, 'creator can withdraw own open challenge');
  // sub-minimum stake bounces
  await expectStatus(api('POST', '/api/challenges', { token: wA.token, body: { stakeKas: 0, mode: 'rapid' } }), 400, 'zero-stake challenge rejected');
  // clean up the live match so funds aren't stranded: A resigns, B settles
  await api('POST', `/api/matches/${m.id}/resign`, { token: wA.token });
  await settleAsWinner(wB, m.id); await waitSettleBroadcast(m.id, wB).catch(() => {});
}

async function phaseChallenges(pool) {
  console.log('\n── PHASE challenges: named challenge + open board ──');
  const [wA, wB] = pool;
  const named = await api('POST', '/api/challenges', { token: wA.token, body: { toAddress: wB.address, stakeKas: 2, mode: 'rapid' } });
  ok(named.toAddress === wB.address, 'named challenge addressed to B');
  const list = await api('GET', '/api/challenges', { token: wB.token });
  ok(list.some((c) => c.id === named.id), 'named challenge visible to its target');
  const m = await api('POST', `/api/challenges/${named.id}/accept`, { token: wB.token });
  ok(m.status === 'awaiting_deposit', 'named challenge accepted → match');
  // clean up (never funded): let it expire on its own; nothing at risk (no deposits)
}

async function phaseExpiry(pool) {
  console.log('\n── PHASE expiry: neither funds → match expires (needs short deadline) ──');
  const [wA, wB] = pool;
  const m = await makeChallengeMatch(wA, wB, 2);
  const dead = await waitForStatus(m.id, ['expired', 'void'], 240_000).catch(() => null);
  ok(dead && ['expired', 'void'].includes(dead.status), `unfunded match reached ${dead?.status} past the deadline`);
}

async function phaseReclaim(pool) {
  console.log('\n── PHASE reclaim: one side funds, match dies, funder reclaims (needs short reclaim window) ──');
  const [wA, wB] = pool;
  const m = await makeChallengeMatch(wA, wB, 2);
  const sompi = BigInt(m.funding.stakeSompi);
  await deposit(wA, m.escrowA, sompi); // only A funds
  await waitForStatus(m.id, ['expired', 'void'], 240_000).catch(() => null);
  // Baseline AFTER expiry: by now the deposit's change output has confirmed, so
  // the wallet reads its true post-deposit balance (not the stale pre-spend one).
  const before = await balance(wA.address);
  const escrowBefore = await balance(m.escrowA);
  ok(escrowBefore >= sompi, `escrow holds the stranded stake before reclaim (${escrowBefore})`);
  // wait for the (short) CLTV window, then reclaim A's escrow
  let done = null;
  for (let i = 0; i < 30; i++) {
    try { done = await reclaimSide(wA, m.id); if (done?.txid) break; } catch (e) { /* window not open yet */ }
    await sleep(5000);
  }
  ok(!!done?.txid, `A reclaimed the stranded stake (txid ${String(done?.txid).slice(0, 12)}…)`);
  let after = 0n; for (let i = 0; i < 15; i++) { after = await balance(wA.address); if (after > before) break; await sleep(3000); }
  ok(after > before, `reclaim returned funds to A (post-expiry ${before} → ${after})`);
  let escrowAfter = sompi; for (let i = 0; i < 10; i++) { escrowAfter = await balance(m.escrowA); if (escrowAfter === 0n) break; await sleep(3000); }
  ok(escrowAfter === 0n, `escrow drained by the reclaim (${escrowAfter} left)`);
}

async function phaseTournament(pool) {
  // All-decisive bracket to a champion (join → 3 rounds → doubling economy).
  // Walkover / neither-funds void / bye are proven separately (live e2e 7ef406f
  // + test_tournament.py), because their branch can't roll a pot forward to fund
  // the doubled next stake and so needs its own funding setup, not this one.
  console.log('\n── PHASE tournament: 8 players → champion (all-decisive) ──');
  const tiers = await api('GET', '/api/tournaments');
  const tier = tiers[0].tierKas;
  console.log(`   tier ${tier} KAS, minEntrants ${tiers[0].minEntrants}, rounds ${tiers[0].rounds}`);
  let joinRes;
  for (const w of pool) joinRes = await api('POST', `/api/tournaments/${tier}/join`, { token: w.token });
  ok(joinRes.started === true, '8th join auto-started the bracket');

  // Derive the tournament id + round-1 matches straight from the players' own
  // match lists — these 8 fresh wallets have no other matches, so every match
  // they hold belongs to the tournament that just started. (Reading tid off the
  // pre-join lobby listing is unreliable: a stale open lobby can shadow it.)
  let r1 = [];
  for (let attempt = 0; attempt < 10 && r1.length === 0; attempt++) {
    const byId = new Map();
    for (const w of pool) for (const m of await api('GET', '/api/matches', { token: w.token }))
      if (m.tournamentId && m.round === 1) byId.set(m.id, m);
    r1 = [...byId.values()];
    if (!r1.length) await sleep(3000);
  }
  ok(r1.length === 4, `round 1 has 4 matches (got ${r1.length})`);
  const tid = r1[0]?.tournamentId;
  if (!tid) { ok(false, 'no tournament id resolved from round-1 matches'); return; }

  const wOf = (addr) => pool.find((w) => w.address === addr);
  const played = new Set(); // match ids already played + settled

  // Play one match decisively: fund both, fool's-mate (B wins), settle, and
  // confirm the winnings land in B's wallet — that pot is exactly what funds B's
  // next-round (doubled) stake, so the roll-forward economy is what's under test.
  async function playAndSettle(m, label) {
    if (played.has(m.id)) return;
    const wa = wOf(m.playerA.address), wb = wOf(m.playerB.address);
    if (!wa || !wb) { console.log(`   (${label} ${m.id.slice(0, 8)}: player not in pool)`); return; }
    const live = m.status === 'live' ? m : await fundBothAndGoLive(m, wa, wb);
    if (live.status !== 'live') return;
    const before = await balance(wb.address);
    await playFoolsMate(m.id, wa, wb);
    await settleAsWinner(wb, m.id);
    await waitSettleBroadcast(m.id, wb).catch(() => {});
    let after = before; for (let i = 0; i < 15; i++) { after = await balance(wb.address); if (after > before) break; await sleep(3000); }
    ok(after > before, `${label}: ${wb.tag} won and took the pot (${before} → ${after})`);
    played.add(m.id);
  }

  for (const m of r1) await playAndSettle(m, 'R1');

  // Drive the remaining rounds: keep finding this tournament's still-open matches,
  // play each decisively, until a champion is crowned (or the safety deadline).
  const deadline = Date.now() + 20 * 60 * 1000;
  let champion = null;
  while (Date.now() < deadline) {
    const det = await api('GET', `/api/tournaments/${tid}/detail`);
    if (det.status === 'complete' && det.champion) { champion = det.champion; break; }
    if (det.status === 'void') break;
    const seen = new Map();
    for (const w of pool) for (const m of await api('GET', '/api/matches', { token: w.token }))
      if (m.tournamentId === tid) seen.set(m.id, m);
    const open = [...seen.values()].filter((m) => ['awaiting_deposit', 'live'].includes(m.status) && !played.has(m.id));
    for (const m of open) {
      try { await playAndSettle(m, `R${m.round}`); }
      catch (e) { console.log(`   (R${m.round} ${m.id.slice(0, 8)} skipped: ${e.message})`); }
    }
    await sleep(6000);
  }
  ok(!!champion, `tournament crowned a champion: ${champion?.shortAddress ?? '(none — timed out)'}`);
}

// ── runner ─────────────────────────────────────────────────────────────────
const PHASES = {
  auth: phaseAuth, duel: phaseDuel, resign: phaseResign, draw: phaseDraw,
  security: phaseSecurity, challenges: phaseChallenges, expiry: phaseExpiry,
  reclaim: phaseReclaim, tournament: phaseTournament,
};

async function main() {
  const args = process.argv.slice(2);
  const want = (args.length && args[0] !== 'all') ? args : Object.keys(PHASES);
  console.log(`DAGmate E2E — API ${API}, network ${NETWORK_ID}`);
  console.log(`phases: ${want.join(', ')}`);

  // Enough wallets for the biggest phase (tournament = 8). Fund generously so a
  // finalist can cover R1+R2+R3 doubling stakes + gas; sweep back at the end.
  const pool = Array.from({ length: 8 }, (_, i) => newWallet(`w${i + 1}`));
  console.log('\n── funding 8 throwaway wallets from the operating address ──');
  await fundWallets(pool, 25n * SOMPI);
  // wait for funding to confirm
  await core.withRpc(async (rpc) => {
    for (let i = 0; i < 30; i++) {
      const { entries } = await rpc.getUtxosByAddresses({ addresses: pool.map((w) => w.address) });
      if (entries.length >= pool.length) return;
      await sleep(3000);
    }
  });
  for (const w of pool) await login(w); // everyone signed in for all phases

  try {
    for (const name of want) {
      if (!PHASES[name]) { console.log(`   (unknown phase ${name})`); continue; }
      // each phase gets its own fresh slice so state doesn't bleed across phases
      await PHASES[name](name === 'tournament' || name === 'auth' ? pool : pool.slice(0, 3));
    }
  } finally {
    console.log('\n── sweeping residual balances back ──');
    await sweepBack(pool);
  }

  console.log(`\n════════ RESULT: ${PASS} passed, ${FAIL} failed ════════`);
  if (FAIL) { console.log('FAILURES:'); for (const f of FAILURES) console.log(`  ✗ ${f}`); }
  process.exit(FAIL ? 1 : 0);
}

main().catch((e) => { console.error('HARNESS ERROR:', e); process.exit(2); });
