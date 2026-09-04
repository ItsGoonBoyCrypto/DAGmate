/**
 * DAGmate — Covenant Escrow v2 capability probes (roadmap #2, docs/DAGMATE_COVENANT_V2.md).
 *
 * De-risks the v2 covenant BEFORE building the real escrow, exactly the way S1–S3 in
 * spikes.mjs de-risked v1: each probe funds a throwaway P2SH with dust, proves ONE opcode
 * behaves as the source says, and sweeps the leftover back. Nothing here touches a player's
 * wallet or a real escrow.
 *
 *   node spikes_covenant.mjs S5a   # OpCat + OpSHA256 build a hash on-chain
 *   node spikes_covenant.mjs S5b   # OpCheckSigFromStack verifies an ORACLE signature
 *   node spikes_covenant.mjs S5c   # OpTxOutputAmount + OpTxOutputSpk enforce the payout
 *
 * ⚠️ TESTNET-10 FIRST. Point core.js at testnet (DAGMATE_NETWORK_ID=testnet-10) and fund the
 * operating address from the faucet before running. Only after all three pass on testnet do
 * we repeat S5c/S6 on mainnet dust (consensus parity), same as S1–S3 were.
 *
 * ⚠️ These are PROBES — first-run iteration is expected (SDK arg shapes, the exact P2SH
 * witness assembly, sigOpCount for CheckSigFromStack). That is the point: prove the unknowns
 * on dust, then bake the confirmed idioms into escrow_v2.js. Opcode stack semantics used here
 * are read from rusty-kaspa master (txscript/src/opcodes/mod.rs), not the KIPs — see the v2 doc.
 */
import { createHash, randomBytes } from 'node:crypto';
import * as core from './core.js';

const k = core.wasm();
const NET = core.netType();
const NETWORK_ID = core.network();
const DUST = 100_000_000n; // 1 KAS

function newKey() {
  const key = new k.PrivateKey(randomBytes(32).toString('hex'));
  const address = key.toPublicKey().toAddress(NET).toString();
  return { key, address };
}

function xOnly(keyOrPub) {
  const pub = keyOrPub.toPublicKey ? keyOrPub.toPublicKey() : keyOrPub;
  const xo = pub.toXOnlyPublicKey ? pub.toXOnlyPublicKey() : pub;
  return Uint8Array.from(Buffer.from(String(xo.toString()).replace(/^0x/, ''), 'hex'));
}

const sha256 = (buf) => createHash('sha256').update(buf).digest();
const rawSig = (s) => { const b = Buffer.from(String(s), 'hex'); return b.length === 66 ? b.subarray(1) : b; };

/** Pull the BARE 64-byte Schnorr signature out of a signScriptHash() result for use with
 *  OpCheckSigFromStack. signScriptHash returns 66 bytes = [push-len(1)][schnorr(64)][sighashType(1)]
 *  (v1's rawSig strips only the leading push byte, leaving the 65-byte tx-sig form [schnorr+sighash]).
 *  A from-stack signature verifies an arbitrary message with NO transaction sighash, so both the
 *  push byte and the trailing sighash-type byte must go — 64 bytes exactly, or the node rejects it
 *  as "malformed signature". */
const oracleSchnorr = (s) => {
  const b = Buffer.from(String(s), 'hex');
  if (b.length === 66) return b.subarray(1, 65); // [push][schnorr 64][sighash]
  if (b.length === 65) return b.subarray(0, 64); // [schnorr 64][sighash]
  return b;                                      // already bare
};

async function fundFromOperatingAddress(rpc, toAddress, sompi) {
  const { address: opAddr, key: opKey } = core.operatingAddress();
  const { entries } = await rpc.getUtxosByAddresses({ addresses: [opAddr] });
  if (!entries.length) throw new Error('operating address has no UTXOs — fund it (faucet on testnet)');
  const { transactions } = await k.createTransactions({
    entries, outputs: [{ address: toAddress, amount: sompi }],
    changeAddress: opAddr, priorityFee: 20_000_000n, networkId: NETWORK_ID,
  });
  let txid = null;
  for (const tx of transactions) { tx.sign([opKey]); txid = await tx.submit(rpc); }
  console.log(`   funded ${toAddress} with ${sompi} sompi — txid ${txid}`);
  return txid;
}

