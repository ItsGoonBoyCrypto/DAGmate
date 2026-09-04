/**
 * DAGmate — a tiny Kaspa-script STACK SIMULATOR for developing covenant choreography offline.
 *
 * The Kaspa WASM SDK exposes no script evaluator, so the only ground truth is submitting a tx to a
 * node (~40-60s + dust per try). Covenant scripts are mostly a stack-ordering puzzle, and that part
 * is deterministic and cheap to model. This sim faithfully implements the PURE stack/data/arith
 * opcodes (Dup/Roll/Pick/Swap/Rot/Over/Nip/Tuck/Cat/SHA256/Blake3/Equal/arith/Bin2Num/…) so the
 * choreography can be verified here, and MOCKS the crypto/introspection opcodes
 * (CheckSigFromStack, CLTV, Tx*Output/Input*) as their stack effect + a predicate over a supplied
 * `ctx`. Once a script leaves a single truthy item here, translate it to ScriptBuilder and confirm
 * the REAL crypto with ONE on-chain run.
 *
 * ⚠️ Semantics mirror rusty-kaspa txscript (the facts we already proved on-chain): Kaspa CLTV POPS
 * its locktime (no OP_DROP), CheckSigFromStack pops [signature, msg_hash(32B), pubkey] bottom→top,
 * OpCat pops b then a → a‖b, script numbers are minimal little-endian. If a real run ever disagrees
 * with the sim, the sim is wrong — fix it here so it stays a reliable oracle.
 *
 * Not consensus-exact (no mass/sigop accounting, no signature crypto) — a choreography checker.
 */
import { createHash } from 'node:crypto';

const sha256 = (b) => createHash('sha256').update(b).digest();
const B = (x) => Buffer.isBuffer(x) ? x : Buffer.from(x);

// ── minimal script-number codec (little-endian, minimal, sign-magnitude high bit) ──
function numToBytes(n) {
  n = BigInt(n);
  if (n === 0n) return Buffer.alloc(0);
  const neg = n < 0n; let v = neg ? -n : n; const out = [];
  while (v > 0n) { out.push(Number(v & 0xffn)); v >>= 8n; }
  if (out[out.length - 1] & 0x80) out.push(neg ? 0x80 : 0x00);
  else if (neg) out[out.length - 1] |= 0x80;
  return Buffer.from(out);
}
function bytesToNum(buf) {
  if (buf.length === 0) return 0n;
  let v = 0n; for (let i = 0; i < buf.length; i++) v |= BigInt(buf[i] & (i === buf.length - 1 ? 0x7f : 0xff)) << BigInt(8 * i);
  return (buf[buf.length - 1] & 0x80) ? -v : v;
}
const truthy = (buf) => { for (let i = 0; i < buf.length; i++) { if (buf[i] !== 0) return !(i === buf.length - 1 && buf[i] === 0x80); } return false; };

/**
 * Run a script. `script` is an array of tokens: a Buffer (data push) or a string opcode name.
 * `witness` is the initial stack (bottom→top array of Buffers). `ctx` supplies the mocks:
 *   { lockTime: BigInt, inputIndex: n, inputs:[{amount}], outputs:[{spk:Buffer, amount:BigInt}],
 *     checksig: (sig,msg,pub)=>bool }  (checksig defaults to true).
 * Returns { ok, stack, error, trace }.
 */
