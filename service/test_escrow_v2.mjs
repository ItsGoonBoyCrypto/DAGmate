/**
 * DAGmate — end-to-end proof of the PRODUCTION v2 module (service/escrow_v2.js) on mainnet dust.
 * Runs BOTH outcomes through the real module: a decisive win (winner takes the pot) and a DRAW
 * (each depositor gets their own stake back). The draw run also proves the 2-input↔2-output
 * same-index binding the covenant relies on. Builds real escrows via buildEscrowV2, funds them,
 * produces the oracle verdict via oracleSignResult, settles through settleV2, checks the payouts,
 * then sweeps.
 *
 *   node test_escrow_v2.mjs
 *
 * ⚠️ Do NOT wrap settleV2 in an outer core.withRpc — it opens its own; the session mutex
 * deadlocks on nesting. Same throwaway-dust discipline as spikes_covenant.mjs.
 */
import { randomBytes } from 'node:crypto';
import * as core from './core.js';
import * as v2 from './escrow_v2.js';

const k = core.wasm();
const NET = core.netType();
const NETWORK_ID = core.network();
const DUST = 100_000_000n; // 1 KAS per escrow

function newKey() {
  const key = new k.PrivateKey(randomBytes(32).toString('hex'));
  return {
    key,
    address: key.toPublicKey().toAddress(NET).toString(),
    pk: String(key.toPublicKey().toXOnlyPublicKey().toString()).replace(/^0x/, ''),
  };
}

async function balanceOf(rpc, address) {
  const { entries } = await rpc.getUtxosByAddresses({ addresses: [address] });
  return { sompi: entries.reduce((s, e) => s + BigInt(e.amount), 0n), entries };
}

async function sweep(rpc, address, key) {
  const { entries } = await rpc.getUtxosByAddresses({ addresses: [address] });
  if (!entries.length) return;
  const { address: opAddr } = core.operatingAddress();
  const { transactions } = await k.createTransactions({ entries, outputs: [], changeAddress: opAddr, priorityFee: 0n, networkId: NETWORK_ID });
  for (const tx of transactions) { tx.sign([key]); await tx.submit(rpc); }
}

let failures = 0;
function check(name, got, want) {
  if (got === want) { console.log(`   ok   ${name}`); }
  else { failures++; console.log(`   FAIL ${name}: got ${got}, want ${want}`); }
}

async function scenario(matchId, outcome) {
  console.log(`\n=== scenario: matchId=${matchId} outcome=${outcome} ===`);
  const a = newKey(), b = newKey();
  let escrowA, escrowB;

  // build + fund both escrows in ONE tx (separate funding txs re-spend the operating UTXO).
  await core.withRpc(async (rpc) => {
    const info = await rpc.getBlockDagInfo();
    const reclaimDaa = BigInt(info.virtualDaaScore) + 1_000_000n;
    escrowA = v2.buildEscrowV2({ matchId, pkA: a.pk, pkB: b.pk, side: 'A', reclaimDaa });
    escrowB = v2.buildEscrowV2({ matchId, pkA: a.pk, pkB: b.pk, side: 'B', reclaimDaa });
    console.log('   escrow A:', escrowA.address, '\n   escrow B:', escrowB.address);
    const { address: opAddr, key: opKey } = core.operatingAddress();
    const { entries } = await rpc.getUtxosByAddresses({ addresses: [opAddr] });
    const { transactions } = await k.createTransactions({
      entries, outputs: [{ address: escrowA.address, amount: DUST }, { address: escrowB.address, amount: DUST }],
      changeAddress: opAddr, priorityFee: 20_000_000n, networkId: NETWORK_ID,
    });
    for (const tx of transactions) { tx.sign([opKey]); await tx.submit(rpc); }
    for (let i = 0; i < 20; i++) {
      const ea = await rpc.getUtxosByAddresses({ addresses: [escrowA.address] });
      const eb = await rpc.getUtxosByAddresses({ addresses: [escrowB.address] });
      if (ea.entries.length && eb.entries.length) return;
      await new Promise((r) => setTimeout(r, 3000));
    }
    throw new Error('escrows did not confirm');
  });

  // oracle verdict + settle (no player signature).
  const verdict = v2.oracleSignResult({ matchId, outcome });
  const res = await v2.settleV2({
    matchId, escrows: [{ ...escrowA, side: 'A' }, { ...escrowB, side: 'B' }],
    outcome, pkA: a.pk, pkB: b.pk, sigA: verdict.sigA, sigB: verdict.sigB,
  });
  console.log('   settled txid:', res.txid);

  // verify payouts, then sweep.
  await core.withRpc(async (rpc) => {
    let A, B;
    for (let i = 0; i < 15; i++) {
      A = await balanceOf(rpc, a.address); B = await balanceOf(rpc, b.address);
      if (A.sompi + B.sompi > 0n) break;
      await new Promise((r) => setTimeout(r, 3000));
    }
    const perInput = DUST - 5_000_000n; // DUST − SETTLE_V2_FEE_SOMPI_PER_INPUT (5M)
    if (outcome === 'B') {
      check('winner B holds the whole pot', B.sompi.toString(), (2n * DUST - BigInt(res.feeSompi)).toString());
      check('loser A holds nothing', A.sompi.toString(), '0');
    } else if (outcome === 'A') {
      check('winner A holds the whole pot', A.sompi.toString(), (2n * DUST - BigInt(res.feeSompi)).toString());
      check('loser B holds nothing', B.sompi.toString(), '0');
    } else { // draw — each depositor back, minus one per-input fee
      check('A got escrow A back', A.sompi.toString(), perInput.toString());
      check('B got escrow B back', B.sompi.toString(), perInput.toString());
    }
    await sweep(rpc, a.address, a.key); await sweep(rpc, b.address, b.key);
  });
}

async function main() {
  await scenario(424201, 'B');    // decisive: player B wins
  await scenario(424203, 'A');    // decisive: player A wins (the other leg)
  await scenario(424202, 'draw'); // draw: each depositor back (2-in ↔ 2-out binding)
  console.log(failures === 0 ? '\nESCROW_V2 E2E PASSED — win-A, win-B and draw all settle correctly via the production module.'
                             : `\n${failures} CHECK(S) FAILED`);
  return failures === 0 ? 0 : 1;
}

main().then((c) => process.exit(c)).catch((e) => { console.error('FAILED:', e.message); process.exit(1); });
