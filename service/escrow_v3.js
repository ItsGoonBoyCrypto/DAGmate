/**
 * DAGmate — Covenant Escrow v3 (roadmap #3a, docs/DAGMATE_ROADMAP_3A.md + DAGMATE_MOVE_CHANNEL.md).
 *
 * v2 (escrow_v2.js) removed the arbiter's skim/redirect power but still trusts DAGmate's ORACLE to
 * declare the winner. v3 adds a TRUSTLESS forfeit path for the abandonment/clock-flag case: a game
 * where a player stops moving is collected by the opponent with NO oracle signature, using a co-signed
 * off-chain checkpoint (move_channel.mjs) and the forfeit covenant legs proven on mainnet
 * (spikes_forfeit.mjs S8/S9/S10/S11/S11b/S11n) — combined with v2's settle legs into ONE per-side
 * redeem (S12, mainnet: the 1047B combined redeem's forfeit spend clears compute-mass).
 *
 * ONE combined redeem, three ways to spend, selected by nested IFs:
 *   SETTLE  (oracle-declared checkmate/resign/draw) — IDENTICAL to v2: OpCheckSigFromStack over the
 *           oracle's verdict; the covenant forces the pot to the declared party.
 *   FORFEIT (timeout) — spend into the S10 pending-forfeit covenant (challenge window), reconstructing
 *           its P2SH on-chain from a co-signed checkpoint. Two-direction (either player may claim).
 *   RECLAIM (14-day CLTV) — byte-identical to v1/v2; the depositor gets their stake back.
 *
 * Gated behind ESCROW_V3 at the call sites; v1/v2 matches finish on their own version.
 * The oracle key and the SETTLE path are shared with v2 (same core.deriveArbiter, same DGMTv2 tag),
 * so v3 is strictly "v2 + a forfeit branch".
 *
 * ⚠️ The byte layout of the redeem and the checkpoint hash are FROZEN by the on-chain proofs. Any
 * change needs a fresh spike (spikes_forfeit.mjs) — a drifted layout means a co-signed checkpoint the
 * covenant can't verify, or a reconstructed pending address the pot never lands at.
 */
import { createHash } from 'node:crypto';
import * as core from './core.js';

const SETTLE_V3_FEE_SOMPI_PER_INPUT = 5_000_000n;   // mirror site/backend/config.py SETTLE_V3_*
const SETTLE_V3_MAXFEE_SOMPI = 15_000_000n;          // covenant rejects output < input − MAXFEE
const sha256 = (buf) => createHash('sha256').update(buf).digest();

const SIDE_A = 0x00, SIDE_B = 0x01;
const WON_A = 0x00, WON_B = 0x01, WON_DRAW = 0x02;
const CLAIMANT_A = 0x01, CLAIMANT_B = 0x02;

// ── shared helpers (same rules as escrow_v2.js) ──
const H = (hex) => Uint8Array.from(Buffer.from(String(hex).replace(/^0x/, ''), 'hex'));
function toXOnly(pkHex) {
  const clean = String(pkHex).replace(/^0x/, '').toLowerCase();
  if (clean.length === 64) return clean;
  const k = core.wasm();
  return String(new k.PublicKey(clean).toXOnlyPublicKey().toString()).replace(/^0x/, '').toLowerCase();
}
function addressForPubkey(xOnlyHex) {
  const k = core.wasm();
  return new k.PublicKey(xOnlyHex).toAddress(core.netType()).toString();
}
function outputSpkBytes(address) {
  const k = core.wasm();
  const spk = k.payToAddressScript(address);
  const version = Number(spk.version) & 0xffff;
  return Uint8Array.from([(version >> 8) & 0xff, version & 0xff, ...H(spk.script)]);
}
function oracleSchnorr(sigHex) {
  const b = Buffer.from(String(sigHex), 'hex');
  if (b.length === 66) return b.subarray(1, 65);
  if (b.length === 65) return b.subarray(0, 64);
  return b;
}
// minimal LE script-number (matches scriptsim.numToBytes / rusty-kaspa)
function numToBytes(n) {
  n = BigInt(n);
  if (n === 0n) return Buffer.alloc(0);
  const neg = n < 0n; let v = neg ? -n : n; const out = [];
  while (v > 0n) { out.push(Number(v & 0xffn)); v >>= 8n; }
  if (out[out.length - 1] & 0x80) out.push(neg ? 0x80 : 0x00);
  else if (neg) out[out.length - 1] |= 0x80;
  return Buffer.from(out);
}
const plyFixed = (ply) => { const b = Buffer.alloc(2); b.writeUInt16LE(Number(ply)); return b; };

