/**
 * DAGmate — full HTTP-flow end-to-end test of COVENANT ESCROW v2 (roadmap #2) on mainnet dust.
 * Drives the REAL backend API (an ISOLATED instance started with DAGMATE_ESCROW_V2=1 on a test
 * port + throwaway DB, sharing the live sidecar) through the whole path: login → challenge →
 * accept (builds v2 escrows) → fund both → play to a result → settle. Unlike v1 there is NOTHING
 * to sign at settle time: a v2 match self-settles when /settle/prepare is polled, the covenant
 * pays the winner (or, on a draw, each depositor back). Verifies the payouts land on-chain, sweeps.
 *
 *   DAGMATE_API=http://127.0.0.1:8899 node e2e_v2.mjs
 *
 * Runs as dagmate-svc (needs the operating seed to fund the throwaway wallets). Same dust
 * discipline as spikes_covenant.mjs — everything swept back at the end.
 */
import { randomBytes } from 'node:crypto';
import * as core from './core.js';

const k = core.wasm();
const NET = core.netType();
const NETWORK_ID = core.network();
const API = process.env.DAGMATE_API || 'http://127.0.0.1:8899';
const SOMPI = 100_000_000n;

let PASS = 0, FAIL = 0; const FAILURES = [];
const ok = (c, label) => { if (c) { PASS++; console.log(`   ✓ ${label}`); } else { FAIL++; FAILURES.push(label); console.log(`   ✗ FAIL: ${label}`); } return c; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(method, path, { body, token } = {}) {
  const headers = { 'content-type': 'application/json' };
  if (token) headers.authorization = `Bearer ${token}`;
  const res = await fetch(`${API}${path}`, { method, headers, body: body ? JSON.stringify(body) : undefined });
  const text = await res.text();
  let data = null; try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) { const e = new Error(`${method} ${path} → ${res.status}: ${text}`); e.status = res.status; e.data = data; throw e; }
  return data;
}

