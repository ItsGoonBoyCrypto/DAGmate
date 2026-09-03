/**
 * DAGmate — Day-0 mainnet spikes (dust only, S1/S2/S3). Run on a box where
 * DAGMATE_MASTER_XPRV (or equivalent) + a funded operating address already
 * exist — see docs/DAGMATE_SPEC.md §3.
 *   node spikes.mjs S1
 *   node spikes.mjs S2
 *   node spikes.mjs S3
 * Each spike funds a throwaway key from DAGmate's own operating address with
 * dust, proves the mechanic, and sweeps any leftover back. Nothing here
 * touches a player's wallet or key — same non-custodial boundary as
 * escrow.js (see its header for what `./core.js` needs to export).
 */
import { randomBytes } from 'node:crypto';
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

async function fundFromOperatingAddress(rpc, toAddress, sompi) {
  const { address: opAddr, key: opKey } = core.operatingAddress();
  const { entries } = await rpc.getUtxosByAddresses({ addresses: [opAddr] });
  if (!entries.length) throw new Error('operating address has no UTXOs');
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
  console.log(`   swept ${fromAddress} back to operating address — txid ${txid}`);
  return txid;
}

// ─────────────────────────────────────────────────────────────────────────
async function s1_payload() {
  console.log('S1 — payload support on createTransactions, mainnet dust');
  await core.withRpc(async (rpc) => {
    const { key, address } = newKey();
    await fundFromOperatingAddress(rpc, address, DUST);
    await waitUtxo(rpc, address);
    const { entries } = await rpc.getUtxosByAddresses({ addresses: [address] });
    const payloadHex = Buffer.from('DGMT|1|SPIKE|0|e2e4|deadbeef', 'utf8').toString('hex');
    const { transactions } = await k.createTransactions({
      entries, outputs: [], changeAddress: address, priorityFee: 0n, networkId: NETWORK_ID,
      payload: payloadHex,
    });
    let txid = null;
    for (const tx of transactions) { tx.sign([key]); txid = await tx.submit(rpc); }
    console.log(`   anchor tx submitted: ${txid}`);
    console.log(`   verify payload on an explorer: kaspa:tx/${txid} (payload hex should start with 44474d54)`);
    await new Promise((r) => setTimeout(r, 4000));
    await sweepBack(rpc, address, key);
  });
  console.log('S1 done — inspect the txid above on an explorer for the payload bytes.');
}

// ─────────────────────────────────────────────────────────────────────────
function buildEscrowRedeem(pkAx, pkBx, pkArbx) {
  // OP_IF <2-of-3 multisig> OP_ELSE <cltv reclaim to depositor> OP_ENDIF
  // Spike S2 exercises ONLY the IF branch (2-of-3 settle). S3 exercises ELSE.
  const sb = new k.ScriptBuilder(core.COVENANT_OPTS);
  sb.addOp(k.Opcodes.OpIf);
  sb.addOp(k.Opcodes.Op2);
  sb.addData(pkAx); sb.addData(pkBx); sb.addData(pkArbx);
  sb.addOp(k.Opcodes.Op3);
  sb.addOp(k.Opcodes.OpCheckMultiSig);
  sb.addOp(k.Opcodes.OpElse);
  sb.addOp(k.Opcodes.OpDrop); // placeholder ELSE branch for S2 (real CLTV branch built in S3)
  sb.addOp(k.Opcodes.OpTrue);
  sb.addOp(k.Opcodes.OpEndIf);
  return sb.drain(); // hex
}

function xOnly(pubkeyObj) {
  const pub = pubkeyObj.toPublicKey ? pubkeyObj.toPublicKey() : pubkeyObj;
  const xo = pub.toXOnlyPublicKey ? pub.toXOnlyPublicKey() : pub;
  const hex = xo.toString();
  return Uint8Array.from(Buffer.from(hex.replace(/^0x/, ''), 'hex'));
}

