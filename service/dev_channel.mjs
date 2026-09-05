// Offline test of move_channel.mjs. The load-bearing property: the module's checkpoint/move hashes
// must be byte-identical to what the on-chain-proven spike code (spikes_forfeit.mjs S11b) computes —
// otherwise a session-key signature made in the browser won't verify inside the covenant. We recompute
// the hash here the EXACT way the spike does (scriptsim numToBytes + 2-byte LE ply) and assert equality,
// then exercise sign/verify/tamper. Run: node dev_channel.mjs
import { createHash } from 'node:crypto';
import { numToBytes as simNum } from './scriptsim.mjs';
import * as mc from './move_channel.mjs';

const sha256 = (u8) => createHash('sha256').update(Buffer.from(u8)).digest();
let bad = 0;
const eq = (label, got, want) => { const ok = Buffer.from(got).equals(Buffer.from(want)); if (!ok) bad++; console.log(`   ${ok ? 'ok ' : 'BAD'} ${label}`); };
const is = (label, cond) => { if (!cond) bad++; console.log(`   ${cond ? 'ok ' : 'BAD'} ${label}`); };

const matchTag = Buffer.alloc(32, 0x5c);
const plySpike = (n) => { const b = Buffer.alloc(2); b.writeUInt16LE(Number(n)); return b; };

// ── (1) checkpoint hash agrees with the spike's recompute across the ply/deadline range ──
for (const [deadline, ply, claimant] of [[500_000_000n, 40, mc.CLAIMANT.A], [1n, 0, mc.CLAIMANT.B], [530_000_123n, 32767, mc.CLAIMANT.B], [255n, 256, mc.CLAIMANT.A]]) {
  const spikeHash = sha256(Buffer.concat([matchTag, Buffer.from(simNum(deadline)), plySpike(ply), Buffer.from([claimant])]));
  const modHash = mc.hashCheckpoint({ matchTag, deadlineDaa: deadline, ply, claimant });
  eq(`checkpoint hash matches spike (dl=${deadline} ply=${ply} cl=${claimant})`, modHash, spikeHash);
}

// ── (2) move hash agrees (hM = SHA256(hC ‖ numToBytes(D_M))) ──
{
  const hC = mc.hashCheckpoint({ matchTag, deadlineDaa: 500_000_000n, ply: 40, claimant: mc.CLAIMANT.A });
  const D_M = 500_003_600n;
  const spikeHM = sha256(Buffer.concat([Buffer.from(hC), Buffer.from(simNum(D_M))]));
  eq('move hash matches spike', mc.hashMove({ hC, nextDeadlineDaa: D_M }), spikeHM);
}

// ── (3) sign / verify roundtrip with session keys ──
const A = mc.newSessionKey(), B = mc.newSessionKey();
const cp = { matchTag, deadlineDaa: 500_000_000n, ply: 40, claimant: mc.CLAIMANT.A };
const sigA = mc.signCheckpoint(cp, A.privHex), sigB = mc.signCheckpoint(cp, B.privHex);
is('A co-sig verifies under A xonly', mc.verifyCheckpoint(cp, sigA, A.xonlyHex));
is('B co-sig verifies under B xonly', mc.verifyCheckpoint(cp, sigB, B.xonlyHex));
is('A sig does NOT verify under B xonly (key binding)', !mc.verifyCheckpoint(cp, sigA, B.xonlyHex));

// ── (4) tamper each field → verification fails (the co-sig binds every field) ──
is('tampered deadline rejected', !mc.verifyCheckpoint({ ...cp, deadlineDaa: 500_000_001n }, sigA, A.xonlyHex));
is('tampered ply rejected', !mc.verifyCheckpoint({ ...cp, ply: 41 }, sigA, A.xonlyHex));
is('tampered claimant rejected', !mc.verifyCheckpoint({ ...cp, claimant: mc.CLAIMANT.B }, sigA, A.xonlyHex));
is('tampered matchTag rejected', !mc.verifyCheckpoint({ ...cp, matchTag: Buffer.alloc(32, 0x11) }, sigA, A.xonlyHex));

// ── (5) move: mover-only signature, extends a co-signed C ──
{
  const hC = mc.hashCheckpoint(cp);
  const m = { hC, nextDeadlineDaa: 500_003_600n };
  const sigM = mc.signMove(m, A.privHex); // A is the mover
  is('move sig verifies under mover xonly', mc.verifyMove(m, sigM, A.xonlyHex));
  is('move sig does NOT verify under opponent xonly', !mc.verifyMove(m, sigM, B.xonlyHex));
  is('move with tampered nextDeadline rejected', !mc.verifyMove({ ...m, nextDeadlineDaa: 500_000_000n }, sigM, A.xonlyHex));
}

// ── (6) range guards ──
is('ply > 65535 throws', (() => { try { mc.plyField(70000); return false; } catch { return true; } })());
is('bad matchTag length throws', (() => { try { mc.checkpointPreimage({ matchTag: Buffer.alloc(31), deadlineDaa: 1n, ply: 1, claimant: 1 }); return false; } catch { return true; } })());

console.log(bad === 0 ? '\nMOVE CHANNEL OK — hashes match the on-chain-proven layout; sign/verify/tamper all correct.' : `\n${bad} FAILURE(S).`);
process.exit(bad ? 1 : 0);
