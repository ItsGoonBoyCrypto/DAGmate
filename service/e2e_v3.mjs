// End-to-end proof of the PRODUCTION escrow_v3.js module on mainnet dust — not the spike. Exercises
// the real public API (buildEscrowV3, oracleSignResult, settleV3, forfeitClaim, forfeitFinalise) and
// drives the forfeit checkpoint through the real move_channel.mjs (session keys).
//
// STRUCTURE: each phase is its OWN short core.withRpc — settleV3/forfeitClaim/forfeitFinalise already
// open their own, and core bounds a single withRpc callback to 60s (a hung node is dropped), so one
// long-lived callback would trip that bound. Waits (funding, challenge window) poll via one quick
// withRpc each, so a stuck node retries fast instead of blocking the whole run.
// Run as dagmate-svc (mnemonic credential):  node e2e_v3.mjs
import { randomBytes } from 'node:crypto';
import * as core from './core.js';
import * as v3 from './escrow_v3.js';
import * as mc from './move_channel.mjs';

const k = core.wasm();
const NET = core.netType(), NETWORK_ID = core.network();
const DUST = 100_000_000n;
let critical = false;
const expect = (label, ok, extra = '') => { if (!ok) critical = true; console.log(`   ${ok ? 'ok ' : 'BAD'} ${label}${extra ? ' — ' + extra : ''}`); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const newKey = () => { const priv = randomBytes(32).toString('hex'); const key = new k.PrivateKey(priv); return { key, priv, address: key.toPublicKey().toAddress(NET).toString(), xo: String(key.toPublicKey().toXOnlyPublicKey().toString()).replace(/^0x/, '') }; };
// one short withRpc per call, retried on the transient public-pool timeout
const rpcCall = async (label, fn, n = 4) => { let last; for (let i = 0; i < n; i++) { try { return await core.withRpc(fn); } catch (e) { last = e; console.log(`   (retry ${label} ${i + 1}/${n}: ${String(e.message).slice(0, 70)})`); await sleep(4000); } } throw last; };
const daaNow = () => rpcCall('daa', async (rpc) => BigInt((await rpc.getBlockDagInfo()).virtualDaaScore));
const utxosAt = (addr) => rpcCall('utxos', async (rpc) => (await rpc.getUtxosByAddresses({ addresses: [addr] })).entries);
async function waitUtxo(addr, tries = 25) { for (let i = 0; i < tries; i++) { const e = await utxosAt(addr); if (e.length) return e; await sleep(3000); } throw new Error('fund timeout ' + addr); }

const A = newKey(), B = newKey();
const SA = mc.newSessionKey(), SB = mc.newSessionKey();
const wDaa = 60;
const mSettle = randomBytes(3).readUIntBE(0, 3), mForfeit = randomBytes(3).readUIntBE(0, 3);
const now0 = await daaNow();
const reclaimDaa = now0 + 10_000_000n;

const eS = v3.buildEscrowV3({ matchId: mSettle, pkA: A.xo, pkB: B.xo, side: 'A', reclaimDaa, sessPkA: SA.xonlyHex, sessPkB: SB.xonlyHex, wDaa });
const eF = v3.buildEscrowV3({ matchId: mForfeit, pkA: A.xo, pkB: B.xo, side: 'A', reclaimDaa, sessPkA: SA.xonlyHex, sessPkB: SB.xonlyHex, wDaa });
console.log('   settle escrow: ', eS.address);
console.log('   forfeit escrow:', eF.address);
console.log(`   [recovery] mSettle=${mSettle} mForfeit=${mForfeit} A.priv=${A.priv} B.priv=${B.priv} SA=${SA.privHex} SB=${SB.privHex} wDaa=${wDaa} reclaimDaa=${reclaimDaa}`);

// ── PHASE 1: fund both escrows (own withRpc), then poll for confirmation ──
const { address: opAddr, key: opKey } = core.operatingAddress();
await rpcCall('fund', async (rpc) => {
  const { entries: opE } = await rpc.getUtxosByAddresses({ addresses: [opAddr] });
  const { transactions } = await k.createTransactions({ entries: opE, outputs: [{ address: eS.address, amount: DUST }, { address: eF.address, amount: DUST }], changeAddress: opAddr, priorityFee: 20_000_000n, networkId: NETWORK_ID });
  for (const tx of transactions) { tx.sign([opKey]); await tx.submit(rpc); }
});
await waitUtxo(eS.address); await waitUtxo(eF.address);

// ── PHASE 2: SETTLE (oracle A won → settleV3 pays A) ──
const verdict = v3.oracleSignResult({ matchId: mSettle, outcome: 'A' });
let settled;
try { settled = await v3.settleV3({ escrows: [{ address: eS.address, redeemHex: eS.redeemHex, side: 'A' }], outcome: 'A', pkA: A.xo, pkB: B.xo, sigA: verdict.sigA, sigB: verdict.sigB }); }
catch (e) { console.log('   settle err:', String(e.message).slice(0, 200)); }
expect('settleV3 (A won) pays A', !!settled?.txid, settled?.txid);

// ── PHASE 3: FORFEIT (A claims a timeout with a move_channel-signed checkpoint) ──
const past = now0 - 200n, PLY = 40;
const cp = { matchTag: eF.checkpointTag, deadlineDaa: past, ply: PLY, claimant: mc.CLAIMANT.A };
const sigA = Buffer.from(mc.signCheckpoint(cp, SA.privHex)).toString('hex');
const sigB = Buffer.from(mc.signCheckpoint(cp, SB.privHex)).toString('hex');
expect('checkpoint co-sigs verify off-chain', mc.verifyCheckpoint(cp, Buffer.from(sigA, 'hex'), SA.xonlyHex) && mc.verifyCheckpoint(cp, Buffer.from(sigB, 'hex'), SB.xonlyHex));
let claim;
try { claim = await v3.forfeitClaim({ escrow: { address: eF.address, redeemHex: eF.redeemHex }, matchId: mForfeit, pkA: A.xo, pkB: B.xo, sessPkA: SA.xonlyHex, sessPkB: SB.xonlyHex, wDaa, deadlineDaa: past, ply: PLY, claimant: 'A', sigA, sigB }); }
catch (e) { console.log('   forfeitClaim err:', String(e.message).slice(0, 200)); }
expect('forfeitClaim spends escrow into the pending covenant', !!claim?.txid, claim?.pendingAddress);

// ── PHASE 4+5: wait out the window, then finalise to A ──
if (claim?.pendingAddress) {
  await sleep(4000);
  const landed = await utxosAt(claim.pendingAddress);
  expect('pot landed at the reconstructed pending address', landed.length > 0);
  if (landed.length) {
    const inDaa = BigInt(landed[0].blockDaaScore);
    console.log(`   waiting for the ${wDaa}-DAA window (+margin)...`);
    for (;;) { if ((await daaNow()) >= inDaa + BigInt(wDaa) + 15n) break; await sleep(3000); }
    let fin;
    for (let i = 0; i < 4 && !fin?.txid; i++) { if (i) await sleep(4000); try { fin = await v3.forfeitFinalise({ pendingRedeem: claim.pendingRedeem, pendingAddress: claim.pendingAddress, claimant: 'A', pkA: A.xo, pkB: B.xo, wDaa }); } catch (e) { if (i === 3) console.log('   finalise err:', String(e.message).slice(0, 200)); } }
    expect('forfeitFinalise pays the claimant A', !!fin?.txid, fin?.txid);
  }
}

// ── sweep A's winnings back ──
await sleep(4000);
try {
  const entries = await utxosAt(A.address);
  if (entries.length) await rpcCall('sweep', async (rpc) => { const { transactions: sw } = await k.createTransactions({ entries, outputs: [], changeAddress: opAddr, priorityFee: 0n, networkId: NETWORK_ID }); for (const tx of sw) { tx.sign([A.key]); await tx.submit(rpc); } });
} catch (e) { console.log('   sweep note:', String(e.message).slice(0, 120)); }

console.log(critical ? '\nE2E V3 FAILED.' : '\nE2E V3 PASSED — production escrow_v3.js: oracle settle + move_channel-driven forfeit + finalise all work on mainnet.');
process.exit(critical ? 1 : 0);
