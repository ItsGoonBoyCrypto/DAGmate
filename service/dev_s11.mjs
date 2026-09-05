// Offline development of the S11 LINK leg: the escrow forfeit spend OUTPUTS into the S10
// pending-forfeit covenant instead of paying the claimant directly. This is what makes the forfeit
// TRUSTLESS end to end — the pot lands in the challenge-window covenant, so the flagged opponent can
// still CANCEL with a newer co-signed state (S10) before the claimant finalises.
//
// The escrow reconstructs the pending covenant's P2SH scriptPubKey ON-CHAIN:
//   pendingRedeem = PENDING_PREFIX ‖ ply2 ‖ PENDING_SUFFIX      (prefix/suffix baked; ply2 = witness)
//   h   = Blake2b(pendingRedeem)
//   spk = 0x0000 ‖ aa 20 ‖ h ‖ 87                                (P2SH spk, 2-byte BE version)
//   require output[inputIndex].spk == spk   AND   output.amount + maxFee >= input.amount
//
// claimedPly is a FIXED-WIDTH 2-byte field so the push framing is constant (baked into PREFIX as the
// trailing 0x02 length byte) — no length arithmetic. It is committed in the co-signed checkpoint
// hC = SHA256(matchTag ‖ deadline ‖ ply2), so the claimant cannot inflate the ply to lock the
// opponent out of the cancel branch: a tampered ply2 breaks the co-signature.
//
// Crypto/introspection are mocked by scriptsim; OpBlake2b is a domain-separated stand-in here and is
// proven for real by spikes_forfeit.mjs S11 on dust. Run: node dev_s11.mjs
import { run, sha256, numToBytes, blake2bSim } from './scriptsim.mjs';

// ── tagged constants (distinct bytes so any mis-pairing shows up) ──
const matchTag = Buffer.from('MATCHTAG-32byte-padding-000000000'.slice(0, 32));
const pkA = Buffer.from('PKA-32byte-padding-00000000000000'.slice(0, 32));
const pkB = Buffer.from('PKB-32byte-padding-00000000000000'.slice(0, 32));
const sigA = Buffer.from('SIGA-64bytes-padding-0000000000000000000000000000000000000000000'.slice(0, 64));
const sigB = Buffer.from('SIGB-64bytes-padding-0000000000000000000000000000000000000000000'.slice(0, 64));

// Opaque stand-ins for the real drained bytes of the pending covenant either side of the ply push.
// PENDING_PREFIX ends with the 0x02 push-length byte (the fixed ply2 field width). In the on-chain
// spike these are the REAL ScriptBuilder.drain() halves and this exactness is asserted there.
const PENDING_PREFIX = Buffer.from('PENDING-REDEEM-PREFIX-BYTES..\x02', 'binary');
const PENDING_SUFFIX = Buffer.from('..PENDING-REDEEM-SUFFIX-BYTES', 'binary');
const VER_AA20 = Buffer.from([0x00, 0x00, 0xaa, 0x20]); // P2SH spk head: version(2B BE) ‖ OP_BLAKE2B-push(aa) ‖ len(20=32)
const TAIL = Buffer.from([0x87]);                       // ‖ OP_EQUAL

const DEADLINE = 500_000_000n, deadline = numToBytes(DEADLINE);
const MAXFEE = 15_000_000n, INPUT_AMT = 100_000_000n;

// fixed-width 2-byte little-endian ply (matches the pending covenant's OpBin2Num read)
const plyFixed = (n) => { const b = Buffer.alloc(2); b.writeUInt16LE(Number(n)); return b; };
const CLAIMED_PLY = 40n, ply2 = plyFixed(CLAIMED_PLY);

// co-signed checkpoint over the fixed-width ply
const hcFor = (dl, p2) => sha256(Buffer.concat([matchTag, dl, p2]));
const hC = hcFor(deadline, ply2);

// The expected pending P2SH spk the honest spend must pay into.
const pendingSpkFor = (p2) => {
  const redeem = Buffer.concat([PENDING_PREFIX, p2, PENDING_SUFFIX]);
  return Buffer.concat([VER_AA20, blake2bSim(redeem), TAIL]);
};
const honestPendingSpk = pendingSpkFor(ply2);

// Witness (bottom→top): deadline, ply2, sigA, sigB
const witness = [deadline, ply2, sigA, sigB];

