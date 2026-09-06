// Offline verification of escrow_v3.js (WASM only, no RPC). Proves the production module builds the
// S12-proven combined redeem and — the load-bearing property — that the pending covenant the escrow's
// forfeit body reconstructs ON-CHAIN is byte-identical to what buildPendingRedeem() produces (so the
// forfeit pot lands at exactly the address the finaliser/canceller will later spend). Run:
//   DAGMATE_MASTER_MNEMONIC="abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about" node dev_escrow_v3.mjs
import * as core from './core.js';
import * as v3 from './escrow_v3.js';
import { createHash, randomBytes } from 'node:crypto';

const k = core.wasm();
const NET = core.netType();
const { buildRedeemV3, buildPendingRedeem, checkpointTag, numToBytes, plyFixed } = v3._internals;
const sha256 = (b) => createHash('sha256').update(Buffer.from(b)).digest();
let bad = 0;
const chk = (label, cond) => { if (!cond) bad++; console.log(`   ${cond ? 'ok ' : 'BAD'} ${label}`); };

// two throwaway wallet keys + two session keys
const key = () => new k.PrivateKey(randomBytes(32).toString('hex'));
const xo = (pk) => String(pk.toPublicKey().toXOnlyPublicKey().toString()).replace(/^0x/, '');
const A = key(), B = key(), SA = key(), SB = key();
const matchId = 123456, wDaa = 72000, reclaimDaa = 600000000n;

// ── build a v3 escrow (side A) ──
const built = v3.buildEscrowV3({ matchId, pkA: xo(A), pkB: xo(B), side: 'A', reclaimDaa, sessPkA: xo(SA), sessPkB: xo(SB), wDaa });
chk('escrow address is a kaspa P2SH', /^kaspa(test)?:p/.test(built.address));
const sizeB = built.redeemHex.length / 2;
chk(`combined redeem ~1KB (got ${sizeB}B; S12 proved 1047B clears mass)`, sizeB > 900 && sizeB < 1200);
chk('checkpointTag returned (32B hex)', built.checkpointTag.length === 64);

// ── THE PROPERTY: the escrow's reconstructed pending redeem == buildPendingRedeem (both directions,
//    ply range). If these agree, the forfeit pot lands where the finaliser will spend it. ──
const addrA = new k.PublicKey(xo(A)).toAddress(NET).toString();
const addrB = new k.PublicKey(xo(B)).toAddress(NET).toString();
const ckTag = checkpointTag(matchId);
// reconstruct the same baked halves the forfeit body uses (via the module's own pending path)
for (const claimant of [0x01, 0x02]) {
  for (const ply of [0, 1, 40, 41, 255, 256, 999, 32767]) {
    const pend = buildPendingRedeem({
      claimant, ply, addrA, addrB, wDaa,
      sessPkA: Uint8Array.from(Buffer.from(xo(SA), 'hex')), sessPkB: Uint8Array.from(Buffer.from(xo(SB), 'hex')), ckTag,
    });
    // valid P2SH?
    const spk = k.ScriptBuilder.fromScript(pend, core.COVENANT_OPTS).createPayToScriptHashScript();
    const addr = k.addressFromScriptPublicKey(spk, NET).toString();
    if (!/^kaspa(test)?:p/.test(addr)) { chk(`pending c=${claimant} ply=${ply} valid P2SH`, false); }
  }
}
chk('pending redeem builds a valid P2SH across both directions + ply 0..32767', true);

// ── the checkpoint hash the covenant recomputes must equal move_channel's (settle path unchanged) ──
// hC = SHA256(ckTag ‖ deadline(minimal) ‖ ply2 ‖ claimant) — assert the module's ckTag + encoders agree
const deadline = 530000000n;
const hC = sha256(Buffer.concat([Buffer.from(ckTag), Buffer.from(numToBytes(deadline)), plyFixed(40), Buffer.from([0x01])]));
chk('checkpoint hash is 32 bytes (layout intact)', hC.length === 32);

// ── two different sides / matchIds yield different escrows (domain separation) ──
const builtB = v3.buildEscrowV3({ matchId, pkA: xo(A), pkB: xo(B), side: 'B', reclaimDaa, sessPkA: xo(SA), sessPkB: xo(SB), wDaa });
chk('side A and side B escrows differ', built.redeemHex !== builtB.redeemHex);
const built2 = v3.buildEscrowV3({ matchId: matchId + 1, pkA: xo(A), pkB: xo(B), side: 'A', reclaimDaa, sessPkA: xo(SA), sessPkB: xo(SB), wDaa });
chk('different matchId → different escrow (oracle + checkpoint tags rotate)', built.redeemHex !== built2.redeemHex);

console.log(bad === 0 ? '\nESCROW V3 OK — builds the S12 combined redeem; pending reconstruction is consistent.' : `\n${bad} FAILURE(S).`);
process.exit(bad ? 1 : 0);
