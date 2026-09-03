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

/** Flat fee for a settle input, per input. Covers the mass a signed 2-of-3
 *  CHECKMULTISIG input carries (redeem script + two sigs) — larger than the
 *  single-sig reclaim input below, smaller than the old 0.6-KAS placeholder.
 *  ⚠️ Mirrored in site/backend/config.py as SETTLE_FEE_SOMPI_PER_INPUT — change
 *  both together, and re-prove on a funded mainnet escrow before launch. */
const SETTLE_FEE_SOMPI_PER_INPUT = 3_000_000n; // 0.03 KAS

const H = (h) => Uint8Array.from(Buffer.from(String(h).replace(/^0x/, ''), 'hex'));
const rawSig = (s) => { const b = Buffer.from(String(s), 'hex'); return b.length === 66 ? b.subarray(1) : b; };

/** x-only (32-byte) schnorr pubkey hex for a private key, as ScriptBuilder wants it. */
function xOnlyHex(key) {
  const pub = key.toPublicKey ? key.toPublicKey() : key;
  const xo = pub.toXOnlyPublicKey ? pub.toXOnlyPublicKey() : pub;
  return String(xo.toString()).replace(/^0x/, '');
}

/** Normalise ANY pubkey hex a wallet hands us to the 32-byte x-only schnorr
 *  form the Kaspa script engine requires. Kasware's getPublicKey() returns a
 *  33-byte COMPRESSED key (66 hex, 02/03 prefix); baking that straight into a
 *  CHECKMULTISIG/CHECKSIG redeem builds a fundable P2SH address (the address is
 *  only a hash, so a deposit succeeds) whose spend the node rejects with
 *  "pubkey invalid: malformed public key". A 32-byte key passes through
 *  untouched, so this is safe on already-x-only wallets. */
