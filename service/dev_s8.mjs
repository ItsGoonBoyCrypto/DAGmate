// Offline development of the S8 forfeit leg (co-signed checkpoint + CLTV + turn-based payout).
// Run: node dev_s8.mjs   — iterate the `leg` token list until it prints ok:true, then translate
// to ScriptBuilder for the on-chain spike. Crypto/introspection are mocked by scriptsim.
import { run, sha256, numToBytes } from './scriptsim.mjs';

// tagged test values (distinct bytes so mis-pairings are caught)
const matchTag = Buffer.from('MATCHTAG-32byte-padding-000000000'.slice(0, 32));
const pkA = Buffer.from('PKA-32byte-padding-00000000000000'.slice(0, 32));
const pkB = Buffer.from('PKB-32byte-padding-00000000000000'.slice(0, 32));
const spkA = Buffer.from('SPKA-outputscript');
const spkB = Buffer.from('SPKB-outputscript');
const sigA = Buffer.from('SIGA-64bytes-padding-0000000000000000000000000000000000000000000'.slice(0, 64));
const sigB = Buffer.from('SIGB-64bytes-padding-0000000000000000000000000000000000000000000'.slice(0, 64));

// checkpoint C = matchTag ‖ deadline(minimal script-number) ‖ turn(1).
// turn: 0x01 = A to move (A forfeits, B claims); 0x02 = B. Never 0x00 (a 1-byte 0x00 push is
// non-minimal and rejected). deadline is the MINIMAL number encoding so it doubles as a CLTV
// operand with no OpBin2Num (matches how v1 pushes reclaimDaa).
const DEADLINE = 500_000_000n;
const deadline = numToBytes(DEADLINE);
const TURN = 0x01; const turn = Buffer.from([TURN]);
const hC = sha256(Buffer.concat([matchTag, deadline, turn]));

// mock context: tx locktime PAST the deadline, single input, output 0 pays the claimant (B, since A forfeits).
const MAXFEE = 15_000_000n, INPUT_AMT = 100_000_000n;
const ctx = {
  lockTime: DEADLINE + 10n,
  inputIndex: 0,
  inputs: [{ amount: INPUT_AMT }],
  outputs: [{ spk: spkB, amount: INPUT_AMT - 5_000_000n }], // pays B (claimant), ≥ input-fee
  checksig: (sig, msg, pub) =>
    msg.equals(hC) && ((sig.equals(sigA) && pub.equals(pkA)) || (sig.equals(sigB) && pub.equals(pkB))),
};

// Witness (bottom→top): turn, deadline, sigA, sigB  (chosen so the multiply-used fields sit low)
const witness = [turn, deadline, sigA, sigB];

const maxFeeBytes = numToBytes(MAXFEE);
// The forfeit leg — copy-into-place strategy: OpPick copies (originals stay put) so the CheckSig
// triples are built on top without disturbing what's needed later.
const leg = [
  // ── rebuild hC = SHA256(matchTag ‖ deadline ‖ turn) using COPIES ──
  matchTag,                          // [turn dl sigA sigB matchTag]
  numToBytes(3), 'OpPick', 'OpCat',  // + copy deadline (depth3) -> matchTag‖deadline
  numToBytes(4), 'OpPick', 'OpCat',  // + copy turn (depth4)      -> ‖turn = C
  'OpSHA256',                        // [turn dl sigA sigB hC]
  // ── verify sigB over hC with pkB: build [sigB, hC, pkB] on top via copies ──
  numToBytes(1), 'OpPick',           // copy sigB (depth1): [.. sigB sigB? ] -> [turn dl sigA sigB hC sigB]
  numToBytes(1), 'OpPick',           // copy hC   (depth1): [.. sigB hC]
  pkB, 'OpCheckSigFromStack', 'OpVerify',   // consumes the 3 copies -> back to [turn dl sigA sigB hC]
  // ── verify sigA over hC with pkA ──
  numToBytes(2), 'OpPick',           // copy sigA (depth2): [.. hC sigA]
  numToBytes(1), 'OpPick',           // copy hC   (depth1): [.. sigA hC]
  pkA, 'OpCheckSigFromStack', 'OpVerify',   // -> back to [turn dl sigA sigB hC]
  // ── done with sigs/hC ──
  'OpDrop', 'OpDrop', 'OpDrop',      // drop hC, sigB, sigA -> [turn dl]
  // ── CLTV(deadline): tx.lockTime >= deadline (deadline is already a minimal number) ──
  'OpCheckLockTimeVerify',           // pops deadline -> [turn]
  // ── turn-based claimant: turn==0x02 (B forfeits) -> pay A; else (A forfeits) -> pay B ──
  Buffer.from([0x02]), 'OpEqual', 'OpIf', spkA, 'OpElse', spkB, 'OpEndIf',  // [spkClaimant]
  // ── output[inputIndex] pays the claimant ──
  'OpTxInputIndex', 'OpTxOutputSpk', 'OpEqualVerify',   // []
  // ── ...in full: output[i].amount + maxFee >= input[i].amount ──
  'OpTxInputIndex', 'OpTxOutputAmount', maxFeeBytes, 'OpAdd',
  'OpTxInputIndex', 'OpTxInputAmount', 'OpGreaterThanOrEqual',   // [bool]
];

const r = run(leg, witness, ctx);
console.log('HAPPY  ok:', r.ok, '(want true)  error:', r.error);

// ── adversarial: each MUST fail (ok:false) ──
const clone = (o) => JSON.parse(JSON.stringify(o, (k, v) => typeof v === 'bigint' ? v.toString() + 'n' : v));
function reject(label, w, c) {
  const rr = run(leg, w, c);
  console.log(`${rr.ok ? 'LEAK ' : 'rej  '} ${label}  (ok:${rr.ok}, ${rr.error || ''})`);
  return !rr.ok;
}
// deadline not passed yet
reject('deadline not reached (locktime < deadline)', witness, { ...ctx, lockTime: DEADLINE - 1n });
// forged sigB (mock rejects it)
reject('forged signature', [turn, deadline, sigA, Buffer.from('FORGED-64bytes-000000000000000000000000000000000000000000000000'.slice(0, 64))], ctx);
// wrong destination: output pays A but A is the forfeiter (turn=0x00 → claimant is B)
reject('pot paid to the forfeiter, not the claimant', witness, { ...ctx, outputs: [{ spk: spkA, amount: INPUT_AMT - 5_000_000n }] });
// skim: winner underpaid below input - maxFee
reject('claimant underpaid (skim)', witness, { ...ctx, outputs: [{ spk: spkB, amount: INPUT_AMT - MAXFEE - 10_000_000n }] });
// turn tampered: claimant tries to flip turn to 0x01 (claim B forfeited) but then must pay A — and
// the co-signed hash no longer matches, so the sig check fails.
const turnFlipped = Buffer.from([0x02]);
reject('turn flipped in the witness (breaks the co-signed hash)', [turnFlipped, deadline, sigA, sigB], ctx);
console.log('\n(happy passes + every attack rejected in-sim → translate to ScriptBuilder and prove on dust)');