// token list (Buffer = data push, string = opcode) → redeem hex, using covenant opts
function drainTokens(tokens) {
  const k = core.wasm();
  const sb = new k.ScriptBuilder(core.COVENANT_OPTS);
  for (const t of tokens) { if (typeof t === 'string') sb.addOp(k.Opcodes[t]); else sb.addData(Uint8Array.from(t)); }
  return sb.drain();
}
function drainBuf(tokens) { return Buffer.from(drainTokens(tokens), 'hex'); }
function p2shAddress(redeemHex) {
  const k = core.wasm();
  const spk = k.ScriptBuilder.fromScript(redeemHex, core.COVENANT_OPTS).createPayToScriptHashScript();
  return k.addressFromScriptPublicKey(spk, core.netType()).toString();
}
function p2shSig(redeemHex, witnessTokens) {
  const k = core.wasm();
  const w = drainTokens(witnessTokens);
  return k.ScriptBuilder.fromScript(redeemHex, core.COVENANT_OPTS).encodePayToScriptHashSignatureScript(w);
}

// ── v2 SETTLE pieces (shared, unchanged) ──
function settleTag(matchId, sideByte) {
  return sha256(Buffer.concat([Buffer.from('DGMTv2'), Buffer.from(String(matchId)), Buffer.from([sideByte])]));
}
const outcomeMsg = (tag, wonByte) => sha256(Buffer.concat([tag, Buffer.from([wonByte])]));

// ── forfeit pieces ──
/** Per-match checkpoint domain (what both clients' sessions co-sign; distinct from the settle tag).
 *  Deterministic from matchId so clients need no extra coordination to agree on it. */
function checkpointTag(matchId) {
  return sha256(Buffer.concat([Buffer.from('DGMTv3ff'), Buffer.from(String(matchId))]));
}

/** The S10 pending-forfeit covenant, fixed-width claimedPly variant, as {prefix, suffix} token halves.
 *  Baked: spkX(finaliser), spkY(canceller), W(DAA), sess pkA/pkB, checkpoint tag, maxFee. */
function pendingParts({ spkX, spkY, wDaa, sessPkA, sessPkB, ckTag, maxFee }) {
  return {
    prefix: [
      'OpIf',
        'OpTxInputIndex', 'OpTxInputDaaScore', numToBytes(wDaa), 'OpAdd', 'OpCheckLockTimeVerify',
        'OpTxInputIndex', 'OpTxOutputSpk', Buffer.from(spkX), 'OpEqualVerify',
        'OpTxInputIndex', 'OpTxOutputAmount', numToBytes(maxFee), 'OpAdd', 'OpTxInputIndex', 'OpTxInputAmount', 'OpGreaterThanOrEqual',
      'OpElse',
        Buffer.from(ckTag), numToBytes(1), 'OpPick', 'OpCat', 'OpSHA256',
        numToBytes(2), 'OpPick', numToBytes(1), 'OpPick', Buffer.from(sessPkB), 'OpCheckSigFromStack', 'OpVerify',
        numToBytes(3), 'OpPick', numToBytes(1), 'OpPick', Buffer.from(sessPkA), 'OpCheckSigFromStack', 'OpVerify',
        'OpDrop', 'OpNip', 'OpNip',
    ],
    suffix: [
        'OpBin2Num', 'OpGreaterThan', 'OpVerify',
        'OpTxInputIndex', 'OpTxOutputSpk', Buffer.from(spkY), 'OpEqualVerify',
        'OpTxInputIndex', 'OpTxOutputAmount', numToBytes(maxFee), 'OpAdd', 'OpTxInputIndex', 'OpTxInputAmount', 'OpGreaterThanOrEqual',
      'OpEndIf',
    ],
  };
}