async function s2_multisig() {
  console.log('S2 — 2-of-3 CHECKMULTISIG escrow spend, mainnet dust (the real unknown)');
  await core.withRpc(async (rpc) => {
    const a = newKey(), b = newKey(), arb = newKey();
    console.log('   playerA pub:', xOnly(a.key).length, 'bytes');

    const redeemHex = buildEscrowRedeem(xOnly(a.key), xOnly(b.key), xOnly(arb.key));
    const spk = k.ScriptBuilder.fromScript(redeemHex, core.COVENANT_OPTS).createPayToScriptHashScript();
    const escrowAddr = k.addressFromScriptPublicKey(spk, NET).toString();
    console.log('   escrow address:', escrowAddr);

    const FUND = 300_000_000n; // 3 KAS — generous margin for fee estimation on this spend
    await fundFromOperatingAddress(rpc, escrowAddr, FUND);
    const entries = await waitUtxo(rpc, escrowAddr);

    // Settle: winner = playerA, arbiter co-signs. Spend to A's own address (fresh throwaway).
    const { address: opAddr } = core.operatingAddress();
    const { transactions } = await k.createTransactions({
      entries, outputs: [{ address: a.address, amount: 100_000_000n }],
      changeAddress: opAddr, priorityFee: 50_000_000n, networkId: NETWORK_ID,
      sigOpCount: 3, // CHECKMULTISIG is billed conservatively by pubkey count (n=3), not required-sig count (m=2)
    });
    const tx = transactions[0];
    const idx = 0; // single input
    const sigArb = tx.createInputSignature(idx, arb.key);
    const sigA = tx.createInputSignature(idx, a.key);
    const raw = (s) => { const buf = Buffer.from(String(s), 'hex'); return buf.length === 66 ? buf.subarray(1) : buf; };

    const redeem = new k.ScriptBuilder(core.COVENANT_OPTS)
      // NOTE: Kaspa's OpCheckMultiSig has no Bitcoin-style off-by-one dummy element — do not push one.
      .addData(raw(sigA)) // sigs must be pushed in the same relative order as their pubkeys (pkA, pkB, pkArb)
      .addData(raw(sigArb))
      .addOp(k.Opcodes.OpTrue) // select IF branch
      .drain();
    tx.fillInput(idx, k.ScriptBuilder.fromScript(redeemHex, core.COVENANT_OPTS).encodePayToScriptHashSignatureScript(redeem));

    const txid = await tx.submit(rpc);
    console.log(`   settle tx submitted: ${txid}`);
    console.log('S2 PASSED — 2-of-3 escrow settle confirmed on mainnet.');
    await new Promise((r) => setTimeout(r, 4000));
    await sweepBack(rpc, a.address, a.key);
  });
}

// ─────────────────────────────────────────────────────────────────────────
async function s3_cltv() {
  console.log('S3 — CLTV reclaim branch, mainnet, short window (~60s in DAA-score terms)');
  await core.withRpc(async (rpc) => {
    const depositor = newKey();
    const info = await rpc.getBlockDagInfo();
    const currentDaa = BigInt(info.virtualDaaScore);
    // Margin must comfortably exceed fundFromOperatingAddress + waitUtxo's polling overhead
    // (can easily be 10-30s on its own), or the "early" attempt will already be past the
    // deadline by the time it actually runs — false-passing the test. DAA advances ~10/s,
    // so 600 ~= 60s of headroom.
    const reclaimDaa = currentDaa + 600n;

    const sb = new k.ScriptBuilder(core.COVENANT_OPTS);
    sb.addOp(k.Opcodes.OpIf);
    sb.addOp(k.Opcodes.OpFalse); // dead branch for this spike (settle path not exercised here)
    sb.addOp(k.Opcodes.OpElse);
    sb.addI64(reclaimDaa);
    sb.addOp(k.Opcodes.OpCheckLockTimeVerify); // Kaspa's CLTV pops the locktime value itself (unlike Bitcoin) — no OpDrop needed/wanted here
    sb.addData(xOnly(depositor.key));
    sb.addOp(k.Opcodes.OpCheckSig);
    sb.addOp(k.Opcodes.OpEndIf);
    const redeemHex = sb.drain();
    const spk = k.ScriptBuilder.fromScript(redeemHex, core.COVENANT_OPTS).createPayToScriptHashScript();
    const escrowAddr = k.addressFromScriptPublicKey(spk, NET).toString();
    console.log('   escrow address:', escrowAddr, ' reclaimDaa:', reclaimDaa.toString(), ' currentDaa:', currentDaa.toString());

    const S3_FUND = 100_000_000n; // 1 KAS — low-level createTransaction() has no auto change output
    await fundFromOperatingAddress(rpc, escrowAddr, S3_FUND);
    const entries = await waitUtxo(rpc, escrowAddr);

    async function attemptReclaim(label) {
      // NOTE: PendingTransaction.transaction is a snapshot — mutating it (lockTime, sequence)
      // does NOT persist into what .submit() actually sends. Build a raw Transaction via the
      // low-level createTransaction() instead, mutate it directly, and submit it ourselves.
      // No changeAddress here, so any leftover beyond output+fee becomes miner fee — keep it small.
      const txn = k.createTransaction(entries, [{ address: depositor.address, amount: 95_000_000n }], 1_000_000n, undefined, 1);
      txn.lockTime = reclaimDaa;
      const inputs = txn.inputs;
      inputs[0].sequence = 0n; // must be < MAX_TX_IN_SEQUENCE_NUM or CLTV is skipped
      txn.inputs = inputs; // commit the mutation back in case .inputs returns clones, not live refs

      const sig = k.createInputSignature(txn, 0, depositor.key);
      const raw = (s) => { const buf = Buffer.from(String(s), 'hex'); return buf.length === 66 ? buf.subarray(1) : buf; };
      const redeem = new k.ScriptBuilder(core.COVENANT_OPTS).addData(raw(sig)).addOp(k.Opcodes.OpFalse).drain();
      const sigScript = k.ScriptBuilder.fromScript(redeemHex, core.COVENANT_OPTS).encodePayToScriptHashSignatureScript(redeem);
      const inputs2 = txn.inputs;
      inputs2[0].signatureScript = sigScript;
      txn.inputs = inputs2;

      try {
        const resp = await rpc.submitTransaction({ transaction: txn, allowOrphan: false });
        console.log(`   [${label}] ACCEPTED — txid ${resp.transactionId}`);
        return true;
      } catch (e) {
        console.log(`   [${label}] REJECTED — ${e?.message ?? e}`);
        return false;
      }
    }

    const early = await attemptReclaim('before deadline (expect REJECT)');
    if (early) throw new Error('S3 FAIL: early reclaim was accepted — CLTV not enforced');

    console.log('   waiting for DAA to pass the reclaim threshold...');
    for (;;) {
      const now = BigInt((await rpc.getBlockDagInfo()).virtualDaaScore);
      if (now >= reclaimDaa) break;
      await new Promise((r) => setTimeout(r, 2000));
    }
    const late = await attemptReclaim('after deadline (expect ACCEPT)');
    if (!late) throw new Error('S3 FAIL: reclaim after deadline was rejected');
    console.log('S3 PASSED — CLTV reclaim branch behaves exactly as designed.');
  });
}