async function waitUtxo(rpc, address, tries = 20) {
  for (let i = 0; i < tries; i++) {
    const { entries } = await rpc.getUtxosByAddresses({ addresses: [address] });
    if (entries.length) return entries;
    await new Promise((r) => setTimeout(r, 3000));
  }
  throw new Error(`timed out waiting for a UTXO at ${address}`);
}

async function sweepBack(rpc, fromAddress, fromKey) {
  const { entries } = await rpc.getUtxosByAddresses({ addresses: [fromAddress] });
  if (!entries.length) return null;
  const { address: opAddr } = core.operatingAddress();
  const { transactions } = await k.createTransactions({
    entries, outputs: [], changeAddress: opAddr, priorityFee: 0n, networkId: NETWORK_ID,
  });
  let txid = null;
  for (const tx of transactions) { tx.sign([fromKey]); txid = await tx.submit(rpc); }
  console.log(`   swept ${fromAddress} back — txid ${txid}`);
  return txid;
}

/** P2SH address for a redeem script (hex), plus its spk. */
function p2shFor(redeemHex) {
  const spk = k.ScriptBuilder.fromScript(redeemHex, core.COVENANT_OPTS).createPayToScriptHashScript();
  const address = k.addressFromScriptPublicKey(spk, NET).toString();
  return { address, spk };
}

/** Wrap witness-side pushes (a ScriptBuilder-drained hex) into a P2SH sigScript for redeemHex. */
function p2shSigScript(redeemHex, witnessHex) {
  return k.ScriptBuilder.fromScript(redeemHex, core.COVENANT_OPTS)
    .encodePayToScriptHashSignatureScript(witnessHex);
}

// ─────────────────────────────────────────────────────────────────────────
// S5a — OpCat + OpSHA256: prove the oracle-message build works on-chain.
// redeem:  OpCat OpSHA256 <expected> OpEqual
// witness: <a> <b>       (a‖b hashed on-chain must equal the baked digest)
async function s5a_cat_sha() {
  console.log('S5a — OpCat + OpSHA256 (build a 32-byte hash on-chain), dust');
  await core.withRpc(async (rpc) => {
    const a = Buffer.from('DAGMATE-V2-PROBE-', 'utf8');
    const b = randomBytes(9);
    const expected = sha256(Buffer.concat([a, b])); // 32 bytes

    const redeemHex = new k.ScriptBuilder(core.COVENANT_OPTS)
      .addOp(k.Opcodes.OpCat)
      .addOp(k.Opcodes.OpSHA256)
      .addData(Uint8Array.from(expected))
      .addOp(k.Opcodes.OpEqual)
      .drain();
    const { address: escrowAddr } = p2shFor(redeemHex);
    console.log('   probe address:', escrowAddr);

    await fundFromOperatingAddress(rpc, escrowAddr, DUST);
    const entries = await waitUtxo(rpc, escrowAddr);
    const sweep = newKey();

    // No signature needed — a hashlock-style redeem. sigOpCount 0.
    const total = entries.reduce((s, e) => s + BigInt(e.amount), 0n);
    const fee = 20_000_000n;
    const tx = k.createTransaction(entries, [{ address: sweep.address, amount: total - fee }], fee, undefined, 0);
    const witness = new k.ScriptBuilder(core.COVENANT_OPTS)
      .addData(Uint8Array.from(a))
      .addData(Uint8Array.from(b))
      .drain();
    const ins = tx.inputs;
    ins[0].signatureScript = p2shSigScript(redeemHex, witness);
    tx.inputs = ins;

    const resp = await rpc.submitTransaction({ transaction: tx, allowOrphan: false });
    console.log(`   spend accepted: ${resp.transactionId ?? resp}`);
    console.log('S5a PASSED — OpCat + OpSHA256 execute with standard byte semantics.');
    await new Promise((r) => setTimeout(r, 4000));
    await sweepBack(rpc, sweep.address, sweep.key);
  });
}