function toXOnly(k, pkHex) {
  const clean = String(pkHex).replace(/^0x/, '').toLowerCase();
  if (clean.length === 64) return clean;               // already x-only
  const pub = new k.PublicKey(clean);                  // parses 33-byte compressed (and 64-hex)
  const xo = pub.toXOnlyPublicKey ? pub.toXOnlyPublicKey() : pub;
  return String(xo.toString()).replace(/^0x/, '').toLowerCase();
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
  // Every pubkey in the redeem MUST be 32-byte x-only or the spend is rejected
  // as malformed — normalise the wallet-supplied player keys, not just ours.
  const pkAx = H(toXOnly(k, pkA));
  const pkBx = H(toXOnly(k, pkB));
  const pkArb = H(xOnlyHex(arbKey));
  const depositorPk = depositorIsA ? pkAx : pkBx;

  const sb = new k.ScriptBuilder(core.COVENANT_OPTS);
  sb.addOp(k.Opcodes.OpIf);
  sb.addOp(k.Opcodes.Op2);
  sb.addData(pkAx); sb.addData(pkBx); sb.addData(pkArb);
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
function settleSigScript(k, redeemHex, sigPlayer, sigArb) {
  const redeem = new k.ScriptBuilder(core.COVENANT_OPTS)
    .addData(rawSig(sigPlayer))
    .addData(rawSig(sigArb))
    .addOp(k.Opcodes.OpTrue) // select the IF (settle) branch
    .drain();
  return k.ScriptBuilder.fromScript(redeemHex, core.COVENANT_OPTS).encodePayToScriptHashSignatureScript(redeem);
}

/** Fill one escrow input's sigScript for the IF (settle) branch using BOTH
 *  players' signatures and NO arbiter — the mutual, player-agreed settlement
 *  (roadmap #1 / docs/DAGMATE_SPEC.md §2.3). For every honestly-finished game
 *  where the loser co-signs the payout, DAGmate's arbiter key is never used.
 *
 *  The 2-of-3 redeem is `OP_2 <pkA> <pkB> <pkArb> OP_3 CHECKMULTISIG`, and
 *  Kaspa's OpCheckMultiSig requires the m=2 signatures to be pushed in the SAME
 *  relative order as their pubkeys — so player A's signature is pushed before
 *  player B's, ALWAYS, regardless of who won. The backend is the only side that
 *  knows which wallet is A and which is B, so it maps winner/loser onto the A/B
 *  roles and hands us role-ordered sigs; this stays a dumb, order-preserving
 *  assembler. Getting the order wrong yields an unspendable input, so it is
 *  fixed here by construction and never left to a caller's argument order.
 *  Proven on mainnet dust by spike S4 (service/spikes.mjs). */
function settleSigScriptMutual(k, redeemHex, sigA, sigB) {
  if (!sigA || !sigB) {
    throw new Error('mutual settle needs both player A and player B signatures for every input');
  }
  const redeem = new k.ScriptBuilder(core.COVENANT_OPTS)
    .addData(rawSig(sigA)) // pkA slot — first, to match pubkey order
    .addData(rawSig(sigB)) // pkB slot
    .addOp(k.Opcodes.OpTrue) // select the IF (settle) branch
    .drain();
  return k.ScriptBuilder.fromScript(redeemHex, core.COVENANT_OPTS).encodePayToScriptHashSignatureScript(redeem);
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

    // Explicit, flat fee per covenant input, spent EXACTLY — same model as the
    // reclaim path (low-level createTransaction, no change address). The old
    // code used createTransactions with a bare-bigint priorityFee, which the
    // SDK treats as SenderPays (charged ON TOP of the outputs): with
    // `outputs = pot - priorityFee` the build needed `pot >= pot + massFee` and
    // failed "insufficient funds" on every settlement — decisive and draw
    // alike. It also routed change to feeAddress(), silently raking ~0.6 KAS a
    // game against the no-fee disclaimer. Here the outputs sum to exactly
    // `pot - fee`, so nothing is left to become change and nothing leaks.
    //
    // Sized to cover the extra mass a signed 2-of-3 CHECKMULTISIG input carries
    // once its redeem script + both signatures are filled at broadcast — mass
    // the auto-estimate can't see while the inputs are still unsigned.
    // ⚠️ Re-prove on a FUNDED mainnet escrow before launch, and keep in step
    // with SETTLE_FEE_SOMPI_PER_INPUT in site/backend/config.py.
    const fee = SETTLE_FEE_SOMPI_PER_INPUT * BigInt(entries.length);

    let outputs;
    if (winnerAddr) {
      const payout = potSompi - rake - fee;
      if (payout <= 0n) throw new Error('pot too small to cover rake + fee');
      outputs = rake > 0n
        ? [{ address: winnerAddr, amount: payout }, { address: core.feeAddress(), amount: rake }]
        : [{ address: winnerAddr, amount: payout }];
    } else {
      const distributable = potSompi - rake - fee;
      if (distributable <= 1n) throw new Error('pot too small to cover rake + fee');
      // Even split; any odd sompi goes to A so the outputs sum EXACTLY to
      // `distributable` — createTransaction has no change output, so an unspent
      // remainder would be silently donated to a miner.
      const halfB = distributable / 2n;
      const halfA = distributable - halfB;
      const aAddr = escrows[0].depositorAddr, bAddr = escrows[1].depositorAddr;
      outputs = rake > 0n
        ? [{ address: aAddr, amount: halfA }, { address: bAddr, amount: halfB }, { address: core.feeAddress(), amount: rake }]
        : [{ address: aAddr, amount: halfA }, { address: bAddr, amount: halfB }];
    }

    // Low-level createTransaction: exact fee, no change, full control of every
    // output. sigOpCount 3 because CHECKMULTISIG is billed by pubkey-count
    // (n=3), not required-sig-count (m=2).
    const tx = k.createTransaction(entries, outputs, fee, undefined, 3);
    // Arbiter signatures, one per input — computed now (we hold that key), held
    // server-side until the site returns the matching player signature(s) to
    // broadcast. Standalone createInputSignature: the low-level Transaction has
    // no instance method for it (unlike the PendingTransaction the old
    // high-level builder returned).
    const sigsArb = entries.map((_, i) => k.createInputSignature(tx, i, arbKey));

    return {
      matchId: Number(matchId), potSompi: potSompi.toString(), rakeSompi: rake.toString(),
      txJson: tx.serializeToSafeJSON(), sigsArb,
      // which escrow each input maps to (so the site knows whose wallet must sign which input)
      inputs: entries.map((e, i) => ({ index: i, address: e.address.toString() })),
    };
  });
}

