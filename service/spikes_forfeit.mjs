/**
 * DAGmate — Roadmap #3a forfeit-covenant spikes on mainnet dust (docs/DAGMATE_ROADMAP_3A.md).
 * The choreography is developed + adversarially checked offline in scriptsim.mjs / dev_s8.mjs; these
 * spikes confirm the REAL crypto the sim mocks (OpCheckSigFromStack over the co-signed checkpoint
 * hash, OpCheckLockTimeVerify against the tx's DAA locktime, output introspection).
 *
 *   node spikes_forfeit.mjs S8   — co-signed checkpoint + DAA-CLTV forfeit leg
 *
 * Same dust discipline as spikes_covenant.mjs; run as dagmate-svc with the mnemonic credential.
 */
import { randomBytes, createHash } from 'node:crypto';
import * as core from './core.js';
import { numToBytes } from './scriptsim.mjs';

const k = core.wasm();
const NET = core.netType();
const NETWORK_ID = core.network();
const DUST = 100_000_000n;
const sha256 = (b) => createHash('sha256').update(b).digest();

function newKey() {
  const key = new k.PrivateKey(randomBytes(32).toString('hex'));
  return { key, address: key.toPublicKey().toAddress(NET).toString() };
}
function xOnly(key) { return Uint8Array.from(Buffer.from(String(key.toPublicKey().toXOnlyPublicKey().toString()).replace(/^0x/, ''), 'hex')); }
function outputSpkBytes(address) {
  const spk = k.payToAddressScript(address); const v = Number(spk.version) & 0xffff;
  return Uint8Array.from([(v >> 8) & 0xff, v & 0xff, ...Buffer.from(String(spk.script), 'hex')]);
}
const oracleSchnorr = (sigHex) => { const b = Buffer.from(String(sigHex), 'hex'); return b.length === 66 ? b.subarray(1, 65) : b.length === 65 ? b.subarray(0, 64) : b; };
const signHash = (hash32, key) => oracleSchnorr(k.signScriptHash(Buffer.from(hash32).toString('hex'), key));

async function fundFrom(rpc, toAddr, sompi) {
  const { address: opAddr, key: opKey } = core.operatingAddress();
  const { entries } = await rpc.getUtxosByAddresses({ addresses: [opAddr] });
  if (!entries.length) throw new Error('operating address has no UTXOs');
  const { transactions } = await k.createTransactions({ entries, outputs: [{ address: toAddr, amount: sompi }], changeAddress: opAddr, priorityFee: 20_000_000n, networkId: NETWORK_ID });
  let txid = null; for (const tx of transactions) { tx.sign([opKey]); txid = await tx.submit(rpc); } return txid;
}
async function waitUtxo(rpc, address, tries = 20) { for (let i = 0; i < tries; i++) { const { entries } = await rpc.getUtxosByAddresses({ addresses: [address] }); if (entries.length) return entries; await new Promise((r) => setTimeout(r, 3000)); } throw new Error('timeout waiting for UTXO'); }
async function sweepBack(rpc, fromAddr, fromKey) { const { entries } = await rpc.getUtxosByAddresses({ addresses: [fromAddr] }); if (!entries.length) return; const { address: opAddr } = core.operatingAddress(); const { transactions } = await k.createTransactions({ entries, outputs: [], changeAddress: opAddr, priorityFee: 0n, networkId: NETWORK_ID }); for (const tx of transactions) { tx.sign([fromKey]); await tx.submit(rpc); } }

/** Translate a token list (Buffers = data pushes, strings = opcodes) into a redeem hex. */
function buildScript(tokens) {
  const sb = new k.ScriptBuilder(core.COVENANT_OPTS);
  for (const t of tokens) {
    if (typeof t === 'string') sb.addOp(k.Opcodes[t]);
    else sb.addData(Uint8Array.from(t));
  }
  return sb.drain();
}
function p2shFor(redeemHex) { const spk = k.ScriptBuilder.fromScript(redeemHex, core.COVENANT_OPTS).createPayToScriptHashScript(); return k.addressFromScriptPublicKey(spk, NET).toString(); }
function p2shSig(redeemHex, witnessTokens) { const w = buildScript(witnessTokens); return k.ScriptBuilder.fromScript(redeemHex, core.COVENANT_OPTS).encodePayToScriptHashSignatureScript(w); }