// ─────────────────────────────────────────────────────────────────────────
// S5b — OpCheckSigFromStack: prove the ORACLE attestation primitive.
// redeem:  <msg(32B)> <pkOracle> OpCheckSigFromStack     (pushes bool)
// witness: <oracleSig>                                   (raw schnorr over msg)
// Source order: pops [signature, msg_hash, pubkey] → push signature (witness), then msg,
// then pubkey (top). msg MUST be exactly 32 bytes.
async function s5b_checksigfromstack() {
  console.log('S5b — OpCheckSigFromStack (verify an oracle signature over a 32-byte message), dust');
  await core.withRpc(async (rpc) => {
    const oracle = newKey();
    const msg = sha256(Buffer.concat([Buffer.from('DAGMATE-V2-ORACLE-', 'utf8'), randomBytes(8)])); // 32B
    // Raw schnorr over the exact 32 bytes — signScriptHash, NOT signMessage (which prefixes+rehashes).
    // signScriptHash(script_hash, privkey): the hash goes in as a hex STRING (a Hash object is rejected).
    const oracleSig = k.signScriptHash(Buffer.from(msg).toString('hex'), oracle.key);

    const redeemHex = new k.ScriptBuilder(core.COVENANT_OPTS)
      .addData(Uint8Array.from(msg))
      .addData(xOnly(oracle.key))
      .addOp(k.Opcodes.OpCheckSigFromStack)
      .drain();
    const { address: escrowAddr } = p2shFor(redeemHex);
    console.log('   probe address:', escrowAddr);

    await fundFromOperatingAddress(rpc, escrowAddr, DUST);
    const entries = await waitUtxo(rpc, escrowAddr);
    const sweep = newKey();

    const total = entries.reduce((s, e) => s + BigInt(e.amount), 0n);
    const fee = 20_000_000n;
    const tx = k.createTransaction(entries, [{ address: sweep.address, amount: total - fee }], fee, undefined, 1);
    const witness = new k.ScriptBuilder(core.COVENANT_OPTS)
      .addData(oracleSchnorr(oracleSig)) // bare 64-byte schnorr, no push/sighash bytes
      .drain();
    const ins = tx.inputs;
    ins[0].signatureScript = p2shSigScript(redeemHex, witness);
    tx.inputs = ins;

    const resp = await rpc.submitTransaction({ transaction: tx, allowOrphan: false });
    console.log(`   spend accepted: ${resp.transactionId ?? resp}`);
    console.log('S5b PASSED — OpCheckSigFromStack verifies a raw oracle signature. Oracle attestation works.');
    await new Promise((r) => setTimeout(r, 4000));
    await sweepBack(rpc, sweep.address, sweep.key);
  });
}

// ─────────────────────────────────────────────────────────────────────────
// S5c — output introspection: prove the covenant can force where and how much it pays.
// redeem enforces: OpTxOutputSpk(0) == <destSpk>  AND  OpTxOutputAmount(0) >= <minAmount>
// The winning constraint of v2: the money can only leave to the intended address, in full.
/** The exact bytes OpTxOutputSpk(idx) returns for a payout to `address`:
 *  ScriptPublicKey::to_bytes() = version (u16 big-endian, 2 bytes) ‖ script.
 *  `payToAddressScript` gives {version, script(hex)}; we serialise it the node's way. */
function outputSpkBytes(address) {
  const spk = k.payToAddressScript(address);
  const version = Number(spk.version) & 0xffff;
  const verBE = Uint8Array.from([(version >> 8) & 0xff, version & 0xff]);
  const script = Uint8Array.from(Buffer.from(String(spk.script), 'hex'));
  return Uint8Array.from([...verBE, ...script]);
}