/** The pending covenant redeem for a specific CLAIMED ply (rebuilt by whoever finalises/cancels). */
function buildPendingRedeem({ claimant, ply, addrA, addrB, wDaa, sessPkA, sessPkB, ckTag, maxFee = SETTLE_V3_MAXFEE_SOMPI }) {
  // finaliser = the claimant; canceller = the opponent
  const spkFinaliser = outputSpkBytes(claimant === CLAIMANT_A ? addrA : addrB);
  const spkCanceller = outputSpkBytes(claimant === CLAIMANT_A ? addrB : addrA);
  const { prefix, suffix } = pendingParts({ spkX: spkFinaliser, spkY: spkCanceller, wDaa, sessPkA, sessPkB, ckTag, maxFee });
  return drainTokens([...prefix, plyFixed(ply), ...suffix]);
}

/** The two-direction forfeit BODY (proven S11b/S12). Baked: checkpoint tag, sess pkA/pkB, and the two
 *  pending covenants' prefix/suffix halves (PREFIX carries the trailing 0x02 ply push-len). */
function forfeitBodyTokens({ ckTag, sessPkA, sessPkB, addrA, addrB, wDaa, maxFee }) {
  const VER_AA20 = Buffer.from([0x00, 0x00, 0xaa, 0x20]), TAIL = Buffer.from([0x87]);
  const pa = pendingParts({ spkX: outputSpkBytes(addrA), spkY: outputSpkBytes(addrB), wDaa, sessPkA, sessPkB, ckTag, maxFee });
  const pb = pendingParts({ spkX: outputSpkBytes(addrB), spkY: outputSpkBytes(addrA), wDaa, sessPkA, sessPkB, ckTag, maxFee });
  const prefixA = Buffer.concat([drainBuf(pa.prefix), Buffer.from([0x02])]), suffixA = drainBuf(pa.suffix);
  const prefixB = Buffer.concat([drainBuf(pb.prefix), Buffer.from([0x02])]), suffixB = drainBuf(pb.suffix);
  return [
    Buffer.from(ckTag), numToBytes(5), 'OpPick', 'OpCat', numToBytes(4), 'OpPick', 'OpCat', numToBytes(3), 'OpPick', 'OpCat', 'OpSHA256',
    numToBytes(1), 'OpPick', numToBytes(1), 'OpPick', Buffer.from(sessPkB), 'OpCheckSigFromStack', 'OpVerify',
    numToBytes(2), 'OpPick', numToBytes(1), 'OpPick', Buffer.from(sessPkA), 'OpCheckSigFromStack', 'OpVerify',
    'OpDrop', 'OpDrop', 'OpDrop',
    numToBytes(2), 'OpRoll', 'OpCheckLockTimeVerify',
    'OpDup', Buffer.from([0x01]), 'OpEqual', 'OpIf', Buffer.from(suffixA), 'OpElse', Buffer.from(suffixB), 'OpEndIf',
    'OpSwap', Buffer.from([0x01]), 'OpEqual', 'OpIf', Buffer.from(prefixA), 'OpElse', Buffer.from(prefixB), 'OpEndIf',
    numToBytes(2), 'OpRoll', 'OpCat', 'OpSwap', 'OpCat',
    'OpBlake2b',
    VER_AA20, 'OpSwap', 'OpCat', TAIL, 'OpCat',
    'OpTxInputIndex', 'OpTxOutputSpk', 'OpEqualVerify',
    'OpTxInputIndex', 'OpTxOutputAmount', numToBytes(maxFee), 'OpAdd', 'OpTxInputIndex', 'OpTxInputAmount', 'OpGreaterThanOrEqual',
  ];
}