// The S8 forfeit leg (exactly the token list validated in dev_s8.mjs). Baked: matchTag, pkA, pkB,
// spkA, spkB, maxFee. Witness (bottom→top): turn, deadline(8B LE), sigA(64), sigB(64).
function forfeitLeg({ matchTag, pkA, pkB, spkA, spkB, maxFee }) {
  return [
    Buffer.from(matchTag), numToBytes(3), 'OpPick', 'OpCat', numToBytes(4), 'OpPick', 'OpCat', 'OpSHA256',
    numToBytes(1), 'OpPick', numToBytes(1), 'OpPick', Buffer.from(pkB), 'OpCheckSigFromStack', 'OpVerify',
    numToBytes(2), 'OpPick', numToBytes(1), 'OpPick', Buffer.from(pkA), 'OpCheckSigFromStack', 'OpVerify',
    'OpDrop', 'OpDrop', 'OpDrop',
    'OpCheckLockTimeVerify',
    Buffer.from([0x02]), 'OpEqual', 'OpIf', Buffer.from(spkA), 'OpElse', Buffer.from(spkB), 'OpEndIf',
    'OpTxInputIndex', 'OpTxOutputSpk', 'OpEqualVerify',
    'OpTxInputIndex', 'OpTxOutputAmount', numToBytes(maxFee), 'OpAdd', 'OpTxInputIndex', 'OpTxInputAmount', 'OpGreaterThanOrEqual',
  ];
}

async function trySpendForfeit(rpc, redeemHex, entries, payAddr, deadlineDaa, turnByte, sigA, sigB) {
  const total = entries.reduce((s, e) => s + BigInt(e.amount), 0n);
  const fee = 5_000_000n;
  // sigOpCount commits the input's COMPUTE-MASS budget. The forfeit leg does 2 CheckSigFromStack
  // + OpPick/OpCat/OpSHA256 over 64-byte sigs (~200k units), so 1 sig-op (~110k) is too little.
  const tx = k.createTransaction(entries, [{ address: payAddr, amount: total - fee }], fee, undefined, 3);
  tx.lockTime = BigInt(deadlineDaa); // CLTV target
  const ins = tx.inputs;
  const dl = Buffer.from(numToBytes(BigInt(deadlineDaa))); // minimal script-number, matches the signed hash
  ins[0].sequence = 0n; // != MAX or CLTV/finality is skipped
  ins[0].signatureScript = p2shSig(redeemHex, [Buffer.from([turnByte]), dl, sigA, sigB]);
  tx.inputs = ins;
  try { const resp = await rpc.submitTransaction({ transaction: tx, allowOrphan: false }); return { accepted: true, id: String(resp.transactionId ?? resp) }; }
  catch (e) { return { accepted: false, err: String(e?.message ?? e).replace(/\s+/g, ' ').slice(0, 400) }; }
}