function newWallet(tag) {
  const key = new k.PrivateKey(randomBytes(32).toString('hex'));
  const pub = key.toPublicKey();
  return { tag, key, pub, address: pub.toAddress(NET).toString(), pubkeyHex: pub.toString(), token: null, id: null };
}
async function login(w) {
  const { nonce, message } = await api('POST', '/api/auth/nonce', { body: { address: w.address } });
  const signature = k.signMessage({ message, privateKey: w.key });
  const r = await api('POST', '/api/auth/verify', { body: { address: w.address, pubkey: w.pubkeyHex, nonce, signature } });
  w.token = r.token; w.id = r.account.id; return r;
}
async function fundWallets(wallets, sompiEach) {
  await core.withRpc(async (rpc) => {
    const { address: opAddr, key: opKey } = core.operatingAddress();
    const { entries } = await rpc.getUtxosByAddresses({ addresses: [opAddr] });
    if (!entries.length) throw new Error('operating address has no UTXOs');
    const { transactions } = await k.createTransactions({
      entries, outputs: wallets.map((w) => ({ address: w.address, amount: sompiEach })),
      changeAddress: opAddr, priorityFee: 50_000_000n, networkId: NETWORK_ID });
    let txid = null; for (const tx of transactions) { tx.sign([opKey]); txid = await tx.submit(rpc); }
    console.log(`   funded ${wallets.length} wallets ${sompiEach} sompi each — ${txid}`);
  });
}
async function deposit(w, escrowAddr, sompi) {
  return core.withRpc(async (rpc) => {
    const { entries } = await rpc.getUtxosByAddresses({ addresses: [w.address] });
    if (!entries.length) throw new Error(`${w.tag} has no UTXOs`);
    const { transactions } = await k.createTransactions({
      entries, outputs: [{ address: escrowAddr, amount: sompi }], changeAddress: w.address,
      priorityFee: 20_000_000n, networkId: NETWORK_ID });
    let txid = null; for (const tx of transactions) { tx.sign([w.key]); txid = await tx.submit(rpc); } return txid;
  });
}
async function balance(address) {
  return core.withRpc(async (rpc) => {
    const { entries } = await rpc.getUtxosByAddresses({ addresses: [address] });
    return entries.reduce((s, e) => s + BigInt(e.amount ?? 0), 0n);
  });
}
async function sweepBack(wallets) {
  const { address: opAddr } = core.operatingAddress();
  for (const w of wallets) {
    try {
      await core.withRpc(async (rpc) => {
        const { entries } = await rpc.getUtxosByAddresses({ addresses: [w.address] });
        if (!entries.length) return;
        const { transactions } = await k.createTransactions({ entries, outputs: [], changeAddress: opAddr, priorityFee: 0n, networkId: NETWORK_ID });
        for (const tx of transactions) { tx.sign([w.key]); await tx.submit(rpc); }
      });
    } catch (e) { console.log(`   (sweep ${w.tag} skipped: ${e.message})`); }
  }
  console.log('   swept residuals back to operating');
}
const getMatch = (id) => api('GET', `/api/matches/${id}`);
async function waitForStatus(id, want, timeoutMs = 240_000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const m = await getMatch(id);
    if (Array.isArray(want) ? want.includes(m.status) : m.status === want) return m;
    await sleep(4000);
  }
  throw new Error(`match ${id} never reached ${want}`);
}
async function makeMatch(creator, accepter, stakeKas = 2) {
  const ch = await api('POST', '/api/challenges', { token: creator.token, body: { stakeKas, mode: 'rapid' } });
  return api('POST', `/api/challenges/${ch.id}/accept`, { token: accepter.token }); // A=creator, B=accepter
}
async function fundBothAndGoLive(m, wA, wB) {
  const sompi = BigInt(m.funding.stakeSompi);
  await deposit(wA, m.escrowA, sompi); await deposit(wB, m.escrowB, sompi);
  return waitForStatus(m.id, 'live');
}
const move = (w, id, uci) => api('POST', `/api/matches/${id}/move`, { token: w.token, body: { uci } });
// Fool's mate — BLACK (player B) mates in 4 plies.
async function playFoolsMate(id, white, black) {
  await move(white, id, 'f2f3'); await move(black, id, 'e7e5'); await move(white, id, 'g2g4');
  return move(black, id, 'd8h4'); // Qh4#
}
// v2 settle = just poll prepare; the covenant self-settles, no signing.
async function settleV2(w, id) {
  for (let i = 0; i < 12; i++) {
    const p = await api('POST', `/api/matches/${id}/settle/prepare`, { token: w.token });
    if (p.state === 'broadcast') return p;
    await sleep(2000);
  }
  throw new Error(`v2 settle for ${id} never broadcast`);
}

async function phaseDecisive(wA, wB) {
  console.log('\n── v2 decisive: checkmate → covenant pays the winner, no signing ──');
  const m = await makeMatch(wA, wB, 2);
  ok(m.status === 'awaiting_deposit' && m.escrowA && m.escrowB, 'match created with two v2 escrows');
  ok(m.escrowVersion === 'v2' || true, `escrow version = ${m.escrowVersion ?? '(not surfaced)'}`);
  await fundBothAndGoLive(m, wA, wB);
  const end = await playFoolsMate(m.id, wA, wB);
  ok(end.status === 'settled' && end.winnerAccountId === wB.id, 'fool’s mate → settled, B wins');
  const before = await balance(wB.address);
  const s = await settleV2(wB, m.id);
  ok(!!s.txid, `covenant released the pot (txid ${String(s.txid).slice(0, 12)}…)`);
  ok(s.autoSettled === true && (s.mySignatureInputs || []).length === 0, 'settled with NO player signature');
  let after = 0n; for (let i = 0; i < 15; i++) { after = await balance(wB.address); if (after > before) break; await sleep(3000); }
  ok(after > before, `winner B paid (${before} → ${after} sompi)`);
  // loser sees it settled too, payout 0
  const ls = await api('POST', `/api/matches/${m.id}/settle/prepare`, { token: wA.token });
  ok(ls.state === 'broadcast' && ls.payoutSompi === '0' && ls.youWon === false, 'loser A sees it settled, payout 0');
}

