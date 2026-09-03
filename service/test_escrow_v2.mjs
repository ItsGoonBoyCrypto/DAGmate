/**
 * DAGmate — end-to-end proof of the PRODUCTION v2 module (service/escrow_v2.js) on mainnet dust.
 * Builds two real v2 escrows through buildEscrowV2, funds them, produces the oracle verdict via
 * oracleSignResult, settles through settleV2, and checks the winner was actually paid. Then sweeps.
 *
 *   node test_escrow_v2.mjs
 *
 * Same throwaway-dust discipline as spikes_covenant.mjs. ⚠️ Do NOT wrap settleV2 in an outer
 * core.withRpc — it opens its own, and the session mutex deadlocks on nesting.
 */
import { randomBytes } from 'node:crypto';
import * as core from './core.js';
import * as v2 from './escrow_v2.js';

const k = core.wasm();
const NET = core.netType();
const NETWORK_ID = core.network();
const DUST = 100_000_000n; // 1 KAS per escrow
const MATCH_ID = 424242;   // any integer < 2^31 (server rowid in production)

function newKey() {
  const key = new k.PrivateKey(randomBytes(32).toString('hex'));
  return {
    key,
    address: key.toPublicKey().toAddress(NET).toString(),
    pk: String(key.toPublicKey().toXOnlyPublicKey().toString()).replace(/^0x/, ''),
  };
}

async function main() {
  const a = newKey(); // player A (the loser here)
  const b = newKey(); // player B (the winner)
  let escrowA, escrowB;

  // 1) build both escrows through the production builder, fund them in ONE tx (separate funding
  //    txs re-spend the operating UTXO before change settles → mempool double-spend).
  await core.withRpc(async (rpc) => {
    const info = await rpc.getBlockDagInfo();
    const reclaimDaa = BigInt(info.virtualDaaScore) + 1_000_000n; // far future — reclaim not exercised
    escrowA = v2.buildEscrowV2({ matchId: MATCH_ID, pkA: a.pk, pkB: b.pk, side: 'A', reclaimDaa });
    escrowB = v2.buildEscrowV2({ matchId: MATCH_ID, pkA: a.pk, pkB: b.pk, side: 'B', reclaimDaa });
    console.log('   escrow A:', escrowA.address);
    console.log('   escrow B:', escrowB.address);

    const { address: opAddr, key: opKey } = core.operatingAddress();
    const { entries } = await rpc.getUtxosByAddresses({ addresses: [opAddr] });
    if (!entries.length) throw new Error('operating address has no UTXOs to fund from');
    const { transactions } = await k.createTransactions({
      entries,
      outputs: [{ address: escrowA.address, amount: DUST }, { address: escrowB.address, amount: DUST }],
      changeAddress: opAddr, priorityFee: 20_000_000n, networkId: NETWORK_ID,
    });
    for (const tx of transactions) { tx.sign([opKey]); await tx.submit(rpc); }
    console.log('   funded both escrows; waiting for confirmation...');
    for (let i = 0; i < 20; i++) {
      const ea = await rpc.getUtxosByAddresses({ addresses: [escrowA.address] });
      const eb = await rpc.getUtxosByAddresses({ addresses: [escrowB.address] });
      if (ea.entries.length && eb.entries.length) return;
      await new Promise((r) => setTimeout(r, 3000));
    }
    throw new Error('escrows did not confirm in time');
  });

  // 2) the oracle declares B the winner (this is ALL DAGmate does to settle).
  const verdict = v2.oracleSignResult({ matchId: MATCH_ID, winner: 'B' });
  console.log('   oracle verdict:', verdict.winner, '(sigA/sigB produced)');

  // 3) settle — no player signature, no co-sign round-trip. Covenant pays the winner.
  const res = await v2.settleV2({
    matchId: MATCH_ID,
    escrows: [{ ...escrowA, side: 'A' }, { ...escrowB, side: 'B' }],
    winnerPk: b.pk, winner: 'B', sigA: verdict.sigA, sigB: verdict.sigB,
  });
  console.log('   SETTLED txid:', res.txid, '| pot', res.potSompi, '| fee', res.feeSompi);

  // 4) verify the winner actually holds the pot on-chain, then sweep back.
  await core.withRpc(async (rpc) => {
    let got = 0n;
    for (let i = 0; i < 15; i++) {
      const { entries } = await rpc.getUtxosByAddresses({ addresses: [b.address] });
      got = entries.reduce((s, e) => s + BigInt(e.amount), 0n);
      if (got > 0n) {
        console.log('   winner B holds:', got.toString(), 'sompi across', entries.length, 'UTXO(s)');
        const { address: opAddr } = core.operatingAddress();
        const { transactions } = await k.createTransactions({
          entries, outputs: [], changeAddress: opAddr, priorityFee: 0n, networkId: NETWORK_ID,
        });
        for (const tx of transactions) { tx.sign([b.key]); await tx.submit(rpc); }
        console.log('   swept winnings back to operating.');
        break;
      }
      await new Promise((r) => setTimeout(r, 3000));
    }
    const expected = 2n * DUST - BigInt(res.feeSompi);
    if (got === expected) console.log(`ESCROW_V2 E2E PASSED — winner paid the full pot (${got} = 2×dust − fee), no arbiter, no player signature.`);
    else console.log(`⚠️ mismatch — winner holds ${got}, expected ${expected}`);
  });
}

main().then(() => process.exit(0)).catch((e) => { console.error('FAILED:', e.message); process.exit(1); });