/** Pull the first pushed data item (the signature) out of a signatureScript hex
 *  string. A wallet's signPskt fills each input's script with a push of the
 *  Schnorr sig (65 bytes incl. the sighash byte); we only want that raw sig so
 *  we can re-assemble it into the covenant's own 2-of-3 sigScript. */
function firstPushHex(scriptHex) {
  const b = Buffer.from(String(scriptHex || ''), 'hex');
  if (!b.length) return null;
  const op = b[0];
  if (op >= 0x01 && op <= 0x4b) return b.subarray(1, 1 + op).toString('hex');       // OP_DATA_1..75
  if (op === 0x4c && b.length >= 2) return b.subarray(2, 2 + b[1]).toString('hex');  // OP_PUSHDATA1
  return null;
}

/** POST /escrow/extract-sigs — the wallet-connect path. A player's wallet
 *  (Kasware signPskt) returns the WHOLE tx with THEIR signature embedded in
 *  each of their inputs. This pulls the raw signature back out of the given
 *  input indexes, so the backend can store it and, once every input is signed,
 *  hand the raw sigs to the existing broadcastSettle assembly — the same
 *  covenant sigScript path the arbiter sigs already ran through. No key here,
 *  no broadcast: pure extraction, so a draw's two separate wallet returns each
 *  contribute only the inputs that were theirs to sign. */
export async function extractSigs({ signedTxJson, indexes }) {
  if (!signedTxJson) throw new Error('signedTxJson required');
  if (!Array.isArray(indexes)) throw new Error('indexes required');
  const k = core.wasm();
  const signed = k.Transaction.deserializeFromSafeJSON(signedTxJson);
  const ins = signed.inputs;
  const sigs = {};
  for (const i of indexes) {
    const sig = firstPushHex(ins[i] && ins[i].signatureScript);
    if (!sig) throw new Error(`no signature in input ${i} — wallet sigScript was `
      + `"${ins[i] && ins[i].signatureScript}"`);
    sigs[String(i)] = sig;
  }
  return { sigs };
}

/** POST /escrow/settle-broadcast — take a tx previously built by
 *  buildSettleUnsigned (site round-tripped it through the winning/depositor
 *  wallet(s) for a signature per input) and the matching player signatures,
 *  assemble the final sigScripts, and submit. */