/** The combined v3 redeem (S12-proven): settle subtree + forfeit body + CLTV reclaim, nested IFs. */
function buildRedeemV3({ matchId, sideByte, pkAx, pkBx, reclaimDaa, sessPkA, sessPkB, wDaa }) {
  const k = core.wasm();
  const { key: oracleKey } = core.deriveArbiter(matchId);
  const pkOracle = Buffer.from(H(String(oracleKey.toPublicKey().toXOnlyPublicKey().toString()).replace(/^0x/, '')));
  const tag = settleTag(matchId, sideByte);
  const msgA = Buffer.from(outcomeMsg(tag, WON_A)), msgB = Buffer.from(outcomeMsg(tag, WON_B)), msgDraw = Buffer.from(outcomeMsg(tag, WON_DRAW));
  const addrA = addressForPubkey(Buffer.from(pkAx).toString('hex')), addrB = addressForPubkey(Buffer.from(pkBx).toString('hex'));
  const spkA = Buffer.from(outputSpkBytes(addrA)), spkB = Buffer.from(outputSpkBytes(addrB));
  const spkDep = sideByte === SIDE_A ? spkA : spkB;
  const pkDep = sideByte === SIDE_A ? Buffer.from(pkAx) : Buffer.from(pkBx);
  const ckTag = checkpointTag(matchId);
  const mf = SETTLE_V3_MAXFEE_SOMPI;
  const leg = (msg, spk) => [
    msg, pkOracle, 'OpCheckSigFromStack', 'OpVerify',
    'OpTxInputIndex', 'OpTxOutputSpk', spk, 'OpEqualVerify',
    'OpTxInputIndex', 'OpTxOutputAmount', numToBytes(mf), 'OpAdd', 'OpTxInputIndex', 'OpTxInputAmount', 'OpGreaterThanOrEqual',
  ];
  const forfeit = forfeitBodyTokens({ ckTag, sessPkA, sessPkB, addrA, addrB, wDaa, maxFee: mf });
  const tokens = [
    'OpIf',
      'OpIf', 'OpDrop', ...leg(msgDraw, spkDep),
      'OpElse', 'OpIf', ...leg(msgB, spkB), 'OpElse', ...leg(msgA, spkA), 'OpEndIf',
      'OpEndIf',
    'OpElse',
      'OpIf', ...forfeit,
      'OpElse', numToBytes(BigInt(reclaimDaa)), 'OpCheckLockTimeVerify', pkDep, 'OpCheckSig',
      'OpEndIf',
    'OpEndIf',
  ];
  return drainTokens(tokens);
}

/** POST /escrow-v3/build — build ONE player's v3 escrow (redeem + P2SH address). Pure: no chain, no
 *  signing. `side` 'A'|'B'; `pkA`/`pkB` are the players' WALLET pubkeys (settle/reclaim/payout);
 *  `sessPkA`/`sessPkB` are the per-match SESSION x-only pubkeys that co-sign checkpoints (move_channel).
 *  `wDaa` is the challenge window in DAA. Returns the checkpoint tag so clients can build C. */
export function buildEscrowV3({ matchId, pkA, pkB, side, reclaimDaa, sessPkA, sessPkB, wDaa }) {
  if (matchId == null) throw new Error('matchId required');
  if (!pkA || !pkB) throw new Error('pkA and pkB required (wallet x-only pubkey hex)');
  if (!sessPkA || !sessPkB) throw new Error('sessPkA and sessPkB required (session x-only pubkey hex)');
  if (side !== 'A' && side !== 'B') throw new Error("side must be 'A' or 'B'");
  if (reclaimDaa == null) throw new Error('reclaimDaa required');
  if (wDaa == null) throw new Error('wDaa (challenge window in DAA) required');
  const redeemHex = buildRedeemV3({
    matchId, sideByte: side === 'A' ? SIDE_A : SIDE_B,
    pkAx: H(toXOnly(pkA)), pkBx: H(toXOnly(pkB)),
    sessPkA: H(toXOnly(sessPkA)), sessPkB: H(toXOnly(sessPkB)),
    reclaimDaa, wDaa,
  });
  return { address: p2shAddress(redeemHex), redeemHex, checkpointTag: Buffer.from(checkpointTag(matchId)).toString('hex') };
}

