/**
 * DAGmate — off-chain signed-move channel (roadmap #3a). The trustless forfeit legs (S8–S11b, proven
 * on mainnet) are fed by two off-chain messages both clients exchange as they play:
 *
 *   Checkpoint C = { matchTag, deadlineDaa, ply, claimant }, co-signed by BOTH players (2-of-2).
 *     hC = SHA256( matchTag(32) ‖ deadlineDaa(minimal LE) ‖ ply(2B LE) ‖ claimant(1B) )
 *     "At this ply the clock is due at deadlineDaa; if it lapses, `claimant` may forfeit-claim."
 *   Move       M = { hC, nextDeadlineDaa }, signed by the MOVER only (extends an agreed C by one move).
 *     hM = SHA256( hC(32) ‖ nextDeadlineDaa(minimal LE) )
 *
 * WHY SESSION KEYS, not the player's wallet: OpCheckSigFromStack verifies a raw BIP340 Schnorr sig over
 * a 32-byte message; browser wallets (Kasware/Kastle/Kaspire) only expose signMessage, which hashes
 * with its own personal-message prefix — incompatible. So each client mints a per-match SESSION keypair
 * and its x-only pubkey is what gets baked into the escrow covenant as pkA/pkB. The main wallet only
 * ever signs the on-chain deposit + claim txs. Verified: noble's BIP340 verify accepts SDK
 * signScriptHash output (the scheme the covenant enforces) and vice-versa, so a session-key signature
 * made here settles on-chain unchanged.
 *
 * This module is PORTABLE (Node now, browser bundle later): crypto is @noble (BIP340 + sha256), no SDK.
 * The byte layout is the one proven on mainnet by spikes_forfeit.mjs — DO NOT change it without a new
 * on-chain proof, or witness hashes will stop matching what the covenant recomputes.
 */
import { schnorr } from '@noble/curves/secp256k1';
import { sha256 } from '@noble/hashes/sha256';

// Coerce a byte-ish input to Uint8Array: a Uint8Array passes through; a string is HEX (0x optional).
const hb = (x) => (x instanceof Uint8Array ? x : Uint8Array.from(Buffer.from(String(x).replace(/^0x/, ''), 'hex')));
const hex = (u8) => Buffer.from(u8).toString('hex');
const cat = (...parts) => { const t = parts.reduce((n, p) => n + p.length, 0); const o = new Uint8Array(t); let i = 0; for (const p of parts) { o.set(p, i); i += p.length; } return o; };

export const CLAIMANT = { A: 0x01, B: 0x02 };

/** Minimal little-endian script-number encoding (matches rusty-kaspa / numToBytes). */
export function numToBytes(n) {
  n = BigInt(n);
  if (n === 0n) return new Uint8Array(0);
  const neg = n < 0n; let v = neg ? -n : n; const out = [];
  while (v > 0n) { out.push(Number(v & 0xffn)); v >>= 8n; }
  if (out[out.length - 1] & 0x80) out.push(neg ? 0x80 : 0x00);
  else if (neg) out[out.length - 1] |= 0x80;
  return Uint8Array.from(out);
}

/** Fixed-width 2-byte little-endian ply (constant push framing; the covenant reads it via OpBin2Num). */
export function plyField(ply) {
  const n = Number(ply);
  if (!Number.isInteger(n) || n < 0 || n > 0xffff) throw new Error(`ply out of 2-byte range: ${ply}`);
  return Uint8Array.from([n & 0xff, (n >> 8) & 0xff]);
}

function claimantByte(claimant) {
  const c = typeof claimant === 'string' ? CLAIMANT[claimant.toUpperCase()] : Number(claimant);
  if (c !== CLAIMANT.A && c !== CLAIMANT.B) throw new Error(`claimant must be A(1)/B(2), got ${claimant}`);
  return Uint8Array.from([c]);
}

// ── Checkpoint C ──
export function checkpointPreimage({ matchTag, deadlineDaa, ply, claimant }) {
  const mt = hb(matchTag);
  if (mt.length !== 32) throw new Error(`matchTag must be 32 bytes, got ${mt.length}`);
  return cat(mt, numToBytes(deadlineDaa), plyField(ply), claimantByte(claimant));
}
export function hashCheckpoint(cp) { return sha256(checkpointPreimage(cp)); }
export function signCheckpoint(cp, privHex) { return schnorr.sign(hashCheckpoint(cp), hb(privHex)); }
export function verifyCheckpoint(cp, sig, xonlyPub) { try { return schnorr.verify(hb(sig), hashCheckpoint(cp), hb(xonlyPub)); } catch { return false; } }

// ── Move M (extends a co-signed C by one move) ──
export function movePreimage({ hC, nextDeadlineDaa }) {
  const h = hb(hC);
  if (h.length !== 32) throw new Error(`hC must be 32 bytes, got ${h.length}`);
  return cat(h, numToBytes(nextDeadlineDaa));
}
export function hashMove(m) { return sha256(movePreimage(m)); }
export function signMove(m, moverPrivHex) { return schnorr.sign(hashMove(m), hb(moverPrivHex)); }
export function verifyMove(m, sig, moverXonly) { try { return schnorr.verify(hb(sig), hashMove(m), hb(moverXonly)); } catch { return false; } }

/** Mint a per-match session keypair (private stays in the client; xonly is baked into the covenant). */
export function newSessionKey() {
  const priv = schnorr.utils.randomPrivateKey();
  return { privHex: hex(priv), xonlyHex: hex(schnorr.getPublicKey(priv)) };
}

export const _hex = hex;
