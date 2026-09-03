/**
 * DAGmate — Covenant Escrow v2 (roadmap #2, docs/DAGMATE_COVENANT_V2.md).
 *
 * Replaces v1's 2-of-3 arbiter co-sign (service/escrow.js) with a KIP-10 introspection
 * covenant. DAGmate stops being a live co-signer and becomes a WRITE-ONCE ORACLE: it signs
 * "player A won" or "player B won" for a match, and the escrow SCRIPT ITSELF verifies that
 * signature (OpCheckSigFromStack) and forces the pot to the declared winner, in full
 * (OpTxOutputSpk + OpTxOutputAmount). The oracle cannot skim, redirect, or replay across
 * matches; settlement needs NO player signature, so the winner is paid the moment the result
 * is signed — DAGmate (or anyone holding the signed result) just relays the tx.
 *
 * Every opcode idiom here was proven on Kaspa mainnet dust before this file existed — see
 * service/spikes_covenant.mjs (S5a/b/c primitives, S6 full settle, S6adv adversarial matrix).
 *
 * Shares core.js with v1 (same WASM, same RPC pool, same HD seed). The oracle key is the SAME
 * per-match key v1 derived as the "arbiter" (core.deriveArbiter) — its ROLE changes (sign a
 * result vs co-sign a tx), not its derivation, so nothing about key management moves.
 *
 * ⚠️ Gated behind ESCROW_V2 at the call sites; v1 escrows keep settling on v1. The two never
 * mix within a match — a match is built entirely v1 or entirely v2.
 *
 * IRREDUCIBLE LIMIT: a covenant cannot know who won a chess game. The oracle DECLARES the
 * result; a compromised oracle could sign the wrong winner. v2 removes the arbiter's
 * skim/redirect/liveness power, not that residual "trust DAGmate to report the right winner"
 * (roadmap #3 — a signed-move fraud-proof channel — is what shrinks that).
 */
import { createHash } from 'node:crypto';
import * as core from './core.js';

/** Flat fee per settle input. A v2 settle input carries the covenant witness (oracle sig +
 *  two selector ops) — heavier than a bare sig, lighter than v1's 2-of-3 CHECKMULTISIG.
 *  ⚠️ Must stay < SETTLE_V2_MAXFEE_SOMPI (the covenant rejects output < input − maxFee), and
 *  mirrored in site/backend/config.py before launch; re-prove on a funded mainnet escrow. */
const SETTLE_V2_FEE_SOMPI_PER_INPUT = 5_000_000n; // 0.05 KAS
/** The covenant's baked slack: a settle output must be >= its input − this. Bounds the worst
 *  case a hostile relayer could shave to the miner (it benefits nobody — the winner is still
 *  the only payee), so it is kept just above the real per-input fee, never generous. */
const SETTLE_V2_MAXFEE_SOMPI = 15_000_000n; // 0.15 KAS

const sha256 = (buf) => createHash('sha256').update(buf).digest();

/** Normalise any wallet pubkey hex to 32-byte x-only (same rule as escrow.js: Kasware hands
 *  back a 33-byte compressed key, and the script engine rejects anything but x-only). */
function toXOnly(pkHex) {
  const clean = String(pkHex).replace(/^0x/, '').toLowerCase();
  if (clean.length === 64) return clean;
  const k = core.wasm();
  const xo = new k.PublicKey(clean).toXOnlyPublicKey();
  return String(xo.toString()).replace(/^0x/, '').toLowerCase();
}
const H = (hex) => Uint8Array.from(Buffer.from(String(hex).replace(/^0x/, ''), 'hex'));

/** The Kaspa address a player's x-only pubkey pays to (their standard P2PK wallet address) —
 *  this is where the pot is sent, and its scriptPublicKey is what the covenant bakes/enforces. */
function addressForPubkey(xOnlyHex) {
  const k = core.wasm();
  return new k.PublicKey(xOnlyHex).toAddress(core.netType()).toString();
}