/** The oracle verdict for a v3 SETTLE — identical to v2 (same key, same DGMTv2 tag). */
export function oracleSignResult({ matchId, outcome }) {
  if (matchId == null) throw new Error('matchId required');
  if (!['A', 'B', 'draw'].includes(outcome)) throw new Error("outcome must be 'A', 'B' or 'draw'");
  const k = core.wasm();
  const { key: oracleKey } = core.deriveArbiter(matchId);
  const wonByte = outcome === 'A' ? WON_A : outcome === 'B' ? WON_B : WON_DRAW;
  const sigFor = (sideByte) => {
    const msg = outcomeMsg(settleTag(matchId, sideByte), wonByte);
    return Buffer.from(oracleSchnorr(k.signScriptHash(Buffer.from(msg).toString('hex'), oracleKey))).toString('hex');
  };
  return { outcome, sigA: sigFor(SIDE_A), sigB: sigFor(SIDE_B) };
}

function settleWitness(redeemHex, oracleSig64, outcome) {
  return p2shSig(redeemHex, [
    H(oracleSig64),
    outcome === 'B' ? numToBytes(1) : Buffer.alloc(0),   // winnerSel (unused on draw)
    outcome === 'draw' ? numToBytes(1) : Buffer.alloc(0), // isDraw
    numToBytes(1),                                        // settle branch
  ]);
}

/** POST /escrow-v3/settle — oracle-settle a decided v3 match (checkmate/resign/draw). Same shape as
 *  settleV2: no player signature; each input pays the required party. `escrows`:[{address,redeemHex,side}]. */
export async function settleV3({ escrows, outcome, pkA, pkB, sigA, sigB }) {
  if (!Array.isArray(escrows) || !escrows.length) throw new Error('escrows required');
  if (!['A', 'B', 'draw'].includes(outcome)) throw new Error("outcome must be 'A', 'B' or 'draw'");
  if (!pkA || !pkB || !sigA || !sigB) throw new Error('pkA, pkB and the oracle verdict (sigA,sigB) required');
  const k = core.wasm();
  const addrA = addressForPubkey(toXOnly(pkA)), addrB = addressForPubkey(toXOnly(pkB));
  const sigBySide = { A: sigA, B: sigB };
  const byAddress = new Map(escrows.map((e) => [e.address, e]));
  const destForSide = (side) => outcome === 'draw' ? (side === 'A' ? addrA : addrB) : (outcome === 'A' ? addrA : addrB);
  return core.withRpc(async (rpc) => {
    const { entries } = await rpc.getUtxosByAddresses({ addresses: escrows.map((e) => e.address) });
    if (!entries.length) throw new Error('no v3 escrow UTXOs found — funded?');
    const fee = SETTLE_V3_FEE_SOMPI_PER_INPUT * BigInt(entries.length);
    const perInput = entries.map((e, i) => {
      const escrow = byAddress.get(String(e.address));
      if (!escrow) throw new Error(`settle input ${i} spends an unknown escrow ${e.address}`);
      return { e, escrow };
    });
    const outputs = perInput.map(({ e, escrow }) => {
      const amt = BigInt(e.amount) - SETTLE_V3_FEE_SOMPI_PER_INPUT;
      if (amt <= 0n) throw new Error('an escrow UTXO is too small to cover the settle fee');
      return { address: destForSide(escrow.side), amount: amt };
    });
    const tx = k.createTransaction(entries, outputs, fee, undefined, 1);
    const ins = tx.inputs;
    perInput.forEach(({ escrow }, i) => { ins[i].signatureScript = settleWitness(escrow.redeemHex, sigBySide[escrow.side], outcome); });
    tx.inputs = ins;
    const resp = await rpc.submitTransaction({ transaction: tx, allowOrphan: false });
    return { txid: String(resp.transactionId ?? resp), potSompi: entries.reduce((s, e) => s + BigInt(e.amount), 0n).toString(), feeSompi: fee.toString(), outcome };
  });
}