async function s8() {
  console.log('S8 — co-signed checkpoint + DAA-CLTV forfeit leg, mainnet dust');
  let critical = false;
  const expect = (label, r, wantAccept) => {
    const good = r.accepted === wantAccept;
    if (!good) critical = true;
    console.log(`   ${good ? 'ok ' : 'BAD'} ${label} → ${r.accepted ? 'ACCEPTED ' + (r.id || '') : 'rejected [' + r.err + ']'}`);
  };
  await core.withRpc(async (rpc) => {
    const A = newKey(), B = newKey(); // A will forfeit (turn=0x00), B claims
    const matchTag = randomBytes(32);
    const consts = { matchTag, pkA: xOnly(A.key), pkB: xOnly(B.key), spkA: outputSpkBytes(A.address), spkB: outputSpkBytes(B.address), maxFee: 15_000_000n };
    const redeemHex = buildScript(forfeitLeg(consts));
    const escrowAddr = p2shFor(redeemHex);
    console.log('   escrow:', escrowAddr);

    const info = await rpc.getBlockDagInfo();
    const nowDaa = BigInt(info.virtualDaaScore);
    const turn = 0x01; // A to move → A forfeits → B claims (0x02 would be B; never 0x00)
    const checkpoint = (deadlineDaa) => {
      const dl = Buffer.from(numToBytes(BigInt(deadlineDaa)));
      const hC = sha256(Buffer.concat([Buffer.from(matchTag), dl, Buffer.from([turn])]));
      return { sigA: signHash(hC, A.key), sigB: signHash(hC, B.key) };
    };

    await fundFrom(rpc, escrowAddr, DUST);
    const entries = await waitUtxo(rpc, escrowAddr);

    // 1) deadline in the FUTURE → timeout not reached → reject
    const future = nowDaa + 5_000n; let s = checkpoint(future);
    expect('deadline not reached (future)', await trySpendForfeit(rpc, redeemHex, entries, B.address, future, turn, s.sigA, s.sigB), false);

    // 2) valid co-signed checkpoint but FORGED sigB → co-sign not satisfied → reject
    const past = nowDaa - 200n; s = checkpoint(past);
    expect('forged co-signature', await trySpendForfeit(rpc, redeemHex, entries, B.address, past, turn, s.sigA, randomBytes(64)), false);

    // 3) pay the FORFEITER (A) instead of the claimant (B) → spk mismatch → reject
    expect('pot paid to the forfeiter', await trySpendForfeit(rpc, redeemHex, entries, A.address, past, turn, s.sigA, s.sigB), false);

    // 4) HONEST forfeit: past deadline, both co-signed, pays claimant B → accept
    const honest = await trySpendForfeit(rpc, redeemHex, entries, B.address, past, turn, s.sigA, s.sigB);
    expect('honest forfeit claim (pays claimant B)', honest, true);
    if (honest.accepted) { await new Promise((r) => setTimeout(r, 4000)); await sweepBack(rpc, B.address, B.key); }
    console.log(critical ? 'S8 FAILED — a case misbehaved on-chain.' : 'S8 PASSED — co-signed checkpoint + DAA-CLTV forfeit works; every attack rejected on-chain.');
  });
}

// The S9 leg (C + bounded unilateral move M), exactly the token list validated in dev_s9.mjs.
// Witness (bottom→top): B, D_C, turn, D_M, sigCA, sigCB, sigM.
function forfeitLegS9({ matchTag, pkA, pkB, spkA, spkB, maxFee, increment }) {
  return [
    Buffer.from(matchTag), numToBytes(6), 'OpPick', 'OpCat', numToBytes(5), 'OpPick', 'OpCat', numToBytes(7), 'OpPick', 'OpCat', 'OpSHA256',
    numToBytes(2), 'OpPick', numToBytes(1), 'OpPick', Buffer.from(pkB), 'OpCheckSigFromStack', 'OpVerify',
    numToBytes(3), 'OpPick', numToBytes(1), 'OpPick', Buffer.from(pkA), 'OpCheckSigFromStack', 'OpVerify',
    numToBytes(4), 'OpPick', 'OpCat', 'OpSHA256',
    numToBytes(1), 'OpPick', numToBytes(1), 'OpPick',
    numToBytes(7), 'OpPick', Buffer.from([0x02]), 'OpEqual', 'OpIf', Buffer.from(pkB), 'OpElse', Buffer.from(pkA), 'OpEndIf',
    'OpCheckSigFromStack', 'OpVerify',
    'OpDrop', 'OpDrop', 'OpDrop', 'OpDrop',
    numToBytes(2), 'OpPick', numToBytes(4), 'OpPick', 'OpAdd', numToBytes(increment), 'OpAdd',
    numToBytes(1), 'OpPick', 'OpSwap', 'OpGreaterThanOrEqual', 'OpVerify',
    'OpCheckLockTimeVerify', 'OpNip', 'OpNip',
    Buffer.from([0x01]), 'OpEqual', 'OpIf', Buffer.from(spkA), 'OpElse', Buffer.from(spkB), 'OpEndIf',
    'OpTxInputIndex', 'OpTxOutputSpk', 'OpEqualVerify',
    'OpTxInputIndex', 'OpTxOutputAmount', numToBytes(maxFee), 'OpAdd', 'OpTxInputIndex', 'OpTxInputAmount', 'OpGreaterThanOrEqual',
  ];
}

