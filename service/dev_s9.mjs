// Offline development of the S9 forfeit leg: co-signed checkpoint C + a BOUNDED unilateral move M.
//   C = {matchTag, D_C(deadline for the C-mover X), turn(X: 0x01=A,0x02=B), B(opponent budget)}
//       co-signed by BOTH; hC = SHA256(matchTag ‖ D_C ‖ turn ‖ B).
//   M = {prevHash=hC, D_M(the opponent Y's new deadline)} signed by the MOVER X;
//       hM = SHA256(hC ‖ D_M).  X moved, so it's now Y's turn by D_M.
// Covenant enforces the SOUNDNESS BOUND: D_M ≤ D_C + B + INCREMENT (X can only give Y MORE time,
// never shorten it), then CLTV(D_M) and pays the claimant X (Y flagged).
import { run, sha256, numToBytes } from './scriptsim.mjs';

const matchTag = Buffer.from('MATCHTAG-32byte-padding-000000000'.slice(0, 32));
const pkA = Buffer.from('PKA-32byte-padding-00000000000000'.slice(0, 32));
const pkB = Buffer.from('PKB-32byte-padding-00000000000000'.slice(0, 32));
const spkA = Buffer.from('SPKA-outputscript');
const spkB = Buffer.from('SPKB-outputscript');
const sigCA = Buffer.from('SIGCA-64-000000000000000000000000000000000000000000000000000000000'.slice(0, 64));
const sigCB = Buffer.from('SIGCB-64-000000000000000000000000000000000000000000000000000000000'.slice(0, 64));
const sigM = Buffer.from('SIGM-64-0000000000000000000000000000000000000000000000000000000000'.slice(0, 64));

const INCREMENT = 20n;                 // baked Fischer increment (DAA)
// SOUNDNESS: D_M must be ≥ D_C + B + INCREMENT — the mover gives the opponent AT LEAST the max
// fair deadline (they moved by D_C, opponent then gets budget B + increment), never less. A LOWER
// bound, not upper: an upper bound would let a mover set a short deadline and steal the forfeit.
const D_C = 500_000_000n, B = 1000n, D_M = 500_000_000n + 1000n + 20n; // = the minimum allowed (D_C+B+INC)
const turnByte = 0x01;                 // A is the C-mover X → A is the claimant; B (Y) flagged
const dC = numToBytes(D_C), bb = numToBytes(B), dM = numToBytes(D_M), turn = Buffer.from([turnByte]);
const hC = sha256(Buffer.concat([matchTag, dC, turn, bb]));
const hM = sha256(Buffer.concat([hC, dM]));
// X = the turn player: sigM is X's (A here) over hM.
const pkX = turnByte === 0x01 ? pkA : pkB;

const MAXFEE = 15_000_000n, INPUT_AMT = 100_000_000n;
const ctx = {
  lockTime: D_M + 10n, inputIndex: 0, inputs: [{ amount: INPUT_AMT }],
  outputs: [{ spk: turnByte === 0x01 ? spkA : spkB, amount: INPUT_AMT - 5_000_000n }], // pays claimant X
  checksig: (sig, msg, pub) =>
    (msg.equals(hC) && ((sig.equals(sigCA) && pub.equals(pkA)) || (sig.equals(sigCB) && pub.equals(pkB)))) ||
    (msg.equals(hM) && sig.equals(sigM) && pub.equals(pkX)),
};

// Witness (bottom→top): B, D_C, turn, D_M, sigCA, sigCB, sigM
const witness = [bb, dC, turn, dM, sigCA, sigCB, sigM];