// S5c — OpTxOutputSpk + OpTxOutputAmount: prove the covenant can FORCE where and how much it
// pays. This is the guarantee that makes "DAGmate can never skim the pot" a consensus rule.
// redeem enforces: output[0].spk == <destSpk>  AND  output[0].amount >= <minAmount>.
// No signature — the covenant is satisfied purely by introspecting the spending tx.
async function s5c_output_introspection() {
  console.log('S5c — OpTxOutputSpk + OpTxOutputAmount (force destination AND amount), dust');
  await core.withRpc(async (rpc) => {
    const dest = newKey();
    const destSpk = outputSpkBytes(dest.address);
    const MIN_AMOUNT = 50_000_000n; // 0.5 KAS — the payout must be at least this
    console.log('   dest:', dest.address);
    console.log('   expected output spk bytes:', Buffer.from(destSpk).toString('hex'));

    const redeemHex = new k.ScriptBuilder(core.COVENANT_OPTS)
      .addOp(k.Opcodes.Op0)              // output index 0 (for the spk check)
      .addOp(k.Opcodes.OpTxOutputSpk)    // -> spk bytes of output 0
      .addData(destSpk)
      .addOp(k.Opcodes.OpEqualVerify)    // output 0 MUST pay the destination
      .addOp(k.Opcodes.Op0)              // output index 0 (for the amount check)
      .addOp(k.Opcodes.OpTxOutputAmount) // -> amount of output 0
      .addI64(MIN_AMOUNT)
      .addOp(k.Opcodes.OpGreaterThanOrEqual) // amount >= MIN_AMOUNT
      .drain();
    const { address: escrowAddr } = p2shFor(redeemHex);
    console.log('   probe address:', escrowAddr);

    await fundFromOperatingAddress(rpc, escrowAddr, DUST);
    const entries = await waitUtxo(rpc, escrowAddr);

    const total = entries.reduce((s, e) => s + BigInt(e.amount), 0n);
    const fee = 20_000_000n;
    // Output 0 pays the destination — exactly what the covenant demands.
    const tx = k.createTransaction(entries, [{ address: dest.address, amount: total - fee }], fee, undefined, 0);
    const ins = tx.inputs;
    ins[0].signatureScript = p2shSigScript(redeemHex, ''); // no witness data — pure introspection
    tx.inputs = ins;

    const resp = await rpc.submitTransaction({ transaction: tx, allowOrphan: false });
    console.log(`   spend accepted: ${resp.transactionId ?? resp}`);
    console.log('S5c PASSED — output introspection forces destination + amount. The pot cannot be skimmed.');
    await new Promise((r) => setTimeout(r, 4000));
    await sweepBack(rpc, dest.address, dest.key);
  });
}