/** The exact bytes OpTxOutputSpk(i) returns for a payout to `address`:
 *  ScriptPublicKey::to_bytes() = version (u16 big-endian, 2 bytes) ‖ script (proven in S5c). */
function outputSpkBytes(address) {
  const k = core.wasm();
  const spk = k.payToAddressScript(address);
  const version = Number(spk.version) & 0xffff;
  return Uint8Array.from([(version >> 8) & 0xff, version & 0xff, ...H(spk.script)]);
}

/** Pull the BARE 64-byte Schnorr signature out of signScriptHash() for OpCheckSigFromStack.
 *  signScriptHash returns 66 bytes = [push(1)][schnorr(64)][sighashType(1)]; a from-stack
 *  signature verifies a raw message with no tx sighash, so both extra bytes must go (S5b). */
function oracleSchnorr(sigHex) {
  const b = Buffer.from(String(sigHex), 'hex');
  if (b.length === 66) return b.subarray(1, 65);
  if (b.length === 65) return b.subarray(0, 64);
  return b;
}

// ── the baked per-match / per-escrow constants ──────────────────────────────
const SIDE_A = 0x00, SIDE_B = 0x01;
const WON_A = 0x00, WON_B = 0x01;

/** Per-escrow domain tag: SHA256("DGMTv2" ‖ matchId ‖ side). Unique per escrow, so an oracle
 *  signature for one escrow can never be replayed on another match or the other side. */
function matchTag(matchId, sideByte) {
  return sha256(Buffer.concat([Buffer.from('DGMTv2'), Buffer.from(String(matchId)), Buffer.from([sideByte])]));
}
/** The 32-byte message the oracle signs to declare a winner for one escrow. */
function outcomeMsg(tag, wonByte) {
  return sha256(Buffer.concat([tag, Buffer.from([wonByte])]));
}

/** Build the v2 per-escrow redeem script (hex). One IF/ELSE, settle vs 14-day CLTV reclaim;
 *  the settle branch is a nested IF selecting the oracle-declared winner. See the file header
 *  and spikes_covenant.mjs `v2SettleRedeem` (byte-for-byte the proven form). */
function buildRedeem({ matchId, sideByte, pkAx, pkBx, reclaimDaa }) {
  const k = core.wasm();
  const { key: oracleKey } = core.deriveArbiter(matchId);
  const pkOracle = H(String(oracleKey.toPublicKey().toXOnlyPublicKey().toString()).replace(/^0x/, ''));

  const tag = matchTag(matchId, sideByte);
  const msgA = Uint8Array.from(outcomeMsg(tag, WON_A));
  const msgB = Uint8Array.from(outcomeMsg(tag, WON_B));
  // The winner is paid at their own wallet address; bake each player's payout spk.
  const spkA = outputSpkBytes(addressForPubkey(Buffer.from(pkAx).toString('hex')));
  const spkB = outputSpkBytes(addressForPubkey(Buffer.from(pkBx).toString('hex')));
  const pkDepositor = sideByte === SIDE_A ? pkAx : pkBx; // this escrow's funder reclaims it

  const sb = new k.ScriptBuilder(core.COVENANT_OPTS);
  const winnerLeg = (msg, spk) => {
    // oracle blessed this winner:
    sb.addData(msg).addData(pkOracle).addOp(k.Opcodes.OpCheckSigFromStack).addOp(k.Opcodes.OpVerify);
    // this input's same-index output pays the winner:
    sb.addOp(k.Opcodes.OpTxInputIndex).addOp(k.Opcodes.OpTxOutputSpk).addData(spk).addOp(k.Opcodes.OpEqualVerify);
    // ...in full: (output.amount + maxFee) >= input.amount
    sb.addOp(k.Opcodes.OpTxInputIndex).addOp(k.Opcodes.OpTxOutputAmount);
    sb.addI64(SETTLE_V2_MAXFEE_SOMPI).addOp(k.Opcodes.OpAdd);
    sb.addOp(k.Opcodes.OpTxInputIndex).addOp(k.Opcodes.OpTxInputAmount);
    sb.addOp(k.Opcodes.OpGreaterThanOrEqual);
  };
  sb.addOp(k.Opcodes.OpIf);                       // settle
  sb.addOp(k.Opcodes.OpIf);                       //   winnerSel truthy = B won
  winnerLeg(msgB, spkB);
  sb.addOp(k.Opcodes.OpElse);                     //   A won
  winnerLeg(msgA, spkA);
  sb.addOp(k.Opcodes.OpEndIf);
  sb.addOp(k.Opcodes.OpElse);                     // reclaim (14-day CLTV, depositor-signed)
  sb.addI64(BigInt(reclaimDaa)).addOp(k.Opcodes.OpCheckLockTimeVerify);
  sb.addData(pkDepositor).addOp(k.Opcodes.OpCheckSig);
  sb.addOp(k.Opcodes.OpEndIf);
  return sb.drain();
}

