/**
 * DAGmate — escrow + settlement builders (CHESS_SPEC.md §2, §3).
 *
 * Reuses kron.js's already-loaded WASM instance + serialized RPC queue
 * (`core.wasm()` / `core.withRpc`) rather than standing up a second WASM/RPC
 * stack: `loadKaspa()` is a process-wide singleton (see
 * @kronsdk/kron-sdk/dist/wasm/index.node.js), so a second independent
 * RpcClient racing kron.js's own would defeat the "only one WASM session at
 * a time" guarantee kron.js's rpcDo exists to provide (see its comment).
 *
 * Script rules baked in here are proven on mainnet dust, not guessed — see
 * CHESS_SPEC.md §5.1 and kron-service/src/chess_spike.mjs for the spikes
 * that found them:
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
import * as core from './kron.js';

const H = (h) => Uint8Array.from(Buffer.from(String(h).replace(/^0x/, ''), 'hex'));
const rawSig = (s) => { const b = Buffer.from(String(s), 'hex'); return b.length === 66 ? b.subarray(1) : b; };

/** x-only (32-byte) schnorr pubkey hex for a private key, as ScriptBuilder wants it. */
function xOnlyHex(key) {
  const pub = key.toPublicKey ? key.toPublicKey() : key;
  const xo = pub.toXOnlyPublicKey ? pub.toXOnlyPublicKey() : pub;
  return String(xo.toString()).replace(/^0x/, '');
}

/** GET /chess/pubkey — x-only pubkey for a player wallet (account 0, `index`
 *  is the user's wallet index) or a per-match arbiter key (account 1,
 *  `index` is then the matchId — CHESS_SPEC.md §2.1). */
export function chessPubkey({ index, account = 0 }) {
  const acc = Number(account);
  if (acc !== 0 && acc !== 1) throw new Error('account must be 0 (player) or 1 (arbiter)');
  if (index == null) throw new Error('index required');
  const { key } = acc === 0 ? core.deriveUser(Number(index)) : core.deriveArbiter(index);
  return { pubkey: xOnlyHex(key) };
}

/** POST /chess/escrow — build the per-player escrow redeem script + P2SH
 *  address. Pure function: no chain calls, no signing. Two branches:
 *    IF   OP_2 <pkA> <pkB> <pkArbiter> OP_3 OP_CHECKMULTISIG      — settle
 *    ELSE <reclaimDaa> OP_CHECKLOCKTIMEVERIFY <pkDepositor> OP_CHECKSIG  — 14d reclaim
 *  `depositorIsA` selects which player's key backs the reclaim branch of
 *  THIS particular escrow address — each player gets their own (§2.2: an
 *  abandoned match degrades to "everyone reclaims their own stake"). */