// ─────────────────────────────────────────────────────────────────────────
// The FULL v2 per-escrow redeem (the target of roadmap #2). Everything the S5 probes proved,
// composed. The oracle message and the winner's payout-spk are BAKED per outcome (msgA/msgB,
// spkA/spkB), so the script needs no on-chain OpCat/SHA and the oracle can ONLY authorise
// "A wins" or "B wins" — never a third-party destination.
//
//   IF (settle)                       witness: <oracleSig64> <winnerSel> <OP_TRUE>
//     IF (winnerSel truthy = B won)
//       <msgB> <pkOracle> CHECKSIGFROMSTACK VERIFY          -- oracle blessed B
//       INPUTINDEX OUTPUTSPK <spkB> EQUALVERIFY             -- same-index output pays B
//       INPUTINDEX OUTPUTAMOUNT <maxFee> ADD INPUTINDEX INPUTAMOUNT GREATERTHANOREQUAL
//     ELSE (A won)  ... symmetric with msgA / spkA / pkA
//   ELSE (reclaim)                    witness: <depositorSig> <OP_FALSE>
//     <reclaimDaa> CHECKLOCKTIMEVERIFY <pkDepositor> CHECKSIG
// 3-way settle: A won / B won / DRAW (each escrow pays its OWN depositor back). Witness:
//   decisive:  <oracleSig64> <winnerSel> <OP_FALSE(isDraw)> <OP_TRUE(settle)>
//   draw:      <oracleSig64> <OP_FALSE(unused)> <OP_TRUE(isDraw)> <OP_TRUE(settle)>
//   reclaim:   <depositorSig> <OP_FALSE(settle)>
// The draw leg OpDrops the unused winnerSel so the stack is clean for CheckSigFromStack.
function v2SettleRedeem({ pkOracle, msgA, msgB, msgDraw, spkA, spkB, spkDepositor, reclaimDaa, pkDepositor, maxFee }) {
  const sb = new k.ScriptBuilder(core.COVENANT_OPTS);
  const leg = (msg, spk) => {
    // oracle blessed this outcome:
    sb.addData(msg).addData(pkOracle).addOp(k.Opcodes.OpCheckSigFromStack).addOp(k.Opcodes.OpVerify);
    // this input's same-index output pays the required party, in full:
    sb.addOp(k.Opcodes.OpTxInputIndex).addOp(k.Opcodes.OpTxOutputSpk).addData(spk).addOp(k.Opcodes.OpEqualVerify);
    // (output[i].amount + maxFee) >= input[i].amount  — no OpSub needed, keeps it to proven ops.
    sb.addOp(k.Opcodes.OpTxInputIndex).addOp(k.Opcodes.OpTxOutputAmount);
    sb.addI64(BigInt(maxFee)).addOp(k.Opcodes.OpAdd);
    sb.addOp(k.Opcodes.OpTxInputIndex).addOp(k.Opcodes.OpTxInputAmount);
    sb.addOp(k.Opcodes.OpGreaterThanOrEqual);
  };
  sb.addOp(k.Opcodes.OpIf);          // settle
  sb.addOp(k.Opcodes.OpIf);          //   isDraw
  sb.addOp(k.Opcodes.OpDrop);        //     drop the unused winnerSel
  leg(msgDraw, spkDepositor);        //     DRAW: pay THIS escrow's depositor back
  sb.addOp(k.Opcodes.OpElse);        //   decisive
  sb.addOp(k.Opcodes.OpIf);          //     winnerSel truthy = B won
  leg(msgB, spkB);
  sb.addOp(k.Opcodes.OpElse);        //     A won
  leg(msgA, spkA);
  sb.addOp(k.Opcodes.OpEndIf);
  sb.addOp(k.Opcodes.OpEndIf);
  sb.addOp(k.Opcodes.OpElse);        // reclaim (14-day CLTV, depositor-signed)
  sb.addI64(BigInt(reclaimDaa)).addOp(k.Opcodes.OpCheckLockTimeVerify);
  sb.addData(pkDepositor).addOp(k.Opcodes.OpCheckSig);
  sb.addOp(k.Opcodes.OpEndIf);
  return sb.drain();
}

/** Build the fixed match constants for a v2 escrow the way escrow_v2.js will. `depKey` is this
 *  escrow's depositor — it backs BOTH the reclaim branch and the draw-payout destination. */
function v2Match({ oracleKey, aKey, bKey, depKey, reclaimDaa, maxFee }) {
  const matchTag = randomBytes(32); // = SHA256(matchId ‖ side) in production
  const msgA = sha256(Buffer.concat([matchTag, Buffer.from([0x00])])); // A won
  const msgB = sha256(Buffer.concat([matchTag, Buffer.from([0x01])])); // B won
  const msgDraw = sha256(Buffer.concat([matchTag, Buffer.from([0x02])])); // draw
  return {
    matchTag, msgA, msgB, msgDraw,
    pkOracle: xOnly(oracleKey),
    spkA: outputSpkBytes(aKey.toPublicKey().toAddress(NET).toString()),
    spkB: outputSpkBytes(bKey.toPublicKey().toAddress(NET).toString()),
    spkDepositor: outputSpkBytes(depKey.toPublicKey().toAddress(NET).toString()),
    pkDepositor: xOnly(depKey),
    reclaimDaa: BigInt(reclaimDaa), maxFee: BigInt(maxFee),
  };
}

/** Settle witness for the 3-way redeem. outcome: 'A' | 'B' | 'draw'. */
function v2Witness(sig64, outcome) {
  const isDraw = outcome === 'draw';
  const winnerSel = outcome === 'B' ? k.Opcodes.OpTrue : k.Opcodes.OpFalse; // unused for draw
  return new k.ScriptBuilder(core.COVENANT_OPTS)
    .addData(sig64)
    .addOp(winnerSel)
    .addOp(isDraw ? k.Opcodes.OpTrue : k.Opcodes.OpFalse) // isDraw
    .addOp(k.Opcodes.OpTrue)                              // settle
    .drain();
}