/** POST /escrow-v3/forfeit-claim — spend the v3 escrow(s) into the pending-forfeit covenant on a
 *  co-signed checkpoint whose deadline has lapsed. `escrows` is [{address, redeemHex}] (both stakes —
 *  each side's escrow) spent in ONE tx so it's atomic: both stakes move into the pending covenant or
 *  neither does (no partial state where the winner's prize is stranded). Both forfeit branches
 *  reconstruct the SAME pending address (the forfeit body is identical across sides), so every input
 *  pays its matching-index output there. `claimant` 'A'|'B'. No oracle involved. */
export async function forfeitClaim({ escrows, matchId, pkA, pkB, sessPkA, sessPkB, wDaa, deadlineDaa, ply, claimant, sigA, sigB }) {
  if (!Array.isArray(escrows) || !escrows.length) throw new Error('escrows [{address, redeemHex}] required');
  if (claimant !== 'A' && claimant !== 'B') throw new Error("claimant must be 'A' or 'B'");
  if (!sigA || !sigB) throw new Error('sigA and sigB (both session co-signatures over the checkpoint) required');
  const k = core.wasm();
  const claimantByte = claimant === 'A' ? CLAIMANT_A : CLAIMANT_B;
  const addrA = addressForPubkey(toXOnly(pkA)), addrB = addressForPubkey(toXOnly(pkB));
  const ckTag = checkpointTag(matchId);
  const pendingRedeem = buildPendingRedeem({
    claimant: claimantByte, ply, addrA, addrB, wDaa,
    sessPkA: H(toXOnly(sessPkA)), sessPkB: H(toXOnly(sessPkB)), ckTag,
  });
  const pendingAddress = p2shAddress(pendingRedeem);
  const byAddress = new Map(escrows.map((e) => [e.address, e]));
  return core.withRpc(async (rpc) => {
    const { entries } = await rpc.getUtxosByAddresses({ addresses: escrows.map((e) => e.address) });
    if (!entries.length) throw new Error('no escrow UTXO to forfeit-claim — funded/already spent?');
    const fee = SETTLE_V3_FEE_SOMPI_PER_INPUT * BigInt(entries.length);
    // one pending-address output per input, SAME index (the forfeit body binds output[i] to input[i]).
    const perInput = entries.map((e) => { const esc = byAddress.get(String(e.address)); if (!esc) throw new Error(`claim input spends unknown escrow ${e.address}`); return { e, esc }; });
    const outputs = perInput.map(({ e }) => ({ address: pendingAddress, amount: BigInt(e.amount) - SETTLE_V3_FEE_SOMPI_PER_INPUT }));
    const tx = k.createTransaction(entries, outputs, fee, undefined, 5 * entries.length);
    tx.lockTime = BigInt(deadlineDaa);
    const dl = Buffer.from(numToBytes(BigInt(deadlineDaa)));
    const witness = [dl, plyFixed(ply), Buffer.from([claimantByte]), H(sigA), H(sigB), numToBytes(1), Buffer.alloc(0)];
    const ins = tx.inputs;
    perInput.forEach(({ esc }, i) => { ins[i].sequence = 0n; ins[i].signatureScript = p2shSig(esc.redeemHex, witness); });
    tx.inputs = ins;
    const resp = await rpc.submitTransaction({ transaction: tx, allowOrphan: false });
    return { txid: String(resp.transactionId ?? resp), pendingAddress, pendingRedeem, inputs: entries.length };
  });
}

/** POST /escrow-v3/forfeit-finalise — after the challenge window, pay the claimant from the pending
 *  covenant (witness [OP_TRUE]). `windowOpenDaa` = the pending UTXO's creation DAA (deadline = +wDaa). */