export async function broadcastSettle({ txJson, escrows, sigsPlayer, sigsArb, sigsA, sigsB }) {
  if (!txJson) throw new Error('txJson required');
  // Two settlement modes over the SAME 2-of-3 escrow:
  //   - mutual  (roadmap #1): both players co-sign, no arbiter — sigsA + sigsB.
  //   - arbiter (v1 / draws / stall fallback): one player + arbiter — sigsPlayer + sigsArb.
  // The backend picks the mode and sends only that mode's arrays; the arbiter
  // path is byte-for-byte the code it always was.
  const mutual = Array.isArray(sigsA) && Array.isArray(sigsB);
  if (!mutual && (!Array.isArray(sigsPlayer) || !Array.isArray(sigsArb))) {
    throw new Error('either sigsA+sigsB (mutual) or sigsPlayer+sigsArb (arbiter) required, one per input');
  }
  const n = mutual ? sigsA.length : sigsPlayer.length;
  const k = core.wasm();
  return core.withRpc(async (rpc) => {
    const tx = k.Transaction.deserializeFromSafeJSON(txJson);
    const inputs = tx.inputs;  // may be clones — mutate then reassign to commit
    for (let i = 0; i < n; i++) {
      // `escrows` here is indexed BY INPUT INDEX, not one entry per escrow —
      // an escrow holding two UTXOs appears twice. The backend expands it
      // (settlement._escrows_per_input); this side trusts the position.
      const escrow = escrows[i];
      if (!escrow) throw new Error(`no escrow mapping for input ${i}`);
      inputs[i].signatureScript = mutual
        ? settleSigScriptMutual(k, escrow.redeemHex, sigsA[i], sigsB[i])
        : settleSigScript(k, escrow.redeemHex, sigsPlayer[i], sigsArb[i]);
    }
    tx.inputs = inputs; // low-level Transaction has no fillInput(); commit the array back
    // A low-level Transaction has no .submit() (that's a PendingTransaction
    // method); it goes to the node through the RPC client, same as reclaim.
    // allowOrphan:false — every escrow UTXO is already confirmed, so an orphan
    // means the tx is malformed, not that a parent is still in flight.
    const resp = await rpc.submitTransaction({ transaction: tx, allowOrphan: false });
    return { txid: String(resp.transactionId ?? resp) };
  });
}

/** Fee for spending the CLTV branch, per input. Far below the settle fee
 *  because this is a single-sig spend (sigOpCount 1) rather than a 2-of-3
 *  CHECKMULTISIG. ⚠️ Mirrored in site/backend/config.py as
 *  RECLAIM_FEE_SOMPI_PER_INPUT — change both or the backend will quote a
 *  payout the chain doesn't deliver. */
const RECLAIM_FEE_SOMPI_PER_INPUT = 10_000_000n; // 0.1 KAS

/** POST /escrow/reclaim-unsigned — build the tx that walks a stranded stake
 *  back out of ONE escrow via its ELSE (timelock) branch, and hand it to the
 *  site unsigned. Nothing here is signed and no key is touched: the reclaim
 *  branch is `<reclaimDaa> CLTV <pkDepositor> CHECKSIG`, so only the
 *  depositor's own wallet can complete it. That is the point — reclaim has to
 *  keep working if DAGmate is gone, and a builder that needed our key would
 *  quietly make that false.
 *
 *  Four things make this different from buildSettleUnsigned, all of them
 *  money-critical:
 *
 *  1. It uses the LOW-LEVEL `createTransaction()`, not `createTransactions()`,
 *     because the high-level builder returns a PendingTransaction whose
 *     `.transaction` is a SNAPSHOT — assigning `lockTime` to it does not
 *     persist, and a tx without the lock time fails CLTV.
 *  2. ⚠️ `createTransaction()` has NO automatic change output. The output
 *     amount is computed exactly as `total - fee`; anything left unspent is
 *     silently donated to a miner. There is deliberately no change address:
 *     one input set, one output, one number to get right.
 *  3. `lockTime` must be >= the script's `reclaimDaa` (the in-script check is
 *     `stack_value <= tx.lock_time`). It is set to exactly `reclaimDaa` so
 *     the tx becomes valid at the earliest moment it legally can.
 *  4. EVERY input's `sequence` must differ from MAX_TX_IN_SEQUENCE_NUM or
 *     both the in-script CLTV and the consensus finality check are skipped
 *     outright — the timelock would be decorative. The mutated array is
 *     assigned back because `.inputs` may hand out clones, not live refs. */
