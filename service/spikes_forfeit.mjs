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

const which = process.argv[2];
if (which === 'S8') await s8();
else { console.error('usage: node spikes_forfeit.mjs [S8]'); process.exit(1); }
process.exit(0);