export async function forfeitFinalise({ pendingRedeem, pendingAddress, claimant, pkA, pkB, wDaa }) {
  const k = core.wasm();
  const payAddr = claimant === 'A' ? addressForPubkey(toXOnly(pkA)) : addressForPubkey(toXOnly(pkB));
  return core.withRpc(async (rpc) => {
    const { entries } = await rpc.getUtxosByAddresses({ addresses: [pendingAddress] });
    if (!entries.length) throw new Error('no pending-forfeit UTXO (already finalised/cancelled?)');
    // Both escrows forfeit into the SAME pending covenant, so there can be >1 UTXO here. The FINALIZE
    // leg binds output[i] to input[i] (spk == claimant AND amount+maxFee >= input[i]), so we need ONE
    // output per input — all paying the claimant — NOT a single combined output. Each input's leg also
    // checks its OWN creation DAA + wDaa <= tx.lockTime, so lockTime must clear the LATEST input.
    const maxInDaa = entries.reduce((mx, e) => { const d = BigInt(e.blockDaaScore); return d > mx ? d : mx; }, 0n);
    const fee = SETTLE_V3_FEE_SOMPI_PER_INPUT * BigInt(entries.length);
    const outputs = entries.map((e) => ({ address: payAddr, amount: BigInt(e.amount) - SETTLE_V3_FEE_SOMPI_PER_INPUT }));
    const tx = k.createTransaction(entries, outputs, fee, undefined, 2 * entries.length);
    tx.lockTime = maxInDaa + BigInt(wDaa);
    const ins = tx.inputs;
    for (let i = 0; i < ins.length; i++) { ins[i].sequence = 0n; ins[i].signatureScript = p2shSig(pendingRedeem, [numToBytes(1)]); }
    tx.inputs = ins;
    const resp = await rpc.submitTransaction({ transaction: tx, allowOrphan: false });
    return { txid: String(resp.transactionId ?? resp), paid: payAddr, inputs: entries.length };
  });
}

/** POST /escrow-v3/forfeit-cancel — within the window, void a bogus claim with a NEWER co-signed
 *  checkpoint (ply' > claimedPly): pays the canceller (the wrongly-accused player). Witness
 *  [sigA' sigB' ply' OP_FALSE]. */
export async function forfeitCancel({ pendingRedeem, pendingAddress, canceller, pkA, pkB, newPly, sigA, sigB }) {
  const k = core.wasm();
  const payAddr = canceller === 'A' ? addressForPubkey(toXOnly(pkA)) : addressForPubkey(toXOnly(pkB));
  return core.withRpc(async (rpc) => {
    const { entries } = await rpc.getUtxosByAddresses({ addresses: [pendingAddress] });
    if (!entries.length) throw new Error('no pending-forfeit UTXO to cancel');
    // One output per input (all to the canceller) — the CANCEL leg binds output[i] to input[i] too,
    // and re-verifies the newer co-signed checkpoint independently per input.
    const fee = SETTLE_V3_FEE_SOMPI_PER_INPUT * BigInt(entries.length);
    const outputs = entries.map((e) => ({ address: payAddr, amount: BigInt(e.amount) - SETTLE_V3_FEE_SOMPI_PER_INPUT }));
    const tx = k.createTransaction(entries, outputs, fee, undefined, 4 * entries.length);
    const ins = tx.inputs;
    const witness = [H(sigA), H(sigB), plyFixed(newPly), Buffer.alloc(0)];
    for (let i = 0; i < ins.length; i++) ins[i].signatureScript = p2shSig(pendingRedeem, witness);
    tx.inputs = ins;
    const resp = await rpc.submitTransaction({ transaction: tx, allowOrphan: false });
    return { txid: String(resp.transactionId ?? resp), paid: payAddr, inputs: entries.length };
  });
}

/* Reclaim (the innermost ELSE) is byte-identical to v1/v2: the depositor spends after the CLTV with
 * witness `<sig> OP_FALSE OP_FALSE`. Reuses the existing escrow.js reclaim path with the v3 redeemHex. */

export const _internals = { buildRedeemV3, buildPendingRedeem, checkpointTag, settleTag, outcomeMsg, numToBytes, plyFixed };