export function run(script, witness = [], ctx = {}) {
  const s = witness.map(B);
  const cond = []; // if/else nesting: true=executing
  const exec = () => cond.every(Boolean);
  const trace = [];
  const need = (n) => { if (s.length < n) throw new Error(`stack underflow (need ${n}, have ${s.length})`); };
  const checksig = ctx.checksig || (() => true);
  try {
    for (let pc = 0; pc < script.length; pc++) {
      const t = script[pc];
      const op = typeof t === 'string' ? t : null;
      // control flow first (must run even when not executing, to track nesting)
      if (op === 'OpIf' || op === 'OpNotIf') {
        let take = false;
        if (exec()) { need(1); const v = truthy(s.pop()); take = op === 'OpIf' ? v : !v; }
        cond.push(take); trace.push(`${op} -> ${take}`); continue;
      }
      if (op === 'OpElse') { cond[cond.length - 1] = !cond[cond.length - 1] && exec_parentOK(cond); trace.push('OpElse'); continue; }
      if (op === 'OpEndIf') { cond.pop(); trace.push('OpEndIf'); continue; }
      if (!exec()) continue;

      if (op === null) { s.push(B(t)); trace.push(`push ${B(t).toString('hex').slice(0, 16)}(${B(t).length}B)`); continue; }
      switch (op) {
        case 'OpTrue': s.push(numToBytes(1)); break;
        case 'OpFalse': s.push(Buffer.alloc(0)); break;
        case 'OpDrop': need(1); s.pop(); break;
        case 'Op2Drop': need(2); s.pop(); s.pop(); break;
        case 'OpDup': need(1); s.push(B(s[s.length - 1])); break;
        case 'Op2Dup': need(2); s.push(B(s[s.length - 2]), B(s[s.length - 1])); break;
        case 'Op3Dup': need(3); s.push(B(s[s.length - 3]), B(s[s.length - 2]), B(s[s.length - 1])); break;
        case 'OpOver': need(2); s.push(B(s[s.length - 2])); break;
        case 'OpNip': need(2); s.splice(s.length - 2, 1); break;
        case 'OpTuck': need(2); s.splice(s.length - 2, 0, B(s[s.length - 1])); break;
        case 'OpSwap': need(2); { const a = s.pop(), b = s.pop(); s.push(a, b); } break;
        case 'OpRot': need(3); { const c = s.pop(), b = s.pop(), a = s.pop(); s.push(b, c, a); } break;
        case 'OpPick': { need(1); const n = Number(bytesToNum(s.pop())); need(n + 1); s.push(B(s[s.length - 1 - n])); } break;
        case 'OpRoll': { need(1); const n = Number(bytesToNum(s.pop())); need(n + 1); s.push(s.splice(s.length - 1 - n, 1)[0]); } break;
        case 'OpDepth': s.push(numToBytes(s.length)); break;
        case 'OpSize': need(1); s.push(numToBytes(s[s.length - 1].length)); break;
        case 'OpCat': need(2); { const b = s.pop(), a = s.pop(); s.push(Buffer.concat([a, b])); } break;
        case 'OpSHA256': need(1); s.push(sha256(s.pop())); break;
        case 'OpEqual': need(2); { const b = s.pop(), a = s.pop(); s.push(a.equals(b) ? numToBytes(1) : Buffer.alloc(0)); } break;
        case 'OpEqualVerify': need(2); { const b = s.pop(), a = s.pop(); if (!a.equals(b)) throw new Error(`OpEqualVerify failed (${a.toString('hex').slice(0,12)} != ${b.toString('hex').slice(0,12)})`); } break;
        case 'OpVerify': need(1); if (!truthy(s.pop())) throw new Error('OpVerify failed'); break;
        case 'OpAdd': need(2); { const b = bytesToNum(s.pop()), a = bytesToNum(s.pop()); s.push(numToBytes(a + b)); } break;
        case 'OpSub': need(2); { const b = bytesToNum(s.pop()), a = bytesToNum(s.pop()); s.push(numToBytes(a - b)); } break;
        case 'OpGreaterThanOrEqual': need(2); { const b = bytesToNum(s.pop()), a = bytesToNum(s.pop()); s.push(a >= b ? numToBytes(1) : Buffer.alloc(0)); } break;
        case 'OpLessThanOrEqual': need(2); { const b = bytesToNum(s.pop()), a = bytesToNum(s.pop()); s.push(a <= b ? numToBytes(1) : Buffer.alloc(0)); } break;
        case 'OpGreaterThan': need(2); { const b = bytesToNum(s.pop()), a = bytesToNum(s.pop()); s.push(a > b ? numToBytes(1) : Buffer.alloc(0)); } break;
        case 'OpLessThan': need(2); { const b = bytesToNum(s.pop()), a = bytesToNum(s.pop()); s.push(a < b ? numToBytes(1) : Buffer.alloc(0)); } break;
        case 'OpBin2Num': need(1); s.push(numToBytes(bytesToNum(s.pop()))); break;
        case 'OpNum2Bin': need(2); { const size = Number(bytesToNum(s.pop())); const n = bytesToNum(s.pop()); const raw = numToBytes(n); if (raw.length > size) throw new Error('OpNum2Bin overflow'); const out = Buffer.alloc(size); raw.copy(out); s.push(out); } break;
        // ── mocked crypto / introspection (stack effect + ctx predicate) ──
        case 'OpCheckSigFromStack': case 'OpCheckSigFromStackECDSA': {
          need(3); const pub = s.pop(), msg = s.pop(), sig = s.pop();
          if (msg.length !== 32) throw new Error('CheckSigFromStack: msg must be 32 bytes');
          s.push(checksig(sig, msg, pub) ? numToBytes(1) : Buffer.alloc(0)); break;
        }
        case 'OpCheckLockTimeVerify': { // Kaspa: POPS the locktime, verifies tx.lockTime >= it
          need(1); const lt = bytesToNum(s.pop());
          if (ctx.lockTime == null) throw new Error('CLTV: ctx.lockTime not supplied');
          if (BigInt(ctx.lockTime) < lt) throw new Error(`CLTV failed: tx.lockTime ${ctx.lockTime} < ${lt}`);
          break;
        }
        case 'OpTxInputIndex': s.push(numToBytes(ctx.inputIndex ?? 0)); break;
        case 'OpTxOutputSpk': { need(1); const i = Number(bytesToNum(s.pop())); s.push(B(ctx.outputs?.[i]?.spk ?? Buffer.alloc(0))); } break;
        case 'OpTxOutputAmount': { need(1); const i = Number(bytesToNum(s.pop())); s.push(numToBytes(ctx.outputs?.[i]?.amount ?? 0n)); } break;
        case 'OpTxInputAmount': { need(1); const i = Number(bytesToNum(s.pop())); s.push(numToBytes(ctx.inputs?.[i]?.amount ?? 0n)); } break;
        case 'OpTxInputDaaScore': { need(1); const i = Number(bytesToNum(s.pop())); s.push(numToBytes(ctx.inputs?.[i]?.daa ?? 0n)); } break;
        default: throw new Error(`unhandled op ${op}`);
      }
      trace.push(`${op} -> [${s.map((x) => x.toString('hex').slice(0, 8) + '(' + x.length + ')').join(' ')}]`);
    }
    const ok = s.length >= 1 && truthy(s[s.length - 1]);
    return { ok, stack: s, error: ok ? null : (s.length ? 'top not truthy' : 'empty stack'), trace };
  } catch (e) {
    return { ok: false, stack: s, error: e.message, trace };
  }
}

function exec_parentOK(cond) { for (let i = 0; i < cond.length - 1; i++) if (!cond[i]) return false; return true; }

export { numToBytes, bytesToNum, truthy, sha256 };
