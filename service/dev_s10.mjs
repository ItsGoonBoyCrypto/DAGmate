// Offline development of the S10 PENDING-FORFEIT covenant (the optimistic challenge window).
// A forfeit claim (S8/S9) spends the escrow into THIS covenant instead of paying immediately.
// Baked: spkX (claimant payout), spkY (canceller payout), claimedPly, W (window, DAA), pkA, pkB,
//        matchTag, maxFee.
//   FINALIZE (X wins): witness [OP_TRUE].  Valid once DAA ≥ (this UTXO's creation DAA) + W; pays X.
//   CANCEL   (Y wins): witness [sigA' sigB' ply' OP_FALSE].  Y proves a NEWER co-signed checkpoint
//            (ply' > claimedPly) → the claim was stale/fraudulent → pays Y (catches the fraud).
import { run, sha256, numToBytes } from './scriptsim.mjs';

const matchTag = Buffer.from('MATCHTAG-32byte-padding-000000000'.slice(0, 32));
const pkA = Buffer.from('PKA-32byte-padding-00000000000000'.slice(0, 32));
const pkB = Buffer.from('PKB-32byte-padding-00000000000000'.slice(0, 32));
const spkX = Buffer.from('SPKX-claimant');
const spkY = Buffer.from('SPKY-canceller');
const sigA = Buffer.from('SIGA-64-00000000000000000000000000000000000000000000000000000000000'.slice(0, 64));
const sigB = Buffer.from('SIGB-64-00000000000000000000000000000000000000000000000000000000000'.slice(0, 64));

const CLAIMED_PLY = 40n, W = 3600n, MAXFEE = 15_000_000n, INPUT_AMT = 100_000_000n;
const PENDING_DAA = 530_000_000n; // the DAA at which the pending UTXO was created

// a newer co-signed checkpoint C' (ply' > claimedPly). hC' = SHA256(matchTag ‖ ply').
const newPly = 42n; const plyBytes = numToBytes(newPly);
const hCp = sha256(Buffer.concat([matchTag, plyBytes]));

const pending = [
  'OpIf',
    // ── FINALIZE: X wins after the window ──
    'OpTxInputIndex', 'OpTxInputDaaScore', numToBytes(W), 'OpAdd', 'OpCheckLockTimeVerify',
    'OpTxInputIndex', 'OpTxOutputSpk', spkX, 'OpEqualVerify',
    'OpTxInputIndex', 'OpTxOutputAmount', numToBytes(MAXFEE), 'OpAdd', 'OpTxInputIndex', 'OpTxInputAmount', 'OpGreaterThanOrEqual',
  'OpElse',
    // ── CANCEL: Y proves a newer co-signed checkpoint (ply' > claimedPly) → pay Y ──
    // stack after OpIf consumes OP_FALSE: [sigA' sigB' ply']
    matchTag, numToBytes(1), 'OpPick', 'OpCat', 'OpSHA256',        // hC' = SHA256(matchTag ‖ ply')
    numToBytes(2), 'OpPick', numToBytes(1), 'OpPick', pkB, 'OpCheckSigFromStack', 'OpVerify',
    numToBytes(3), 'OpPick', numToBytes(1), 'OpPick', pkA, 'OpCheckSigFromStack', 'OpVerify',
    'OpDrop', 'OpNip', 'OpNip',                                   // drop hC', sigB, sigA -> [ply']
    numToBytes(CLAIMED_PLY), 'OpGreaterThan', 'OpVerify',          // ply' > claimedPly
    'OpTxInputIndex', 'OpTxOutputSpk', spkY, 'OpEqualVerify',
    'OpTxInputIndex', 'OpTxOutputAmount', numToBytes(MAXFEE), 'OpAdd', 'OpTxInputIndex', 'OpTxInputAmount', 'OpGreaterThanOrEqual',
  'OpEndIf',
];

const ctxFinal = (lockTime, spk = spkX) => ({ lockTime, inputIndex: 0, inputs: [{ amount: INPUT_AMT, daa: PENDING_DAA }], outputs: [{ spk, amount: INPUT_AMT - 5_000_000n }] });
const ctxCancel = (spk = spkY) => ({ inputIndex: 0, inputs: [{ amount: INPUT_AMT, daa: PENDING_DAA }], outputs: [{ spk, amount: INPUT_AMT - 5_000_000n }],
  checksig: (sig, msg, pub) => msg.equals(hCp) && ((sig.equals(sigA) && pub.equals(pkA)) || (sig.equals(sigB) && pub.equals(pkB))) });

const show = (label, r, want) => console.log(`${r.ok === want ? 'ok  ' : 'BAD '} ${label} (ok:${r.ok}, want:${want})${r.error ? '  [' + r.error + ']' : ''}`);

// FINALIZE
show('finalize AFTER window', run(pending, [numToBytes(1)], ctxFinal(PENDING_DAA + W + 10n)), true);
show('finalize BEFORE window (rejected)', run(pending, [numToBytes(1)], ctxFinal(PENDING_DAA + W - 10n)), false);
show('finalize but pays Y not X (rejected)', run(pending, [numToBytes(1)], ctxFinal(PENDING_DAA + W + 10n, spkY)), false);
// CANCEL
show('cancel with a NEWER checkpoint (ply>claimed) → Y', run(pending, [sigA, sigB, plyBytes, Buffer.alloc(0)], ctxCancel()), true);
{ // stale: ply' == claimedPly (not newer)
  const stale = numToBytes(CLAIMED_PLY); const hStale = sha256(Buffer.concat([matchTag, stale]));
  const cx = { ...ctxCancel(), checksig: (sig, msg, pub) => msg.equals(hStale) && ((sig.equals(sigA) && pub.equals(pkA)) || (sig.equals(sigB) && pub.equals(pkB))) };
  show('cancel with a STALE checkpoint (ply==claimed, rejected)', run(pending, [sigA, sigB, stale, Buffer.alloc(0)], cx), false);
}
show('cancel with a forged co-signature (rejected)', run(pending, [sigA, Buffer.from('FORGED-64-0000000000000000000000000000000000000000000000000000000'.slice(0, 64)), plyBytes, Buffer.alloc(0)], ctxCancel()), false);
show('cancel but pays X not Y (rejected)', run(pending, [sigA, sigB, plyBytes, Buffer.alloc(0)], ctxCancel(spkX)), false);
