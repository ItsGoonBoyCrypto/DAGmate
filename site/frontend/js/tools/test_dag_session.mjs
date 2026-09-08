// Byte-match guard: the BROWSER session module (dag_session.js) must produce the SAME checkpoint/move
// hashes as the Node twin the sidecar uses (service/move_channel.mjs) — otherwise a signature made in
// the browser won't verify inside the covenant. Runs dag_session.js under Node (it shims `self` so the
// vendored noble resolves). Run: node site/frontend/js/tools/test_dag_session.mjs
import * as ds from '../dag_session.js';
import * as mc from '../../../../service/move_channel.mjs';

let bad = 0;
const eq = (label, a, b) => { const ok = Buffer.from(a).equals(Buffer.from(b)); if (!ok) bad++; console.log(`   ${ok ? 'ok ' : 'BAD'} ${label}`); };
const is = (label, c) => { if (!c) bad++; console.log(`   ${c ? 'ok ' : 'BAD'} ${label}`); };

const matchTag = 'a'.repeat(64);

// (1) checkpoint hashes agree across the ply/deadline/claimant range
for (const [deadlineDaa, ply, claimant] of [[500000000n, 40, 1], [1n, 0, 2], [530000123n, 32767, 2], [255n, 256, 1]]) {
  const cp = { matchTag, deadlineDaa, ply, claimant };
  eq(`checkpoint hash (dl=${deadlineDaa} ply=${ply} cl=${claimant})`, await ds.hashCheckpoint(cp), mc.hashCheckpoint(cp));
}

// (2) move hash agrees
{
  const hC = mc.hashCheckpoint({ matchTag, deadlineDaa: 500000000n, ply: 40, claimant: 1 });
  const hcHex = Buffer.from(hC).toString('hex');
  const m = { hC: hcHex, nextDeadlineDaa: 500003600n };
  eq('move hash', await ds.hashMove(m), mc.hashMove({ hC, nextDeadlineDaa: 500003600n }));
}

// (3) a signature made in the browser module VERIFIES under the Node twin (interop = covenant-valid)
{
  const { privHex, xonlyHex } = await ds.mint();
  const cp = { matchTag, deadlineDaa: 500000000n, ply: 40, claimant: 1 };
  const sig = await ds.signCheckpoint(cp, privHex);
  is('browser sig verifies under browser module', await ds.verifyCheckpoint(cp, sig, xonlyHex));
  is('browser sig verifies under the Node twin (move_channel)', mc.verifyCheckpoint(cp, Buffer.from(sig, 'hex'), xonlyHex));
  // and the reverse: a move_channel-signed checkpoint verifies in the browser module
  const k = mc.newSessionKey();
  const sig2 = Buffer.from(mc.signCheckpoint(cp, k.privHex)).toString('hex');
  is('Node-twin sig verifies under the browser module', await ds.verifyCheckpoint(cp, sig2, k.xonlyHex));
  // tamper rejected
  is('tampered ply rejected', !(await ds.verifyCheckpoint({ ...cp, ply: 41 }, sig, xonlyHex)));
}

// (4) mint yields a 32-byte x-only key
{ const { xonlyHex } = await ds.mint(); is('mint x-only is 32 bytes', xonlyHex.length === 64); }

console.log(bad === 0 ? '\nDAG SESSION OK — browser hashes match move_channel; sigs interop; covenant-valid.' : `\n${bad} FAILURE(S).`);
process.exit(bad ? 1 : 0);