async function trySpendS9(rpc, redeemHex, entries, payAddr, witnessNums, sigs) {
  const total = entries.reduce((s, e) => s + BigInt(e.amount), 0n); const fee = 5_000_000n;
  const tx = k.createTransaction(entries, [{ address: payAddr, amount: total - fee }], fee, undefined, 5); // 3 sigs → more compute
  tx.lockTime = BigInt(witnessNums.D_M);
  const num = (n) => Buffer.from(numToBytes(BigInt(n)));
  const ins = tx.inputs; ins[0].sequence = 0n;
  ins[0].signatureScript = p2shSig(redeemHex, [num(witnessNums.B), num(witnessNums.D_C), Buffer.from([witnessNums.turn]), num(witnessNums.D_M), sigs.sigCA, sigs.sigCB, sigs.sigM]);
  tx.inputs = ins;
  try { const resp = await rpc.submitTransaction({ transaction: tx, allowOrphan: false }); return { accepted: true, id: String(resp.transactionId ?? resp) }; }
  catch (e) { return { accepted: false, err: String(e?.message ?? e).replace(/\s+/g, ' ').slice(0, 400) }; }
}

async function s9() {
  console.log('S9 — co-signed checkpoint + BOUNDED unilateral move forfeit leg, mainnet dust');
  let critical = false;
  const expect = (label, r, wantAccept) => { const good = r.accepted === wantAccept; if (!good) critical = true; console.log(`   ${good ? 'ok ' : 'BAD'} ${label} → ${r.accepted ? 'ACCEPTED ' + (r.id || '') : 'rejected [' + r.err + ']'}`); };
  await core.withRpc(async (rpc) => {
    const A = newKey(), B = newKey(); // A = mover X = claimant; B (Y) flagged
    const matchTag = randomBytes(32); const INCREMENT = 20n, BUD = 1000n, turn = 0x01;
    const consts = { matchTag, pkA: xOnly(A.key), pkB: xOnly(B.key), spkA: outputSpkBytes(A.address), spkB: outputSpkBytes(B.address), maxFee: 15_000_000n, increment: INCREMENT };
    const redeemHex = buildScript(forfeitLegS9(consts));
    const escrowAddr = p2shFor(redeemHex); console.log('   escrow:', escrowAddr);
    const info = await rpc.getBlockDagInfo(); const nowDaa = BigInt(info.virtualDaaScore);
    const num = (n) => Buffer.from(numToBytes(BigInt(n)));
    const sign = (D_C, D_M) => {
      const hC = sha256(Buffer.concat([Buffer.from(matchTag), num(D_C), Buffer.from([turn]), num(BUD)]));
      const hM = sha256(Buffer.concat([hC, num(D_M)]));
      return { sigCA: signHash(hC, A.key), sigCB: signHash(hC, B.key), sigM: signHash(hM, A.key) };
    };
    await fundFrom(rpc, escrowAddr, DUST); const entries = await waitUtxo(rpc, escrowAddr);

    // honest: D_M = D_C + BUD + INC (min allowed), all past so CLTV/finality OK
    const D_M = nowDaa - 200n, D_C = D_M - BUD - INCREMENT;
    let s = sign(D_C, D_M);
    // 1) SHORT deadline steal: D_M' < D_C+BUD+INC → lower bound rejects
    const shortDM = D_C + 10n; const ss = sign(D_C, shortDM);
    expect('short-deadline steal (D_M < bound)', await trySpendS9(rpc, redeemHex, entries, A.address, { B: BUD, D_C, turn, D_M: shortDM }, ss), false);
    // 2) forged M sig → reject
    expect('forged move signature', await trySpendS9(rpc, redeemHex, entries, A.address, { B: BUD, D_C, turn, D_M }, { ...s, sigM: randomBytes(64) }), false);
    // 3) pay the flagger B instead of the claimant A → reject
    expect('paid the flagger not the claimant', await trySpendS9(rpc, redeemHex, entries, B.address, { B: BUD, D_C, turn, D_M }, s), false);
    // 4) HONEST bounded forfeit → accept
    const honest = await trySpendS9(rpc, redeemHex, entries, A.address, { B: BUD, D_C, turn, D_M }, s);
    expect('honest bounded forfeit (pays claimant A)', honest, true);
    if (honest.accepted) { await new Promise((r) => setTimeout(r, 4000)); await sweepBack(rpc, A.address, A.key); }
    console.log(critical ? 'S9 FAILED.' : 'S9 PASSED — bounded unilateral move forfeit works; the short-deadline steal is rejected on-chain.');
  });
}