const leg = [
  // ── rebuild hC = SHA256(matchTag ‖ deadline ‖ ply2) from copies ──
  matchTag,                          // [dl ply2 sigA sigB matchTag]
  numToBytes(4), 'OpPick', 'OpCat',  // + copy deadline (depth4) -> matchTag‖dl
  numToBytes(3), 'OpPick', 'OpCat',  // + copy ply2 (depth3)     -> ‖ply2  = preimage
  'OpSHA256',                        // [dl ply2 sigA sigB hC]
  // ── verify sigB over hC with pkB (build [sigB hC pkB] on top via copies) ──
  numToBytes(1), 'OpPick', numToBytes(1), 'OpPick', pkB, 'OpCheckSigFromStack', 'OpVerify',
  // ── verify sigA over hC with pkA ──
  numToBytes(2), 'OpPick', numToBytes(1), 'OpPick', pkA, 'OpCheckSigFromStack', 'OpVerify',
  // ── done with sigs/hC ──
  'OpDrop', 'OpDrop', 'OpDrop',      // drop hC, sigB, sigA -> [dl ply2]
  // ── CLTV(deadline): tx.lockTime >= deadline ──
  'OpSwap', 'OpCheckLockTimeVerify', // [ply2 dl] -> pops dl -> [ply2]
  // ── reconstruct pendingRedeem = PREFIX ‖ ply2 ‖ SUFFIX ──
  PENDING_PREFIX, 'OpSwap', 'OpCat', // [PREFIX‖ply2]
  PENDING_SUFFIX, 'OpCat',           // [pendingRedeem]
  'OpBlake2b',                       // [h]
  // ── assemble P2SH spk = VER_AA20 ‖ h ‖ TAIL ──
  VER_AA20, 'OpSwap', 'OpCat',       // [VER_AA20‖h]
  TAIL, 'OpCat',                     // [spk]
  // ── output[inputIndex].spk must equal the reconstructed pending spk ──
  'OpTxInputIndex', 'OpTxOutputSpk', 'OpEqualVerify',
  // ── ...and the pot carries over: output.amount + maxFee >= input.amount ──
  'OpTxInputIndex', 'OpTxOutputAmount', numToBytes(MAXFEE), 'OpAdd',
  'OpTxInputIndex', 'OpTxInputAmount', 'OpGreaterThanOrEqual',
];

// checksig mock: true only for a genuine (sigA,pkA)/(sigB,pkB) over the message that was co-signed.
const mkCtx = ({ lockTime = DEADLINE + 10n, outSpk = honestPendingSpk, outAmt = INPUT_AMT - 5_000_000n, signedPly = ply2 } = {}) => ({
  lockTime, inputIndex: 0,
  inputs: [{ amount: INPUT_AMT }],
  outputs: [{ spk: outSpk, amount: outAmt }],
  checksig: (sig, msg, pub) => msg.equals(hcFor(deadline, signedPly)) && ((sig.equals(sigA) && pub.equals(pkA)) || (sig.equals(sigB) && pub.equals(pkB))),
});

const r = run(leg, witness, mkCtx());
console.log('HAPPY  ok:', r.ok, '(want true)  error:', r.error);
if (!r.ok) { console.log('  trace tail:', r.trace.slice(-12).join('\n              ')); }

function reject(label, w, ctx) {
  const rr = run(leg, w, ctx);
  console.log(`${rr.ok ? 'LEAK ' : 'rej  '} ${label}  (ok:${rr.ok}, ${rr.error || ''})`);
}
// 1) deadline not reached
reject('deadline not reached (locktime < deadline)', witness, mkCtx({ lockTime: DEADLINE - 1n }));
// 2) forged co-signature
reject('forged sigB', [deadline, ply2, sigA, Buffer.from('FORGED-64bytes-000000000000000000000000000000000000000000000000'.slice(0, 64))], mkCtx());
// 3) output pays a DIFFERENT script (e.g. straight to the claimant, skipping the challenge window)
reject('output skips the pending covenant', witness, mkCtx({ outSpk: Buffer.from('SOME-OTHER-SPK-not-the-pending-covenant') }));
// 4) inflate claimedPly to lock the opponent out — but the co-signature is over ply=40, not 999
reject('inflated ply2 (breaks the co-signed hash)', [deadline, plyFixed(999n), sigA, sigB], mkCtx({ signedPly: ply2 }));
// 5) claimant signs a NEW hash for ply=999 but then the reconstructed spk must still match output.
//    Attacker also swaps the output to the ply=999 pending covenant → the cosign check passes, so this
//    is the SAME as an honest claim at ply 999 (legitimate if both really co-signed ply 999). Confirm
//    the mechanism is internally consistent (should ACCEPT — ply is whatever both players signed).
{
  const p999 = plyFixed(999n);
  const rr = run(leg, [deadline, p999, sigA, sigB], mkCtx({ signedPly: p999, outSpk: pendingSpkFor(p999) }));
  console.log(`${rr.ok ? 'ok  ' : 'BAD '} co-signed ply=999 pays the ply=999 pending covenant (want accept: ${rr.ok})`);
}
// 6) skim: output underpaid below input - maxFee
reject('claimant skims (output < input - maxFee)', witness, mkCtx({ outAmt: INPUT_AMT - MAXFEE - 10_000_000n }));
// 7) right ply co-signed, but output pays the pending covenant for a DIFFERENT ply (spk mismatch)
reject('output pays a mismatched-ply pending covenant', witness, mkCtx({ outSpk: pendingSpkFor(plyFixed(41n)) }));

console.log('\n(happy passes + every attack rejected in-sim -> translate to ScriptBuilder and prove OpBlake2b + the real prefix/suffix split on dust)');