export function chessEscrow({ matchId, pkA, pkB, depositorIsA, reclaimDaa }) {
  if (matchId == null) throw new Error('matchId required');
  if (!pkA || !pkB) throw new Error('pkA and pkB required (x-only pubkey hex)');
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
  sb.addOp(k.Opcodes.OpCheckLockTimeVerify); // pops the locktime itself — no OP_DROP (spike S3)
  sb.addData(depositorPk);
  sb.addOp(k.Opcodes.OpCheckSig);
  sb.addOp(k.Opcodes.OpEndIf);
  const redeemHex = sb.drain();

  const spk = k.ScriptBuilder.fromScript(redeemHex, core.COVENANT_OPTS).createPayToScriptHashScript();
  const address = k.addressFromScriptPublicKey(spk, core.netType()).toString();
  return { address, redeemHex, arbiterIndex: Number(matchId) };
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

/** POST /chess/settle — spend one or both escrow addresses' UTXOs in a single
 *  tx via the IF (2-of-3) branch.
 *  `escrows`: [{ address, redeemHex, depositorIndex }] (1 or 2 entries — an
 *  escrow can carry more than one UTXO if externally topped up; every UTXO at
 *  a listed address is swept in).
 *  `winnerIndex` — decisive result: that wallet's key co-signs EVERY input.
 *  `split: true` — draw: each escrow is co-signed by its OWN `depositorIndex`
 *  and the pot is split evenly.
 *  submit:false → dry-run (payout preview only, nothing signed/broadcast). */
export async function chessSettle({ matchId, escrows, winnerIndex, split, rakeSompi = 0n, submit = false }) {
  if (matchId == null) throw new Error('matchId required');
  if (!Array.isArray(escrows) || !escrows.length) throw new Error('escrows required');
  if (winnerIndex == null && !split) throw new Error('winnerIndex or split required');
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
    // CHECKMULTISIG spend costs (spike S2 needed an explicit override too).
    const priorityFee = 60_000_000n * BigInt(entries.length);

    let outputs;
    if (winnerIndex != null) {
      const { address: winnerAddr } = core.deriveUser(Number(winnerIndex));
      const payout = potSompi - rake - priorityFee;
      if (payout <= 0n) throw new Error('pot too small to cover rake + fee');
      outputs = rake > 0n
        ? [{ address: winnerAddr, amount: payout }, { address: core.feeAddress(), amount: rake }]
        : [{ address: winnerAddr, amount: payout }];
    } else {
      const half = (potSompi - rake - priorityFee) / 2n;
      if (half <= 0n) throw new Error('pot too small to cover rake + fee');
      const { address: aAddr } = core.deriveUser(Number(escrows[0].depositorIndex));
      const { address: bAddr } = core.deriveUser(Number(escrows[1].depositorIndex));
      outputs = rake > 0n
        ? [{ address: aAddr, amount: half }, { address: bAddr, amount: half }, { address: core.feeAddress(), amount: rake }]
        : [{ address: aAddr, amount: half }, { address: bAddr, amount: half }];
    }

    if (!submit) {
      return {
        potSompi: potSompi.toString(), rakeSompi: rake.toString(),
        payoutPreview: outputs.map((o) => ({ address: o.address, amount: o.amount.toString() })),
      };
    }

    const { transactions } = await k.createTransactions({
      entries, outputs, changeAddress: core.feeAddress(), priorityFee, networkId: core.network(),
      sigOpCount: 3, // CHECKMULTISIG billed by pubkey-count (n=3), not required-sig-count (m=2) — spike S2
    });
    const tx = transactions[0];
    for (let i = 0; i < entries.length; i++) {
      const addr = entries[i].address.toString();
      const escrow = escrows.find((e) => e.address === addr);
      if (!escrow) throw new Error(`UTXO at unexpected address ${addr}`);
      const signerIndex = winnerIndex != null ? Number(winnerIndex) : Number(escrow.depositorIndex);
      const { key: playerKey } = core.deriveUser(signerIndex);
      const sigPlayer = tx.createInputSignature(i, playerKey);
      const sigArb = tx.createInputSignature(i, arbKey);
      fillEscrowInput(k, tx, i, escrow.redeemHex, sigPlayer, sigArb);
    }
    const txid = await tx.submit(rpc);
    return { txid, potSompi: potSompi.toString(), rakeSompi: rake.toString() };
  });
}

/** POST /chess/anchor — dust self-spend (or a fee-wallet payment if
 *  feeSompi>0) from wallet[index] carrying a DGCHS move-anchor payload.
 *  Payload support on this SDK confirmed by spike S1. */
export async function chessAnchor({ index, payloadHex, feeSompi = 0n }) {
  if (index == null) throw new Error('index required');
  if (!payloadHex) throw new Error('payloadHex required');
  const k = core.wasm();
  const { key, address } = core.deriveUser(Number(index));
  const fee = BigInt(feeSompi ?? 0);
  return core.withRpc(async (rpc) => {
    const { entries } = await rpc.getUtxosByAddresses({ addresses: [address] });
    if (!entries.length) throw new Error('no UTXO to anchor from');
    const outputs = fee > 0n ? [{ address: core.feeAddress(), amount: fee }] : [];
    const { transactions } = await k.createTransactions({
      entries, outputs, changeAddress: address, priorityFee: 0n, networkId: core.network(),
      payload: String(payloadHex),
    });
    let txid = null;
    for (const tx of transactions) { tx.sign([key]); txid = await tx.submit(rpc); }
    return { txid };
  });
}

/** GET /chess/daa — current virtual DAA score, so the bot can compute a
 *  reclaim deadline (`reclaimDaa = current + ~14 days of DAA`, CHESS_SPEC.md
 *  §2.3) without embedding chain-tip knowledge in Python. */
export async function chessDaaScore() {
  return core.withRpc(async (rpc) => {
    const info = await rpc.getBlockDagInfo();
    return { daaScore: String(info.virtualDaaScore) };
  });
}
