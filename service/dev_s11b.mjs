// Offline development of the S11b TWO-DIRECTION forfeit link. The S11 leg baked ONE claimant; a real
// match needs either player to be able to claim the opponent's timeout. We add a co-signed `claimant`
// byte (0x01 = A claims / A finalises, 0x02 = B claims) that selects which pending covenant to
// reconstruct: PA (finaliser A, canceller B) or PB (finaliser B, canceller A). Because `claimant` is
// folded into the co-signed checkpoint hC = SHA256(matchTag ‖ deadline ‖ ply2 ‖ claimant), a claimant
// cannot flip the direction without both players' signatures over the new byte.
//
// Crypto/introspection mocked by scriptsim; OpBlake2b is a stand-in and proven on dust by S11b.
// Run: node dev_s11b.mjs
import { run, sha256, numToBytes, blake2bSim } from './scriptsim.mjs';

const matchTag = Buffer.from('MATCHTAG-32byte-padding-000000000'.slice(0, 32));
const pkA = Buffer.from('PKA-32byte-padding-00000000000000'.slice(0, 32));
const pkB = Buffer.from('PKB-32byte-padding-00000000000000'.slice(0, 32));
const sigA = Buffer.from('SIGA-64bytes-padding-0000000000000000000000000000000000000000000'.slice(0, 64));
const sigB = Buffer.from('SIGB-64bytes-padding-0000000000000000000000000000000000000000000'.slice(0, 64));

// distinct opaque halves for the two pending covenants (real drained bytes on-chain). PREFIX carries
// the trailing 0x02 ply push-len.
const PREFIX_A = Buffer.from('PENDING-A-PREFIX...........\x02', 'binary');
const SUFFIX_A = Buffer.from('..PENDING-A-SUFFIX', 'binary');
const PREFIX_B = Buffer.from('PENDING-B-PREFIX-different-len...\x02', 'binary');
const SUFFIX_B = Buffer.from('..PENDING-B-SUFFIX-differs', 'binary');
const VER_AA20 = Buffer.from([0x00, 0x00, 0xaa, 0x20]);
const TAIL = Buffer.from([0x87]);

const DEADLINE = 500_000_000n, deadline = numToBytes(DEADLINE);
const MAXFEE = 15_000_000n, INPUT_AMT = 100_000_000n;
const plyFixed = (n) => { const b = Buffer.alloc(2); b.writeUInt16LE(Number(n)); return b; };
const CLAIMED_PLY = 40n, ply2 = plyFixed(CLAIMED_PLY);

const hcFor = (dl, p2, claimant) => sha256(Buffer.concat([matchTag, dl, p2, Buffer.from([claimant])]));

const pendingSpkFor = (claimant, p2) => {
  const [PRE, SUF] = claimant === 0x01 ? [PREFIX_A, SUFFIX_A] : [PREFIX_B, SUFFIX_B];
  return Buffer.concat([VER_AA20, blake2bSim(Buffer.concat([PRE, p2, SUF])), TAIL]);
};

// Witness (bottom→top): deadline, ply2, claimant, sigA, sigB
const leg = [
  // ── rebuild hC = SHA256(matchTag ‖ deadline ‖ ply2 ‖ claimant) from copies ──
  matchTag,                          // [dl ply2 cl sigA sigB matchTag]
  numToBytes(5), 'OpPick', 'OpCat',  // + copy deadline (depth5)
  numToBytes(4), 'OpPick', 'OpCat',  // + copy ply2 (depth4)
  numToBytes(3), 'OpPick', 'OpCat',  // + copy claimant (depth3) = preimage
  'OpSHA256',                        // [dl ply2 cl sigA sigB hC]
  // ── verify sigB over hC with pkB ──
  numToBytes(1), 'OpPick', numToBytes(1), 'OpPick', pkB, 'OpCheckSigFromStack', 'OpVerify',
  // ── verify sigA over hC with pkA ──
  numToBytes(2), 'OpPick', numToBytes(1), 'OpPick', pkA, 'OpCheckSigFromStack', 'OpVerify',
  'OpDrop', 'OpDrop', 'OpDrop',      // drop hC, sigB, sigA -> [dl ply2 cl]
  // ── CLTV(deadline): deadline is at depth2, roll it up ──
  numToBytes(2), 'OpRoll', 'OpCheckLockTimeVerify',   // [ply2 cl]
  // ── select SUFFIX by claimant (keep a copy of claimant for the prefix select) ──
  'OpDup', Buffer.from([0x01]), 'OpEqual', 'OpIf', SUFFIX_A, 'OpElse', SUFFIX_B, 'OpEndIf',  // [ply2 cl suffix]
  // ── select PREFIX by claimant ──
  'OpSwap', Buffer.from([0x01]), 'OpEqual', 'OpIf', PREFIX_A, 'OpElse', PREFIX_B, 'OpEndIf', // [ply2 suffix prefix]
  // ── reconstruct prefix ‖ ply2 ‖ suffix ──
  numToBytes(2), 'OpRoll',           // [suffix prefix ply2]
  'OpCat',                           // [suffix prefix‖ply2]
  'OpSwap', 'OpCat',                 // [prefix‖ply2‖suffix]
  'OpBlake2b',                       // [h]
  // ── assemble P2SH spk = VER_AA20 ‖ h ‖ TAIL ──
  VER_AA20, 'OpSwap', 'OpCat', TAIL, 'OpCat',   // [spk]
  'OpTxInputIndex', 'OpTxOutputSpk', 'OpEqualVerify',
  'OpTxInputIndex', 'OpTxOutputAmount', numToBytes(MAXFEE), 'OpAdd',
  'OpTxInputIndex', 'OpTxInputAmount', 'OpGreaterThanOrEqual',
];

