/**
 * DAGmate — escrow + settlement builders (docs/DAGMATE_SPEC.md §2, §3).
 *
 * Non-custodial: player keys are never derived or held here — a player's
 * pubkey comes from their own wallet's connect response (Kasware/Kastle/
 * Kaspire), and a player's signature over a settle tx comes from that
 * wallet's own custom-script-signing call, not from this service. The only
 * key this module derives is the per-match ARBITER co-signing key, from
 * DAGmate's own HD seed (fresh, project-local — never shared key material).
 *
 * NEEDS `./core.js` (not yet built): a standalone Kaspa WASM/RPC module
 * exporting `wasm()`, `withRpc(fn)`, `netType()`, `network()`,
 * `COVENANT_OPTS`, `deriveArbiter(matchId)`, `operatingAddress()` (funds
 * move anchors — see `anchor()` below), `feeAddress()` (project rake/fee
 * destination). Same shape as any Kaspa WASM sidecar (init WASM, hold one
 * serialized RPC client, HD-derive from an env-configured xprv) — just with
 * zero Dagger code or shared key material.
 *
 * Script rules baked in here are proven on mainnet dust — see
 * docs/DAGMATE_SPEC.md §3 and service/spikes.mjs for the spikes that found
 * them:
 *   - sigOpCount is a createTransactions() OPTION, billed by CHECKMULTISIG
 *     pubkey-count (n=3 for a 2-of-3), not required-sig-count (m=2).
 *   - Multisig witness sigs must be pushed in the SAME relative order as
 *     their pubkeys in the redeem script (pkA, pkB, pkArb) — not signer/role
 *     order. Since arbiter is always pushed LAST, the non-arbiter co-signer's
 *     sig always goes first, regardless of whether it's player A or B.
 *   - Kaspa's OpCheckMultiSig has NO Bitcoin-style off-by-one dummy element.
 *   - Kaspa's OpCheckLockTimeVerify POPS the locktime value — no OP_DROP
 *     after it (that's Bitcoin-only convention and drops the wrong item here).
 */
import * as core from './core.js';

const H = (h) => Uint8Array.from(Buffer.from(String(h).replace(/^0x/, ''), 'hex'));
const rawSig = (s) => { const b = Buffer.from(String(s), 'hex'); return b.length === 66 ? b.subarray(1) : b; };

/** x-only (32-byte) schnorr pubkey hex for a private key, as ScriptBuilder wants it. */
function xOnlyHex(key) {
  const pub = key.toPublicKey ? key.toPublicKey() : key;
  const xo = pub.toXOnlyPublicKey ? pub.toXOnlyPublicKey() : pub;
  return String(xo.toString()).replace(/^0x/, '');
}

/** GET /escrow/arbiter-pubkey?matchId=<n> — x-only pubkey for the per-match
 *  arbiter co-signing key. There is no player-pubkey endpoint: a player's
 *  pubkey comes from their own wallet's connect response, not from us. */
export function arbiterPubkey({ matchId }) {
  if (matchId == null) throw new Error('matchId required');
  const { key } = core.deriveArbiter(matchId);
  return { pubkey: xOnlyHex(key) };
}

/** POST /escrow/build — build one player's escrow redeem script + P2SH
 *  address. Pure function: no chain calls, no signing. Two branches:
 *    IF   OP_2 <pkA> <pkB> <pkArbiter> OP_3 OP_CHECKMULTISIG      — settle
 *    ELSE <reclaimDaa> OP_CHECKLOCKTIMEVERIFY <pkDepositor> OP_CHECKSIG  — 14d reclaim
 *  `pkA`/`pkB` are the players' own wallet pubkeys (site collects these at
 *  connect time). `depositorIsA` selects which player's key backs the
 *  reclaim branch of THIS particular escrow address — each player gets
 *  their own (an abandoned match degrades to "everyone reclaims their own
 *  stake"). */
export function buildEscrow({ matchId, pkA, pkB, depositorIsA, reclaimDaa }) {
  if (matchId == null) throw new Error('matchId required');
  if (!pkA || !pkB) throw new Error('pkA and pkB required (x-only pubkey hex, from each wallet)');
  if (reclaimDaa == null) throw new Error('reclaimDaa required');
  const k = core.wasm();
  const { key: arbKey } = core.deriveArbiter(matchId);
  const pkArb = H(xOnlyHex(arbKey));
  const depositorPk = H(depositorIsA ? pkA : pkB);

  const sb = new k.ScriptBuilder(core.COVENANT_OPTS);
  sb.addOp(k.Opcodes.OpIf);
  sb.addOp(k.Opcodes.Op2);
  sb.addData(H(pkA)); sb.addData(H(pkB)); sb.addData(pkArb);
  sb.addOp(k.Opcodes.Op3);
  sb.addOp(k.Opcodes.OpCheckMultiSig);
  sb.addOp(k.Opcodes.OpElse);
  sb.addI64(BigInt(reclaimDaa));
  sb.addOp(k.Opcodes.OpCheckLockTimeVerify); // pops the locktime itself — no OP_DROP
  sb.addData(depositorPk);
  sb.addOp(k.Opcodes.OpCheckSig);
  sb.addOp(k.Opcodes.OpEndIf);
  const redeemHex = sb.drain();

  const spk = k.ScriptBuilder.fromScript(redeemHex, core.COVENANT_OPTS).createPayToScriptHashScript();
  const address = k.addressFromScriptPublicKey(spk, core.netType()).toString();
  return { address, redeemHex };
}