// The S10 PENDING-FORFEIT covenant (optimistic challenge window), validated in dev_s10.mjs.
// Baked: spkX, spkY, claimedPly, W, pkA, pkB, matchTag, maxFee.
//   FINALIZE: witness [OP_TRUE]  — X after DAA ≥ (pending UTXO DAA)+W.
//   CANCEL:   witness [sigA' sigB' ply' OP_FALSE] — Y with a newer co-signed checkpoint.
function pendingLeg({ spkX, spkY, claimedPly, W, pkA, pkB, matchTag, maxFee }) {
  return [
    'OpIf',
      'OpTxInputIndex', 'OpTxInputDaaScore', numToBytes(W), 'OpAdd', 'OpCheckLockTimeVerify',
      'OpTxInputIndex', 'OpTxOutputSpk', Buffer.from(spkX), 'OpEqualVerify',
      'OpTxInputIndex', 'OpTxOutputAmount', numToBytes(maxFee), 'OpAdd', 'OpTxInputIndex', 'OpTxInputAmount', 'OpGreaterThanOrEqual',
    'OpElse',
      Buffer.from(matchTag), numToBytes(1), 'OpPick', 'OpCat', 'OpSHA256',
      numToBytes(2), 'OpPick', numToBytes(1), 'OpPick', Buffer.from(pkB), 'OpCheckSigFromStack', 'OpVerify',
      numToBytes(3), 'OpPick', numToBytes(1), 'OpPick', Buffer.from(pkA), 'OpCheckSigFromStack', 'OpVerify',
      'OpDrop', 'OpNip', 'OpNip',
      numToBytes(claimedPly), 'OpGreaterThan', 'OpVerify',
      'OpTxInputIndex', 'OpTxOutputSpk', Buffer.from(spkY), 'OpEqualVerify',
      'OpTxInputIndex', 'OpTxOutputAmount', numToBytes(maxFee), 'OpAdd', 'OpTxInputIndex', 'OpTxInputAmount', 'OpGreaterThanOrEqual',
    'OpEndIf',
  ];
}

