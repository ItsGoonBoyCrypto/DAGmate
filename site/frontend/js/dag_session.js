/**
 * DAGmate — browser side of the move channel (roadmap #3a). Mints the per-match SESSION keypair and
 * signs/verifies the co-signed checkpoints the v3 forfeit covenant reads. The PRIVATE key never leaves
 * the browser; only its x-only pubkey is sent to the server (baked into the covenant as a checkpoint
 * signer). Exposed as `window.DAGSession` for the classic app.js script.
 *
 * ⚠️ The byte layout below MUST stay identical to service/move_channel.mjs (the Node twin the sidecar
 * uses) — the covenant recomputes SHA256(matchTag ‖ deadline ‖ ply2 ‖ claimant) on-chain and a drifted
 * layout means a signature it can't verify. A Node byte-match test guards this (tools/test_dag_session.mjs).
 * BIP340 via vendored @noble/secp256k1 v1.7.1 (interoperable with the sidecar's @noble/curves, proven).
 */
// Node (test) has no `self`; the browser does. The shim import MUST come first — ES imports evaluate
// in source order, so this sets `self` before the vendored lib's module body reads `self.crypto`.
import './session_shim.js';
import * as secp from './vendor/noble-secp256k1.js';

const enc = (u8) => Array.from(u8, (b) => b.toString(16).padStart(2, '0')).join('');
const dec = (hex) => { const s = String(hex).replace(/^0x/, ''); const o = new Uint8Array(s.length / 2); for (let i = 0; i < o.length; i++) o[i] = parseInt(s.substr(i * 2, 2), 16); return o; };
const cat = (...ps) => { const n = ps.reduce((s, p) => s + p.length, 0); const o = new Uint8Array(n); let i = 0; for (const p of ps) { o.set(p, i); i += p.length; } return o; };
async function sha256(u8) { return new Uint8Array(await crypto.subtle.digest('SHA-256', u8)); }

export const CLAIMANT = { A: 0x01, B: 0x02 };

// minimal little-endian script-number (matches scriptsim.numToBytes / move_channel.numToBytes)
export function numToBytes(n) {
  n = BigInt(n);
  if (n === 0n) return new Uint8Array(0);
  const neg = n < 0n; let v = neg ? -n : n; const out = [];
  while (v > 0n) { out.push(Number(v & 0xffn)); v >>= 8n; }
  if (out[out.length - 1] & 0x80) out.push(neg ? 0x80 : 0x00);
  else if (neg) out[out.length - 1] |= 0x80;
  return Uint8Array.from(out);
}
export function plyField(ply) {
  const n = Number(ply);
  if (!Number.isInteger(n) || n < 0 || n > 0xffff) throw new Error(`ply out of 2-byte range: ${ply}`);
  return Uint8Array.from([n & 0xff, (n >> 8) & 0xff]);
}
function claimantByte(c) {
  const v = typeof c === 'string' ? CLAIMANT[c.toUpperCase()] : Number(c);
  if (v !== CLAIMANT.A && v !== CLAIMANT.B) throw new Error(`claimant must be A(1)/B(2)`);
  return Uint8Array.from([v]);
}

// ── Checkpoint C ──
export function checkpointPreimage({ matchTag, deadlineDaa, ply, claimant }) {
  const mt = dec(matchTag);
  if (mt.length !== 32) throw new Error(`matchTag must be 32 bytes`);
  return cat(mt, numToBytes(deadlineDaa), plyField(ply), claimantByte(claimant));
}
export async function hashCheckpoint(cp) { return sha256(checkpointPreimage(cp)); }
export async function signCheckpoint(cp, privHex) { return enc(await secp.schnorr.sign(await hashCheckpoint(cp), dec(privHex))); }
export async function verifyCheckpoint(cp, sigHex, xonlyHex) { try { return await secp.schnorr.verify(dec(sigHex), await hashCheckpoint(cp), dec(xonlyHex)); } catch { return false; } }

// ── Move M (extends a co-signed C) ──
export function movePreimage({ hC, nextDeadlineDaa }) {
  const h = dec(hC);
  if (h.length !== 32) throw new Error(`hC must be 32 bytes`);
  return cat(h, numToBytes(nextDeadlineDaa));
}
export async function hashMove(m) { return sha256(movePreimage(m)); }
export async function signMove(m, moverPrivHex) { return enc(await secp.schnorr.sign(await hashMove(m), dec(moverPrivHex))); }
export async function verifyMove(m, sigHex, moverXonly) { try { return await secp.schnorr.verify(dec(sigHex), await hashMove(m), dec(moverXonly)); } catch { return false; } }

// ── session keypair ──
export async function mint() {
  const priv = secp.utils.randomPrivateKey();
  const pub = await secp.schnorr.getPublicKey(priv);   // 32-byte x-only
  return { privHex: enc(priv), xonlyHex: enc(pub) };
}

// ── per-match private-key storage (browser only; never sent anywhere) ──
// The creator mints before a match exists, so their key is stashed by CHALLENGE id and resolved by
// the match's challengeId later; the accepter stashes by MATCH id directly.
const CH_KEY = (id) => `dagmate_sess_ch_${id}`;
const M_KEY = (id) => `dagmate_sess_m_${id}`;
export function stashForChallenge(challengeId, privHex) { try { localStorage.setItem(CH_KEY(challengeId), privHex); } catch {} }
export function stashForMatch(matchId, privHex) { try { localStorage.setItem(M_KEY(matchId), privHex); } catch {} }
export function privForMatch(matchId, challengeId) {
  try { return localStorage.getItem(M_KEY(matchId)) || (challengeId ? localStorage.getItem(CH_KEY(challengeId)) : null); }
  catch { return null; }
}

if (typeof window !== 'undefined') {
  window.DAGSession = {
    CLAIMANT, mint, hashCheckpoint, signCheckpoint, verifyCheckpoint, hashMove, signMove, verifyMove,
    stashForChallenge, stashForMatch, privForMatch, numToBytes, plyField,
  };
}