export async function buildReclaimUnsigned({ address, depositorAddr, reclaimDaa }) {
  if (!address) throw new Error('address required (the escrow to drain)');
  if (!depositorAddr) throw new Error('depositorAddr required (where the stake goes back to)');
  if (reclaimDaa == null) throw new Error('reclaimDaa required');
  const k = core.wasm();
  const lockTime = BigInt(reclaimDaa);

  return core.withRpc(async (rpc) => {
    const info = await rpc.getBlockDagInfo();
    const tipDaa = BigInt(info.virtualDaaScore);
    // Refused rather than built-and-rejected. The node would throw this tx out
    // anyway (consensus finality), but a wallet popup the player can only
    // approve into a failure is a worse way to learn the date.
    if (tipDaa < lockTime) {
      throw new Error(`timelock hasn't opened yet — reclaimable at DAA ${lockTime}, chain is at ${tipDaa}`);
    }

    const { entries } = await rpc.getUtxosByAddresses({ addresses: [address] });
    if (!entries.length) throw new Error('nothing left in this escrow to reclaim');
    const totalSompi = entries.reduce((s, e) => s + BigInt(e.amount), 0n);
    const fee = RECLAIM_FEE_SOMPI_PER_INPUT * BigInt(entries.length);
    const payout = totalSompi - fee;
    if (payout <= 0n) {
      throw new Error('what is left in this escrow is smaller than the network fee needed to move it');
    }

    const txn = k.createTransaction(entries, [{ address: depositorAddr, amount: payout }], fee, undefined, 1);
    txn.lockTime = lockTime;
    const inputs = txn.inputs;
    for (const inp of inputs) inp.sequence = 0n; // != MAX, or CLTV is skipped
    txn.inputs = inputs; // commit back — .inputs may return clones

    return {
      txJson: txn.serializeToSafeJSON(),
      inputs: entries.map((e, i) => ({ index: i, address: e.address.toString() })),
      totalSompi: totalSompi.toString(), feeSompi: fee.toString(), payoutSompi: payout.toString(),
      tipDaa: tipDaa.toString(), reclaimDaa: lockTime.toString(),
    };
  });
}

/** POST /escrow/reclaim-broadcast — take the tx from buildReclaimUnsigned,
 *  now carrying the depositor's own signature per input, finish the sigScripts
 *  and submit.
 *
 *  The witness is `<sig> OP_FALSE`: the trailing FALSE selects the script's
 *  ELSE branch (settle is the same shape with OP_TRUE). One signature, no
 *  arbiter — there is no second party to wait on, which is what makes this a
 *  one-visit flow while settlement is a multi-visit one. */
export async function broadcastReclaim({ txJson, redeemHex, sigs }) {
  if (!txJson) throw new Error('txJson required');
  if (!redeemHex) throw new Error('redeemHex required');
  if (!Array.isArray(sigs) || !sigs.length) throw new Error('sigs required, one per input');
  const k = core.wasm();
  return core.withRpc(async (rpc) => {
    const tx = k.Transaction.deserializeFromSafeJSON(txJson);
    const inputs = tx.inputs;  // may be clones — mutate then reassign to commit
    if (inputs.length !== sigs.length) {
      throw new Error(`expected ${inputs.length} signatures, got ${sigs.length}`);
    }
    for (let i = 0; i < sigs.length; i++) {
      if (!sigs[i]) throw new Error(`missing signature for input ${i}`);
      const redeem = new k.ScriptBuilder(core.COVENANT_OPTS)
        .addData(rawSig(sigs[i]))
        .addOp(k.Opcodes.OpFalse) // select the ELSE (timelock reclaim) branch
        .drain();
      inputs[i].signatureScript = k.ScriptBuilder.fromScript(redeemHex, core.COVENANT_OPTS)
        .encodePayToScriptHashSignatureScript(redeem);
    }
    tx.inputs = inputs; // low-level Transaction has no fillInput(); commit the array back
    // Explicit `allowOrphan: false`: a reclaim spends a confirmed UTXO that has
    // been sitting for two weeks, so an orphan here means something is wrong
    // with the tx, not that a parent is in flight.
    const resp = await rpc.submitTransaction({ transaction: tx, allowOrphan: false });
    return { txid: String(resp.transactionId ?? resp) };
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