/** Fill one escrow input's sigScript for the IF (settle) branch. `sigPlayer`
 *  is always pushed before `sigArb` — pubkey push order is pkA, pkB, pkArb,
 *  so whichever of A/B co-signs, it precedes the arbiter's slot either way. */
function fillEscrowInput(k, tx, inputIdx, redeemHex, sigPlayer, sigArb) {
  const redeem = new k.ScriptBuilder(core.COVENANT_OPTS)
    .addData(rawSig(sigPlayer))
    .addData(rawSig(sigArb))
    .addOp(k.Opcodes.OpTrue) // select the IF (settle) branch
    .drain();
  tx.fillInput(inputIdx, k.ScriptBuilder.fromScript(redeemHex, core.COVENANT_OPTS).encodePayToScriptHashSignatureScript(redeem));
}

/** POST /escrow/balances — how much is actually sitting at each address.
 *  This is what the backend's deposit watcher runs on, so it is a
 *  money-critical read and deliberately conservative:
 *    - `confirmedSompi` only counts UTXOs at least `confirmDaa` DAA deep.
 *      A match must never go live (and so become settleable) off the back of
 *      a UTXO that could still be reorged out from under the escrow.
 *    - Totals are summed as BigInt and returned as decimal STRINGS. Sompi
 *      amounts exceed JS's safe-integer range at ~90M KAS, and more to the
 *      point JSON numbers would hand the Python side a float — never a thing
 *      to compare a stake against.
 *  One RPC round-trip covers every address, so the watcher can poll every
 *  open match at once rather than per-match. */
export async function addressBalances({ addresses, confirmDaa = 0 }) {
  if (!Array.isArray(addresses) || !addresses.length) throw new Error('addresses required');
  const minDepth = BigInt(confirmDaa ?? 0);
  return core.withRpc(async (rpc) => {
    const info = await rpc.getBlockDagInfo();
    const tipDaa = BigInt(info.virtualDaaScore);
    const { entries } = await rpc.getUtxosByAddresses({ addresses });

    const out = {};
    for (const a of addresses) out[a] = { sompi: 0n, confirmedSompi: 0n, utxos: 0 };
    for (const e of entries) {
      const addr = String(e.address);
      const slot = out[addr];
      if (!slot) continue; // node returned an address we didn't ask about
      const amount = BigInt(e.amount);
      // A UTXO not yet in the DAG has no blockDaaScore; treat it as depth 0
      // rather than assuming it's confirmed.
      const daa = e.blockDaaScore == null ? tipDaa : BigInt(e.blockDaaScore);
      const depth = tipDaa > daa ? tipDaa - daa : 0n;
      slot.sompi += amount;
      slot.utxos += 1;
      if (depth >= minDepth) slot.confirmedSompi += amount;
    }
    return {
      tipDaa: tipDaa.toString(),
      balances: Object.fromEntries(Object.entries(out).map(([a, v]) => [a, {
        sompi: v.sompi.toString(), confirmedSompi: v.confirmedSompi.toString(), utxos: v.utxos,
      }])),
    };
  });
}

/** POST /escrow/settle-unsigned — gather escrow UTXOs, build the settle tx,
 *  and return it (plus the arbiter's own signatures, computed now since we
 *  hold that key) for the site to pass to the winning player's wallet for a
 *  wallet-connect custom-script signature. Nothing is broadcast here.
 *  `escrows`: [{ address, redeemHex, depositorAddr }] (1 or 2 entries).
 *  `winnerAddr` — decisive result: that address's key co-signs EVERY input.
 *  `split: true` — draw: each escrow is co-signed by its OWN depositor and
 *  the pot is split evenly. */