async function s10() {
  console.log('S10 — pending-forfeit covenant (optimistic challenge window), mainnet dust');
  let critical = false;
  const expect = (label, r, want) => { const good = r.accepted === want; if (!good) critical = true; console.log(`   ${good ? 'ok ' : 'BAD'} ${label} → ${r.accepted ? 'ACCEPTED ' + (r.id || '') : 'rejected [' + r.err + ']'}`); };
  await core.withRpc(async (rpc) => {
    const X = newKey(), Y = newKey(); // A=X claimant, B=Y canceller
    const matchTag = randomBytes(32), claimedPly = 40n, W = 30n;
    const consts = { spkX: outputSpkBytes(X.address), spkY: outputSpkBytes(Y.address), claimedPly, W, pkA: xOnly(X.key), pkB: xOnly(Y.key), matchTag, maxFee: 15_000_000n };
    const redeemHex = buildScript(pendingLeg(consts));
    const pendAddr = p2shFor(redeemHex); console.log('   pending covenant:', pendAddr);
    const num = (n) => Buffer.from(numToBytes(BigInt(n)));
    const signCp = (ply) => { const h = sha256(Buffer.concat([Buffer.from(matchTag), num(ply)])); return { sigA: signHash(h, X.key), sigB: signHash(h, Y.key) }; };

    // fund TWO pending UTXOs (one for cancel, one for finalize) in one tx
    const { address: opAddr, key: opKey } = core.operatingAddress();
    const { entries: opE } = await rpc.getUtxosByAddresses({ addresses: [opAddr] });
    const { transactions } = await k.createTransactions({ entries: opE, outputs: [{ address: pendAddr, amount: DUST }, { address: pendAddr, amount: DUST }], changeAddress: opAddr, priorityFee: 20_000_000n, networkId: NETWORK_ID });
    for (const tx of transactions) { tx.sign([opKey]); await tx.submit(rpc); }
    const all = await waitUtxo(rpc, pendAddr); // 2 utxos
    while ((await rpc.getUtxosByAddresses({ addresses: [pendAddr] })).entries.length < 2) await new Promise((r) => setTimeout(r, 3000));
    const utxos = (await rpc.getUtxosByAddresses({ addresses: [pendAddr] })).entries;
    const u1 = [utxos[0]], u2 = [utxos[1]];

    const spendCancel = async (input, ply, sigs, payAddr) => {
      const total = BigInt(input[0].amount), fee = 5_000_000n;
      const tx = k.createTransaction(input, [{ address: payAddr, amount: total - fee }], fee, undefined, 4);
      const ins = tx.inputs; ins[0].signatureScript = p2shSig(redeemHex, [sigs.sigA, sigs.sigB, num(ply), Buffer.alloc(0)]); tx.inputs = ins;
      try { const resp = await rpc.submitTransaction({ transaction: tx, allowOrphan: false }); return { accepted: true, id: String(resp.transactionId ?? resp) }; }
      catch (e) { return { accepted: false, err: String(e?.message ?? e).replace(/\s+/g, ' ').slice(0, 300) }; }
    };
    const spendFinalize = async (input, lockTime, payAddr) => {
      const total = BigInt(input[0].amount), fee = 5_000_000n;
      const tx = k.createTransaction(input, [{ address: payAddr, amount: total - fee }], fee, undefined, 2);
      tx.lockTime = BigInt(lockTime); const ins = tx.inputs; ins[0].sequence = 0n; ins[0].signatureScript = p2shSig(redeemHex, [numToBytes(1)]); tx.inputs = ins;
      try { const resp = await rpc.submitTransaction({ transaction: tx, allowOrphan: false }); return { accepted: true, id: String(resp.transactionId ?? resp) }; }
      catch (e) { return { accepted: false, err: String(e?.message ?? e).replace(/\s+/g, ' ').slice(0, 300) }; }
    };

    // ── CANCEL branch on u1 ──
    expect('cancel STALE (ply==claimed)', await spendCancel(u1, claimedPly, signCp(claimedPly), Y.address), false);
    expect('cancel forged co-sig', await spendCancel(u1, 42n, { ...signCp(42n), sigB: randomBytes(64) }, Y.address), false);
    expect('cancel pays X not Y', await spendCancel(u1, 42n, signCp(42n), X.address), false);
    expect('cancel NEWER (ply>claimed) pays Y', await spendCancel(u1, 42n, signCp(42n), Y.address), true);

    // ── FINALIZE branch on u2 ──
    const inDaa = BigInt(u2[0].blockDaaScore);
    expect('finalize BEFORE window', await spendFinalize(u2, inDaa + W, Y.address), false); // also wrong payee, but window not open yet
    console.log(`   waiting for the ${W}-DAA window to pass...`);
    for (;;) { const now = BigInt((await rpc.getBlockDagInfo()).virtualDaaScore); if (now >= inDaa + W) break; await new Promise((r) => setTimeout(r, 3000)); }
    const fin = await spendFinalize(u2, inDaa + W, X.address);
    expect('finalize AFTER window pays X', fin, true);

    await new Promise((r) => setTimeout(r, 4000));
    await sweepBack(rpc, Y.address, Y.key); await sweepBack(rpc, X.address, X.key);
    console.log(critical ? 'S10 FAILED.' : 'S10 PASSED — pending-forfeit covenant: finalize-after-window + cancel-by-newer-state both work; stale/forged/wrong-payee/early rejected.');
  });
}

const which = process.argv[2];
if (which === 'S8') await s8();
else if (which === 'S9') await s9();
else if (which === 'S10') await s10();
else { console.error('usage: node spikes_forfeit.mjs [S8|S9|S10]'); process.exit(1); }
process.exit(0);