const mkCtx = ({ lockTime = DEADLINE + 10n, outSpk, outAmt = INPUT_AMT - 5_000_000n, signedClaimant, signedPly = ply2 } = {}) => ({
  lockTime, inputIndex: 0,
  inputs: [{ amount: INPUT_AMT }],
  outputs: [{ spk: outSpk, amount: outAmt }],
  checksig: (sig, msg, pub) => msg.equals(hcFor(deadline, signedPly, signedClaimant)) && ((sig.equals(sigA) && pub.equals(pkA)) || (sig.equals(sigB) && pub.equals(pkB))),
});

const wit = (claimant) => [deadline, ply2, Buffer.from([claimant]), sigA, sigB];

// ── happy: both directions land at the right pending covenant ──
const rA = run(leg, wit(0x01), mkCtx({ signedClaimant: 0x01, outSpk: pendingSpkFor(0x01, ply2) }));
console.log('DIR A (claimant=1) lands at PA:', rA.ok, '(want true)', rA.error || '');
const rB = run(leg, wit(0x02), mkCtx({ signedClaimant: 0x02, outSpk: pendingSpkFor(0x02, ply2) }));
console.log('DIR B (claimant=2) lands at PB:', rB.ok, '(want true)', rB.error || '');
if (!rA.ok) console.log('  A trace tail:', rA.trace.slice(-10).join('\n               '));
if (!rB.ok) console.log('  B trace tail:', rB.trace.slice(-10).join('\n               '));

function reject(label, w, ctx) { const rr = run(leg, w, ctx); console.log(`${rr.ok ? 'LEAK ' : 'rej  '} ${label}  (ok:${rr.ok}, ${rr.error || ''})`); }
// cross-direction: co-signed for A, but output pays PB (claimant tries to redirect to the other covenant)
reject('claimant=1 but output pays PB', wit(0x01), mkCtx({ signedClaimant: 0x01, outSpk: pendingSpkFor(0x02, ply2) }));
// claimant byte not co-signed (attacker flips 1→2 in witness; sig was over 1)
reject('flipped claimant (2 in witness, co-signed 1)', wit(0x02), mkCtx({ signedClaimant: 0x01, outSpk: pendingSpkFor(0x02, ply2) }));
// deadline not reached
reject('deadline not reached', wit(0x01), mkCtx({ signedClaimant: 0x01, outSpk: pendingSpkFor(0x01, ply2), lockTime: DEADLINE - 1n }));
// forged co-sig
reject('forged sigB', [deadline, ply2, Buffer.from([0x01]), sigA, Buffer.from('FORGED-64bytes-000000000000000000000000000000000000000000000000'.slice(0, 64))], mkCtx({ signedClaimant: 0x01, outSpk: pendingSpkFor(0x01, ply2) }));
// skim
reject('claimant skims', wit(0x01), mkCtx({ signedClaimant: 0x01, outSpk: pendingSpkFor(0x01, ply2), outAmt: INPUT_AMT - MAXFEE - 10_000_000n }));

console.log('\n(both directions land + every attack rejected in-sim -> prove both on dust via spikes_forfeit.mjs S11b)');