export async function buildSettleUnsigned({ matchId, escrows, winnerAddr, split, rakeSompi = 0n }) {
  if (matchId == null) throw new Error('matchId required');
  if (!Array.isArray(escrows) || !escrows.length) throw new Error('escrows required');
  if (!winnerAddr && !split) throw new Error('winnerAddr or split required');
  if (split && escrows.length !== 2) throw new Error('split settle needs exactly 2 escrows');
  const k = core.wasm();
  const { key: arbKey } = core.deriveArbiter(matchId);
  const rake = BigInt(rakeSompi ?? 0);

  return core.withRpc(async (rpc) => {
    const { entries } = await rpc.getUtxosByAddresses({ addresses: escrows.map((e) => e.address) });
    if (!entries.length) throw new Error('no escrow UTXOs found — has the match been funded?');
    const potSompi = entries.reduce((s, e) => s + BigInt(e.amount), 0n);

    // Generous, fixed priority fee per covenant input — createTransactions()'s
    // auto-estimate doesn't fully price the extra sigOpCount budget a 2-of-3
    // CHECKMULTISIG spend costs.
    const priorityFee = 60_000_000n * BigInt(entries.length);

    let outputs;
    if (winnerAddr) {
      const payout = potSompi - rake - priorityFee;
      if (payout <= 0n) throw new Error('pot too small to cover rake + fee');
      outputs = rake > 0n
        ? [{ address: winnerAddr, amount: payout }, { address: core.feeAddress(), amount: rake }]
        : [{ address: winnerAddr, amount: payout }];
    } else {
      const half = (potSompi - rake - priorityFee) / 2n;
      if (half <= 0n) throw new Error('pot too small to cover rake + fee');
      const aAddr = escrows[0].depositorAddr, bAddr = escrows[1].depositorAddr;
      outputs = rake > 0n
        ? [{ address: aAddr, amount: half }, { address: bAddr, amount: half }, { address: core.feeAddress(), amount: rake }]
        : [{ address: aAddr, amount: half }, { address: bAddr, amount: half }];
    }

    const { transactions } = await k.createTransactions({
      entries, outputs, changeAddress: core.feeAddress(), priorityFee, networkId: core.network(),
      sigOpCount: 3, // CHECKMULTISIG billed by pubkey-count (n=3), not required-sig-count (m=2)
    });
    const tx = transactions[0];
    // Arbiter signatures, one per input — computed now, held server-side
    // until the site returns the matching player signature(s) to broadcast.
    const sigsArb = entries.map((_, i) => tx.createInputSignature(i, arbKey));

    return {
      matchId: Number(matchId), potSompi: potSompi.toString(), rakeSompi: rake.toString(),
      txJson: tx.serializeToSafeJSON(), sigsArb,
      // which escrow each input maps to (so the site knows whose wallet must sign which input)
      inputs: entries.map((e, i) => ({ index: i, address: e.address.toString() })),
    };
  });
}

/** POST /escrow/settle-broadcast — take a tx previously built by
 *  buildSettleUnsigned (site round-tripped it through the winning/depositor
 *  wallet(s) for a signature per input) and the matching player signatures,
 *  assemble the final sigScripts, and submit. */
export async function broadcastSettle({ txJson, escrows, sigsPlayer, sigsArb }) {
  if (!txJson) throw new Error('txJson required');
  if (!Array.isArray(sigsPlayer) || !Array.isArray(sigsArb)) throw new Error('sigsPlayer and sigsArb required, one per input');
  const k = core.wasm();
  return core.withRpc(async (rpc) => {
    const tx = k.Transaction.deserializeFromSafeJSON(txJson);
    for (let i = 0; i < sigsPlayer.length; i++) {
      // `escrows` here is indexed BY INPUT INDEX, not one entry per escrow —
      // an escrow holding two UTXOs appears twice. The backend expands it
      // (settlement._escrows_per_input); this side trusts the position.
      const escrow = escrows[i];
      if (!escrow) throw new Error(`no escrow mapping for input ${i}`);
      fillEscrowInput(k, tx, i, escrow.redeemHex, sigsPlayer[i], sigsArb[i]);
    }
    const txid = await tx.submit(rpc);
    return { txid };
  });
}

/** POST /escrow/anchor — dust tx carrying a DGMT move-anchor payload, paid
 *  from DAGmate's OWN operating address (never a player's wallet — anchoring
 *  every move can't require a wallet-connect popup per move without ruining
 *  the game's UX, and this never touches player funds, so it's a normal
 *  project operating cost, not custody). */
export async function anchor({ matchId, ply, payloadHex, feeSompi = 0n }) {
  if (!payloadHex) throw new Error('payloadHex required');
  const k = core.wasm();
  const { key, address } = core.operatingAddress();
  const fee = BigInt(feeSompi ?? 0);
  return core.withRpc(async (rpc) => {
    const { entries } = await rpc.getUtxosByAddresses({ addresses: [address] });
    if (!entries.length) throw new Error('no UTXO to anchor from — fund the operating address');
    const outputs = fee > 0n ? [{ address: core.feeAddress(), amount: fee }] : [];
    const { transactions } = await k.createTransactions({
      entries, outputs, changeAddress: address, priorityFee: 0n, networkId: core.network(),
      payload: String(payloadHex),
    });
    let txid = null;
    for (const tx of transactions) { tx.sign([key]); txid = await tx.submit(rpc); }
    return { txid, matchId: Number(matchId), ply: Number(ply) };
  });
}

/** GET /escrow/daa — current virtual DAA score, so the backend can compute
 *  an escrow's CLTV reclaim deadline (`reclaimDaa = current + ~14 days of
 *  DAA`) without embedding chain-tip knowledge outside this service. */
export async function daaScore() {
  return core.withRpc(async (rpc) => {
    const info = await rpc.getBlockDagInfo();
    return { daaScore: String(info.virtualDaaScore) };
  });
}