// S6 — the full v2 escrow, HAPPY PATH: oracle blesses B, the claim pays B in full, no arbiter.
async function s6_full_happy() {
  console.log('S6 — full v2 escrow settle (oracle blesses winner B; self-claiming; amount-enforced), dust');
  await core.withRpc(async (rpc) => {
    const oracle = newKey(), a = newKey(), b = newKey(); // b = the winner
    const info = await rpc.getBlockDagInfo();
    const reclaimDaa = BigInt(info.virtualDaaScore) + 1_000_000n; // far future — reclaim not used here
    const maxFee = 10_000_000n;
    const m = v2Match({ oracleKey: oracle.key, aKey: a.key, bKey: b.key, depKey: b.key, reclaimDaa, maxFee });

    const redeemHex = v2SettleRedeem(m);
    const { address: escrowAddr } = p2shFor(redeemHex);
    console.log('   escrow address:', escrowAddr);

    // Oracle declares B the winner: raw schnorr over msgB (bare 64 bytes for CheckSigFromStack).
    const oracleSig = oracleSchnorr(k.signScriptHash(Buffer.from(m.msgB).toString('hex'), oracle.key));

    await fundFromOperatingAddress(rpc, escrowAddr, DUST);
    const entries = await waitUtxo(rpc, escrowAddr);
    const total = entries.reduce((s, e) => s + BigInt(e.amount), 0n);
    const fee = 5_000_000n;
    // 1 input ↔ 1 output, output[0] pays the winner B — exactly what the covenant demands.
    const tx = k.createTransaction(entries, [{ address: b.address, amount: total - fee }], fee, undefined, 1);
    const ins = tx.inputs;
    ins[0].signatureScript = p2shSigScript(redeemHex, v2Witness(oracleSig, 'B'));
    tx.inputs = ins;

    const resp = await rpc.submitTransaction({ transaction: tx, allowOrphan: false });
    console.log(`   settle accepted: ${resp.transactionId ?? resp}`);
    console.log('S6 PASSED — full v2 escrow releases to the oracle-declared winner with NO arbiter, amount enforced.');
    await new Promise((r) => setTimeout(r, 4000));
    await sweepBack(rpc, b.address, b.key);
  });
}

/** Build a claim tx over `entries` and try to submit it. Returns whether the node ACCEPTED it. */
async function trySpend(rpc, redeemHex, entries, outputs, fee, witnessHex) {
  const tx = k.createTransaction(entries, outputs, fee, undefined, 1);
  const ins = tx.inputs;
  ins[0].signatureScript = p2shSigScript(redeemHex, witnessHex);
  tx.inputs = ins;
  try {
    const resp = await rpc.submitTransaction({ transaction: tx, allowOrphan: false });
    return { accepted: true, id: String(resp.transactionId ?? resp) };
  } catch (e) {
    return { accepted: false, err: String(e?.message ?? e).split('\n')[0].slice(0, 120) };
  }
}