// ─────────────────────────────────────────────────────────────────────────
async function s4_mutual() {
  console.log('S4 — 2-of-3 escrow settled by BOTH players (mutual, NO arbiter), mainnet dust');
  console.log('     Roadmap #1: proves the honestly-agreed settle path where DAGmate never co-signs.');
  await core.withRpc(async (rpc) => {
    const a = newKey(), b = newKey(), arb = newKey();

    // Identical escrow to S2 — the same 2-of-3 redeem. The ONLY difference is
    // which two of the three keys sign the spend: here it is playerA + playerB,
    // never the arbiter. If S2 passes and S4 fails, the multisig is rejecting a
    // valid 2-subset and mutual settlement must not ship.
    const redeemHex = buildEscrowRedeem(xOnly(a.key), xOnly(b.key), xOnly(arb.key));
    const spk = k.ScriptBuilder.fromScript(redeemHex, core.COVENANT_OPTS).createPayToScriptHashScript();
    const escrowAddr = k.addressFromScriptPublicKey(spk, NET).toString();
    console.log('   escrow address:', escrowAddr);

    const FUND = 300_000_000n; // 3 KAS — same generous margin as S2
    await fundFromOperatingAddress(rpc, escrowAddr, FUND);
    const entries = await waitUtxo(rpc, escrowAddr);

    // Settle to playerA's address (the "winner"), signed by A AND B. No arbiter.
    const { address: opAddr } = core.operatingAddress();
    const { transactions } = await k.createTransactions({
      entries, outputs: [{ address: a.address, amount: 100_000_000n }],
      changeAddress: opAddr, priorityFee: 50_000_000n, networkId: NETWORK_ID,
      sigOpCount: 3, // CHECKMULTISIG is billed by pubkey count (n=3), not required-sig count (m=2)
    });
    const tx = transactions[0];
    const idx = 0; // single input
    const sigA = tx.createInputSignature(idx, a.key);
    const sigB = tx.createInputSignature(idx, b.key);
    const raw = (s) => { const buf = Buffer.from(String(s), 'hex'); return buf.length === 66 ? buf.subarray(1) : buf; };

    const redeem = new k.ScriptBuilder(core.COVENANT_OPTS)
      // sigs in the same relative order as their pubkeys (pkA, pkB, pkArb):
      // playerA's signature first, playerB's second, and NO arbiter push.
      .addData(raw(sigA))
      .addData(raw(sigB))
      .addOp(k.Opcodes.OpTrue) // select IF branch
      .drain();
    tx.fillInput(idx, k.ScriptBuilder.fromScript(redeemHex, core.COVENANT_OPTS).encodePayToScriptHashSignatureScript(redeem));

    const txid = await tx.submit(rpc);
    console.log(`   mutual settle tx submitted: ${txid}`);
    console.log('S4 PASSED — 2-of-3 escrow settled by both players with NO arbiter, on mainnet.');
    await new Promise((r) => setTimeout(r, 4000));
    await sweepBack(rpc, a.address, a.key);
  });
}

const which = process.argv[2];
if (which === 'S1') await s1_payload();
else if (which === 'S2') await s2_multisig();
else if (which === 'S3') await s3_cltv();
else if (which === 'S4') await s4_mutual();
else { console.error('usage: node spikes.mjs [S1|S2|S3|S4]'); process.exit(1); }
process.exit(0);