/** POST /escrow-v2/build — build ONE player's v2 escrow (redeem + P2SH address). Pure: no
 *  chain calls, no signing. `side` is 'A' or 'B' (which escrow); `pkA`/`pkB` are the players'
 *  own wallet pubkeys. Mirrors escrow.js buildEscrow, so the deposit watcher is unchanged. */
export function buildEscrowV2({ matchId, pkA, pkB, side, reclaimDaa }) {
  if (matchId == null) throw new Error('matchId required');
  if (!pkA || !pkB) throw new Error('pkA and pkB required (x-only pubkey hex, from each wallet)');
  if (side !== 'A' && side !== 'B') throw new Error("side must be 'A' or 'B'");
  if (reclaimDaa == null) throw new Error('reclaimDaa required');
  const k = core.wasm();
  const redeemHex = buildRedeem({
    matchId, sideByte: side === 'A' ? SIDE_A : SIDE_B,
    pkAx: H(toXOnly(pkA)), pkBx: H(toXOnly(pkB)), reclaimDaa,
  });
  const spk = k.ScriptBuilder.fromScript(redeemHex, core.COVENANT_OPTS).createPayToScriptHashScript();
  const address = k.addressFromScriptPublicKey(spk, core.netType()).toString();
  return { address, redeemHex };
}

/** The oracle's signed verdict for a match — the ONE thing DAGmate produces to settle a v2
 *  game. Two signatures (one per escrow, since each has its own domain tag). PUBLISH these so
 *  the winner (or anyone) can relay the settle even if DAGmate never does — that published
 *  verdict + the covenant is what keeps v2 non-custodial. `winner` is 'A' or 'B'. */
export function oracleSignResult({ matchId, winner }) {
  if (matchId == null) throw new Error('matchId required');
  if (winner !== 'A' && winner !== 'B') throw new Error("winner must be 'A' or 'B'");
  const k = core.wasm();
  const { key: oracleKey } = core.deriveArbiter(matchId);
  const wonByte = winner === 'A' ? WON_A : WON_B;
  const sigFor = (sideByte) => {
    const msg = outcomeMsg(matchTag(matchId, sideByte), wonByte);
    return Buffer.from(oracleSchnorr(k.signScriptHash(Buffer.from(msg).toString('hex'), oracleKey))).toString('hex');
  };
  return { winner, sigA: sigFor(SIDE_A), sigB: sigFor(SIDE_B) };
}

/** Assemble one v2 settle input's sigScript: witness = <oracleSig64> <winnerSel> <OP_TRUE>.
 *  winnerSel truthy selects the B leg; falsy the A leg. */
function settleWitness(k, redeemHex, oracleSig64, winnerIsB) {
  const witness = new k.ScriptBuilder(core.COVENANT_OPTS)
    .addData(H(oracleSig64))
    .addOp(winnerIsB ? k.Opcodes.OpTrue : k.Opcodes.OpFalse) // winner selector
    .addOp(k.Opcodes.OpTrue)                                 // settle branch
    .drain();
  return k.ScriptBuilder.fromScript(redeemHex, core.COVENANT_OPTS).encodePayToScriptHashSignatureScript(witness);
}