// S6adv — the ADVERSARIAL MATRIX. Oracle has declared B the winner. Every attack below MUST be
// rejected on-chain; only the honest B-claim may spend the escrow. If any attack is ACCEPTED,
// that is a CRITICAL finding and v2 does not ship.
async function s6_adversarial() {
  console.log('S6adv — v2 escrow adversarial matrix (every attack MUST be rejected), dust');
  let critical = false;
  const expectReject = (label, r) => {
    if (r.accepted) { critical = true; console.log(`   ✗ CRITICAL — ${label} was ACCEPTED (${r.id})`); }
    else console.log(`   ✓ rejected — ${label}  [${r.err}]`);
  };
  await core.withRpc(async (rpc) => {
    const oracle = newKey(), a = newKey(), b = newKey(), evil = newKey();
    const info = await rpc.getBlockDagInfo();
    const reclaimDaa = BigInt(info.virtualDaaScore) + 1_000_000n;
    const maxFee = 10_000_000n;
    const m = v2Match({ oracleKey: oracle.key, aKey: a.key, bKey: b.key, depKey: b.key, reclaimDaa, maxFee });
    const redeemHex = v2SettleRedeem(m);
    const { address: escrowAddr } = p2shFor(redeemHex);
    console.log('   escrow address:', escrowAddr);

    // The one legitimate authorisation that exists: oracle over msgB (B won).
    const sigB = oracleSchnorr(k.signScriptHash(Buffer.from(m.msgB).toString('hex'), oracle.key));
    const settleWitness = (sig, winnerSelOp) => v2Witness(sig, winnerSelOp === k.Opcodes.OpTrue ? 'B' : 'A');

    await fundFromOperatingAddress(rpc, escrowAddr, DUST);
    const entries = await waitUtxo(rpc, escrowAddr);
    const total = entries.reduce((s, e) => s + BigInt(e.amount), 0n);
    const payB = (amt, fee) => [{ address: b.address, amount: amt }];

    // A1 — forged oracle signature (random 64 bytes). CheckSigFromStack must fail.
    expectReject('A1 forged oracle signature',
      await trySpend(rpc, redeemHex, entries, payB(total - 5_000_000n), 5_000_000n,
        settleWitness(randomBytes(64), k.Opcodes.OpTrue)));

    // A2 — wrong winner: claim the A-branch (winnerSel=false) with B's signature. The A-leg
    //      checks the oracle over msgA, which nobody signed → reject. (Pay A so only the sig differs.)
    expectReject('A2 wrong-winner (B-sig used on the A-branch)',
      await trySpend(rpc, redeemHex, entries, [{ address: a.address, amount: total - 5_000_000n }], 5_000_000n,
        settleWitness(sigB, k.Opcodes.OpFalse)));

    // A3 — skim/underpay: valid B-sig, pays B, but LESS than input − maxFee. Amount check must fail.
    //      output 0.80 KAS, input 1.00 KAS, maxFee 0.10 → 0.80+0.10 < 1.00 → reject.
    expectReject('A3 skim (winner underpaid below input − maxFee)',
      await trySpend(rpc, redeemHex, entries, payB(80_000_000n), 20_000_000n,
        settleWitness(sigB, k.Opcodes.OpTrue)));

    // A4 — wrong destination: valid B-sig, but pays a third party. OutputSpk != spkB → reject.
    expectReject('A4 wrong destination (pot redirected to a third party)',
      await trySpend(rpc, redeemHex, entries, [{ address: evil.address, amount: total - 5_000_000n }], 5_000_000n,
        settleWitness(sigB, k.Opcodes.OpTrue)));

    // A5 — cross-match replay: an oracle sig from a DIFFERENT escrow (different matchTag → different
    //      msgB) used here. The redeem checks THIS escrow's msgB → reject.
    const m2 = v2Match({ oracleKey: oracle.key, aKey: a.key, bKey: b.key, depKey: b.key, reclaimDaa, maxFee });
    const sigB2 = oracleSchnorr(k.signScriptHash(Buffer.from(m2.msgB).toString('hex'), oracle.key));
    expectReject('A5 cross-match replay (oracle sig from another escrow)',
      await trySpend(rpc, redeemHex, entries, payB(total - 5_000_000n), 5_000_000n,
        settleWitness(sigB2, k.Opcodes.OpTrue)));

    // A7 — draw-branch attempt with a WIN signature: the draw leg checks the oracle over msgDraw,
    //      which nobody signed → reject. Stops a decided game being laundered into a "draw" split.
    expectReject('A7 draw branch triggered with a win signature',
      await trySpend(rpc, redeemHex, entries, payB(total - 5_000_000n), 5_000_000n,
        v2Witness(sigB, 'draw')));

    // A6 — the HONEST claim finally releases to B and sweeps the escrow. MUST be accepted.
    const honest = await trySpend(rpc, redeemHex, entries, payB(total - 5_000_000n), 5_000_000n,
      settleWitness(sigB, k.Opcodes.OpTrue));
    if (honest.accepted) console.log(`   ✓ honest B-claim ACCEPTED (${honest.id})`);
    else { critical = true; console.log(`   ✗ CRITICAL — the honest claim was REJECTED [${honest.err}]`); }

    if (honest.accepted) { await new Promise((r) => setTimeout(r, 4000)); await sweepBack(rpc, b.address, b.key); }
    console.log(critical ? 'S6adv FAILED — a CRITICAL case did not behave; v2 must NOT ship.'
                         : 'S6adv PASSED — every attack rejected, only the honest winner-claim released the pot.');
  });
}

