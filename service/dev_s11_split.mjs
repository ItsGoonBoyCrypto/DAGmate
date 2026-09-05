// Offline (WASM-only, no RPC) proof of the byte-exact prefix/suffix split the S11 escrow leg relies
// on. If drain(PREFIX_tokens) ‖ 0x02 ‖ ply2 ‖ drain(SUFFIX_tokens) reproduces the full pending redeem
// byte-for-byte, then the escrow's on-chain reconstruction (PREFIX ‖ ply2 ‖ SUFFIX) yields exactly the
// pending covenant's real P2SH — which is the whole premise of the link. Run with the throwaway
// mnemonic (serialization only; derives nothing spendable):
//   DAGMATE_MASTER_MNEMONIC="abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about" node dev_s11_split.mjs
import * as core from './core.js';
import { numToBytes } from './scriptsim.mjs';

const k = core.wasm();
const NET = core.netType();

function drain(tokens) {
  const sb = new k.ScriptBuilder(core.COVENANT_OPTS);
  for (const t of tokens) { if (typeof t === 'string') sb.addOp(k.Opcodes[t]); else sb.addData(Uint8Array.from(t)); }
  return Buffer.from(sb.drain(), 'hex');
}
function p2shSpkHex(redeemHex) { return String(k.ScriptBuilder.fromScript(redeemHex, core.COVENANT_OPTS).createPayToScriptHashScript().script); }
function p2shAddr(redeemHex) { const spk = k.ScriptBuilder.fromScript(redeemHex, core.COVENANT_OPTS).createPayToScriptHashScript(); return k.addressFromScriptPublicKey(spk, NET).toString(); }

// fixed-width 2-byte LE ply (constant push framing 0x02 <b0><b1>)
const plyFixed = (n) => { const b = Buffer.alloc(2); b.writeUInt16LE(Number(n)); return b; };

// S10 pending covenant, FIXED-WIDTH claimedPly variant (only change vs the proven S10: the ply is a
// 2-byte field read with OpBin2Num instead of a minimal push). Baked: spkX, spkY, W, pkA, pkB,
// matchTag, maxFee — and claimedPly is the reconstructed field. Prefix/suffix are the token halves
// either side of that field.
function pendingFixedParts({ spkX, spkY, W, pkA, pkB, matchTag, maxFee }) {
  const prefix = [
    'OpIf',
      'OpTxInputIndex', 'OpTxInputDaaScore', numToBytes(W), 'OpAdd', 'OpCheckLockTimeVerify',
      'OpTxInputIndex', 'OpTxOutputSpk', Buffer.from(spkX), 'OpEqualVerify',
      'OpTxInputIndex', 'OpTxOutputAmount', numToBytes(maxFee), 'OpAdd', 'OpTxInputIndex', 'OpTxInputAmount', 'OpGreaterThanOrEqual',
    'OpElse',
      Buffer.from(matchTag), numToBytes(1), 'OpPick', 'OpCat', 'OpSHA256',
      numToBytes(2), 'OpPick', numToBytes(1), 'OpPick', Buffer.from(pkB), 'OpCheckSigFromStack', 'OpVerify',
      numToBytes(3), 'OpPick', numToBytes(1), 'OpPick', Buffer.from(pkA), 'OpCheckSigFromStack', 'OpVerify',
      'OpDrop', 'OpNip', 'OpNip',
      // -- claimedPly (fixed 2B) goes HERE --
  ];
  const suffix = [
      'OpBin2Num', 'OpGreaterThan', 'OpVerify',
      'OpTxInputIndex', 'OpTxOutputSpk', Buffer.from(spkY), 'OpEqualVerify',
      'OpTxInputIndex', 'OpTxOutputAmount', numToBytes(maxFee), 'OpAdd', 'OpTxInputIndex', 'OpTxInputAmount', 'OpGreaterThanOrEqual',
    'OpEndIf',
  ];
  return { prefix, suffix };
}

const consts = {
  spkX: Buffer.from('SPKX-claimant-outputscript-0000'),
  spkY: Buffer.from('SPKY-canceller-outputscript-000'),
  W: 30n, pkA: Buffer.alloc(32, 0xA1), pkB: Buffer.alloc(32, 0xB2),
  matchTag: Buffer.alloc(32, 0x5c), maxFee: 15_000_000n,
};
const { prefix, suffix } = pendingFixedParts(consts);

let allOk = true;
const check = (label, cond) => { if (!cond) allOk = false; console.log(`   ${cond ? 'ok ' : 'BAD'} ${label}`); };

for (const ply of [0n, 1n, 40n, 41n, 255n, 256n, 999n, 32767n]) {
  const p2 = plyFixed(ply);
  const full = drain([...prefix, p2, ...suffix]);
  // Reconstruct the way the escrow does on-chain: baked PREFIX (with the trailing 0x02 push-len) ‖ ply2 ‖ baked SUFFIX
  const PREFIX = Buffer.concat([drain(prefix), Buffer.from([0x02])]);
  const SUFFIX = drain(suffix);
  const recon = Buffer.concat([PREFIX, p2, SUFFIX]);
  const match = recon.equals(full);
  check(`ply=${ply}: reconstruction == real redeem (${full.length}B)`, match);
  if (match) {
    // and the P2SH derived from either is identical (sanity — same bytes, same hash)
    check(`ply=${ply}: P2SH addr agrees`, p2shAddr(full.toString('hex')) === p2shAddr(recon.toString('hex')));
  } else {
    console.log('       full :', full.toString('hex'));
    console.log('       recon:', recon.toString('hex'));
  }
}

// report the constant halves so the S11 spike can bake them, and confirm PREFIX really ends in 0x02
const PREFIX = Buffer.concat([drain(prefix), Buffer.from([0x02])]);
const SUFFIX = drain(suffix);
console.log(`\n   PREFIX ends in 0x02: ${PREFIX[PREFIX.length - 1] === 0x02}   |PREFIX|=${PREFIX.length}  |SUFFIX|=${SUFFIX.length}`);
console.log(allOk ? '\nSPLIT PROVEN — fixed-width ply reconstruction is byte-exact across the ply range.' : '\nSPLIT FAILED — do NOT spend dust; the reconstruction would pay a wrong address.');
process.exit(allOk ? 0 : 1);