/** POST /escrow-v2/settle — build AND submit the settle tx for a decided v2 match. No player
 *  signature is needed: the oracle verdict + the covenant authorise the spend, and the covenant
 *  forces every input's stake to the winner. Idempotent at the chain level (a re-submit of an
 *  already-spent escrow is a harmless double-spend rejection — the caller checks for a prior txid).
 *
 *  `escrows`: [{ address, redeemHex, side }] (1–2). `winnerPk`: the winner's x-only pubkey (the
 *  pot is sent to its wallet address — the same address the covenant bakes, so a wrong one just
 *  fails the covenant rather than misdirecting funds). `winner`: 'A' or 'B'. `sigA`/`sigB`: the
 *  oracle verdict from oracleSignResult (kept out of this function so the SAME verdict can be
 *  relayed by a third party). */
export async function settleV2({ matchId, escrows, winnerPk, winner, sigA, sigB }) {
  if (!Array.isArray(escrows) || !escrows.length) throw new Error('escrows required');
  if (winner !== 'A' && winner !== 'B') throw new Error("winner must be 'A' or 'B'");
  if (!winnerPk) throw new Error('winnerPk required');
  if (!sigA || !sigB) throw new Error('sigA and sigB (oracle verdict) required');
  const k = core.wasm();
  const winnerIsB = winner === 'B';
  const winnerAddr = addressForPubkey(toXOnly(winnerPk));
  const sigBySide = { A: sigA, B: sigB };
  const byAddress = new Map(escrows.map((e) => [e.address, e]));

  return core.withRpc(async (rpc) => {
    const { entries } = await rpc.getUtxosByAddresses({ addresses: escrows.map((e) => e.address) });
    if (!entries.length) throw new Error('no v2 escrow UTXOs found — has the match been funded?');
    const fee = SETTLE_V2_FEE_SOMPI_PER_INPUT * BigInt(entries.length);

    // 1 output per input, same index, each paying the winner input−perInputFee. The covenant
    // binds output[i] to input[i], so the ordering is load-bearing: build outputs in entry order.
    const outputs = entries.map((e) => {
      const amt = BigInt(e.amount) - SETTLE_V2_FEE_SOMPI_PER_INPUT;
      if (amt <= 0n) throw new Error('an escrow UTXO is too small to cover the settle fee');
      return { address: winnerAddr, amount: amt };
    });
    const tx = k.createTransaction(entries, outputs, fee, undefined, 1);

    const ins = tx.inputs;
    entries.forEach((e, i) => {
      const escrow = byAddress.get(String(e.address));
      if (!escrow) throw new Error(`settle input ${i} spends an unknown escrow ${e.address}`);
      const oracleSig = sigBySide[escrow.side];
      if (!oracleSig) throw new Error(`no oracle signature for escrow side ${escrow.side}`);
      ins[i].signatureScript = settleWitness(k, escrow.redeemHex, oracleSig, winnerIsB);
    });
    tx.inputs = ins;

    const resp = await rpc.submitTransaction({ transaction: tx, allowOrphan: false });
    return {
      txid: String(resp.transactionId ?? resp),
      potSompi: entries.reduce((s, e) => s + BigInt(e.amount), 0n).toString(),
      feeSompi: fee.toString(),
      winnerAddr,
    };
  });
}

/* Reclaim (the ELSE branch) is byte-identical to v1: the depositor spends it after the CLTV
 * with witness `<sig> OP_FALSE`. So a stranded v2 escrow reclaims through the EXISTING
 * escrow.js buildReclaimUnsigned/broadcastReclaim, passing the v2 redeemHex — no new code, and
 * the same "DAGmate can be gone and you still get your stake back" guarantee. */