// witness depths from top: sigM0 sigCB1 sigCA2 D_M3 turn4 D_C5 B6
const leg = [
  // ── hC = SHA256(matchTag ‖ D_C ‖ turn ‖ B); after matchTag push, D_C=6 turn=5 B=7 ──
  matchTag,
  numToBytes(6), 'OpPick', 'OpCat',   // + D_C
  numToBytes(5), 'OpPick', 'OpCat',   // + turn
  numToBytes(7), 'OpPick', 'OpCat',   // + B
  'OpSHA256',                         // [B D_C turn D_M sigCA sigCB sigM hC]
  // ── verify the two C co-signatures over hC ──
  numToBytes(2), 'OpPick', numToBytes(1), 'OpPick', pkB, 'OpCheckSigFromStack', 'OpVerify', // sigCB/pkB
  numToBytes(3), 'OpPick', numToBytes(1), 'OpPick', pkA, 'OpCheckSigFromStack', 'OpVerify', // sigCA/pkA
  // ── hM = SHA256(hC ‖ D_M): copy D_M(depth4), cat onto hC, hash (consumes hC) ──
  numToBytes(4), 'OpPick', 'OpCat', 'OpSHA256',  // [B D_C turn D_M sigCA sigCB sigM hM]
  // ── verify M's mover signature with pkX = turn==0x02 ? pkB : pkA ──
  numToBytes(1), 'OpPick',            // copy sigM
  numToBytes(1), 'OpPick',            // copy hM
  numToBytes(7), 'OpPick', Buffer.from([0x02]), 'OpEqual', 'OpIf', pkB, 'OpElse', pkA, 'OpEndIf', // pkX
  'OpCheckSigFromStack', 'OpVerify',
  // ── drop hM, sigM, sigCB, sigCA -> [B D_C turn D_M] ──
  'OpDrop', 'OpDrop', 'OpDrop', 'OpDrop',
  // ── SOUNDNESS BOUND: D_M >= (D_C + B + INCREMENT) ──  [B D_C turn D_M], D_M=0 turn=1 D_C=2 B=3
  numToBytes(2), 'OpPick',            // copy D_C
  numToBytes(4), 'OpPick', 'OpAdd',   // + B (now depth4) -> D_C+B
  numToBytes(INCREMENT), 'OpAdd',     // + INCREMENT = bound  -> [.. D_M bound]
  numToBytes(1), 'OpPick', 'OpSwap',  // copy D_M then swap -> [.. bound(consumed order) D_M bound]→ arrange a=D_M b=bound
  'OpGreaterThanOrEqual', 'OpVerify', // D_M >= bound
  // ── CLTV(D_M): tx.lockTime >= D_M ──  [B D_C turn D_M]
  'OpCheckLockTimeVerify',            // pops D_M -> [B D_C turn]
  // ── clean the leftover B, D_C (keep turn) so the stack ends with one item ──
  'OpNip', 'OpNip',                   // -> [turn]
  // ── claimant = X (the turn player): pay spkA if turn==0x01 else spkB ──
  Buffer.from([0x01]), 'OpEqual', 'OpIf', spkA, 'OpElse', spkB, 'OpEndIf',
  'OpTxInputIndex', 'OpTxOutputSpk', 'OpEqualVerify',
  'OpTxInputIndex', 'OpTxOutputAmount', numToBytes(MAXFEE), 'OpAdd', 'OpTxInputIndex', 'OpTxInputAmount', 'OpGreaterThanOrEqual',
];

const r = run(leg, witness, ctx);
console.log('S9 HAPPY ok:', r.ok, '(want true)  error:', r.error);
if (!r.ok) { console.log('--- trace ---'); for (const t of r.trace) console.log(' ', t); }

function reject(label, w, c) {
  const rr = run(leg, w, c);
  console.log(`${rr.ok ? 'LEAK ' : 'rej  '} ${label}  (ok:${rr.ok}, ${rr.error || ''})`);
}
// THE soundness test: mover sets a SHORT D_M (< D_C+B+INC) to steal a forfeit → lower bound rejects.
{
  const shortDM = D_C - 500n; const dMs = numToBytes(shortDM);
  const hMs = sha256(Buffer.concat([hC, dMs]));
  const cs = { ...ctx, lockTime: shortDM + 10n, checksig: (sig, msg, pub) =>
    (msg.equals(hC) && ((sig.equals(sigCA) && pub.equals(pkA)) || (sig.equals(sigCB) && pub.equals(pkB)))) ||
    (msg.equals(hMs) && sig.equals(sigM) && pub.equals(pkX)) };
  reject('short deadline (steal attempt, D_M < D_C+B+INC)', [bb, dC, turn, dMs, sigCA, sigCB, sigM], cs);
}
reject('forged M signature', [bb, dC, turn, dM, sigCA, sigCB, Buffer.from('FORGED-64-00000000000000000000000000000000000000000000000000000000'.slice(0, 64))], ctx);
reject('forged C signature', [bb, dC, turn, dM, Buffer.from('FORGED-64-00000000000000000000000000000000000000000000000000000000'.slice(0, 64)), sigCB, sigM], ctx);
reject('deadline not reached (CLTV)', witness, { ...ctx, lockTime: D_M - 1n });
reject('paid the flagger not the claimant', witness, { ...ctx, outputs: [{ spk: spkB, amount: INPUT_AMT - 5_000_000n }] });
console.log('\n(S9 happy + soundness bound + attacks all correct in-sim → prove on dust)');