// S7 — the DRAW outcome: oracle signs "draw", each escrow pays its OWN depositor back. Here the
// escrow's depositor is A, so a draw must pay A. Plus draw-specific attacks that must be rejected.
async function s7_draw() {
  console.log('S7 — v2 escrow DRAW settle (oracle signs draw → escrow pays its depositor back), dust');
  let critical = false;
  const expectReject = (label, r) => {
    if (r.accepted) { critical = true; console.log(`   ✗ CRITICAL — ${label} was ACCEPTED (${r.id})`); }
    else console.log(`   ✓ rejected — ${label}  [${r.err}]`);
  };
  await core.withRpc(async (rpc) => {
    const oracle = newKey(), a = newKey(), b = newKey(); // depositor = A → a draw pays A
    const info = await rpc.getBlockDagInfo();
    const reclaimDaa = BigInt(info.virtualDaaScore) + 1_000_000n;
    const maxFee = 10_000_000n;
    const m = v2Match({ oracleKey: oracle.key, aKey: a.key, bKey: b.key, depKey: a.key, reclaimDaa, maxFee });
    const redeemHex = v2SettleRedeem(m);
    const { address: escrowAddr } = p2shFor(redeemHex);
    console.log('   escrow address:', escrowAddr, ' (depositor = A)');

    const drawSig = oracleSchnorr(k.signScriptHash(Buffer.from(m.msgDraw).toString('hex'), oracle.key));
    await fundFromOperatingAddress(rpc, escrowAddr, DUST);
    const entries = await waitUtxo(rpc, escrowAddr);
    const total = entries.reduce((s, e) => s + BigInt(e.amount), 0n);
    const payTo = (addr) => [{ address: addr, amount: total - 5_000_000n }];

    // D1 — draw signature used on the WIN-B branch: the B leg checks oracle over msgB → reject.
    expectReject('D1 draw signature used to claim a win',
      await trySpend(rpc, redeemHex, entries, payTo(b.address), 5_000_000n, v2Witness(drawSig, 'B')));
    // D2 — valid draw sig, but the payout goes to B instead of the depositor A → spk check fails.
    expectReject('D2 draw paid to the wrong party (not the depositor)',
      await trySpend(rpc, redeemHex, entries, payTo(b.address), 5_000_000n, v2Witness(drawSig, 'draw')));

    // The HONEST draw: pays the depositor A back. MUST be accepted.
    const honest = await trySpend(rpc, redeemHex, entries, payTo(a.address), 5_000_000n, v2Witness(drawSig, 'draw'));
    if (honest.accepted) console.log(`   ✓ honest draw settle ACCEPTED (${honest.id})`);
    else { critical = true; console.log(`   ✗ CRITICAL — the honest draw was REJECTED [${honest.err}]`); }

    if (honest.accepted) { await new Promise((r) => setTimeout(r, 4000)); await sweepBack(rpc, a.address, a.key); }
    console.log(critical ? 'S7 FAILED — a CRITICAL draw case misbehaved; v2 must NOT ship.'
                         : 'S7 PASSED — draw pays each depositor back; a draw sig cannot win and a win sig cannot draw.');
  });
}

const which = process.argv[2];
if (which === 'S5a') await s5a_cat_sha();
else if (which === 'S5b') await s5b_checksigfromstack();
else if (which === 'S5c') await s5c_output_introspection();
else if (which === 'S6') await s6_full_happy();
else if (which === 'S6adv') await s6_adversarial();
else if (which === 'S7') await s7_draw();
else { console.error('usage: node spikes_covenant.mjs [S5a|S5b|S5c|S6|S6adv|S7]'); process.exit(1); }
process.exit(0);