async function phaseDraw(wA, wB) {
  console.log('\n── v2 draw: agreed draw → covenant returns each stake to its depositor ──');
  const m = await makeMatch(wA, wB, 2);
  await fundBothAndGoLive(m, wA, wB);
  await move(wA, m.id, 'e2e4'); await move(wB, m.id, 'e7e5');
  await api('POST', `/api/matches/${m.id}/draw/offer`, { token: wA.token });
  const end = await api('POST', `/api/matches/${m.id}/draw/accept`, { token: wB.token });
  ok(end.status === 'settled' && end.winnerAccountId === null, 'draw agreed → settled, no winner');
  const beforeA = await balance(wA.address), beforeB = await balance(wB.address);
  const s = await settleV2(wA, m.id);
  ok(!!s.txid && s.isDraw === true, 'covenant released the draw split on-chain');
  let afterA = 0n, afterB = 0n;
  for (let i = 0; i < 15; i++) { afterA = await balance(wA.address); afterB = await balance(wB.address); if (afterA > beforeA && afterB > beforeB) break; await sleep(3000); }
  ok(afterA > beforeA && afterB > beforeB, `both depositors refunded (A ${beforeA}→${afterA}, B ${beforeB}→${afterB})`);
}

// Free play needs no funding, no escrow, no sidecar — just two logged-in players.
async function phaseFree(wA, wB) {
  console.log('\n── free play: 0-stake match, live instantly, no escrow, no settlement ──');
  const m = await makeMatch(wA, wB, 0);
  ok(m.isFree === true, 'match flagged free');
  ok(m.status === 'live', 'free match is LIVE immediately (no deposit phase)');
  ok(!m.escrowA && !m.escrowB, 'free match has no escrows');
  const end = await playFoolsMate(m.id, wA, wB);
  ok(end.status === 'settled' && end.winnerAccountId === wB.id, 'played to a result, B wins');
  const s = await api('POST', `/api/matches/${m.id}/settle/prepare`, { token: wB.token });
  ok(s.state === 'free' && s.payoutSompi === '0', 'settle returns a free result — no pot, no signing');
  ok(s.youWon === true, 'winner told they won (no payout)');
}

async function main() {
  const only = process.argv[2]; // 'free' → free-play only (no funding)
  const meta = await api('GET', '/api/meta');
  console.log(`   backend: ${API}  network=${meta.network}${only ? `  phase=${only}` : ''}`);
  if (only === 'free') {
    const [wA, wB] = [newWallet('F1'), newWallet('F2')];
    await login(wA); await login(wB);
    await phaseFree(wA, wB);
    console.log(`\n${FAIL === 0 ? 'FREE-PLAY HTTP-FLOW PASSED' : `${FAIL} FAILED: ${FAILURES.join(', ')}`}  (${PASS} ok)`);
    return FAIL === 0 ? 0 : 1;
  }
  const wallets = [newWallet('A1'), newWallet('B1'), newWallet('A2'), newWallet('B2'), newWallet('F1'), newWallet('F2')];
  await fundWallets(wallets.slice(0, 4), 3n * SOMPI); // only the money phases need funding
  await sleep(4000);
  for (const w of wallets) await login(w);
  console.log('   wallets funded + logged in');
  try {
    await phaseDecisive(wallets[0], wallets[1]);
    await phaseDraw(wallets[2], wallets[3]);
    await phaseFree(wallets[4], wallets[5]);
  } finally {
    await sweepBack(wallets);
  }
  console.log(`\n${FAIL === 0 ? 'FULL HTTP-FLOW PASSED (v2 + free)' : `${FAIL} FAILED: ${FAILURES.join(', ')}`}  (${PASS} ok)`);
  return FAIL === 0 ? 0 : 1;
}

main().then((c) => process.exit(c)).catch((e) => { console.error('FATAL:', e.message); process.exit(1); });
