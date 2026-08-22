/**
 * DAGmate — Kaspa WASM/RPC/HD-seed core (docs/DAGMATE_SPEC.md §2, §3).
 *
 * The one module that touches Kaspa L1 directly. Everything above this
 * (escrow.js, spikes.mjs) is pure script/tx logic built on top of what this
 * file exports. Own HD seed, own env vars, own key layout — zero Dagger code
 * or shared key material, even though the underlying SDK (`@kronsdk/kron-sdk`,
 * a public package) and a couple of proven patterns (the WASM re-entrancy
 * serialization queue below) are the same ones Dagger's kron-service uses,
 * because they're facts about the SDK/protocol, not about Dagger.
 *
 * Env vars (all DAGmate-specific, nothing shared with Dagger):
 *   DAGMATE_MASTER_MNEMONIC  — BIP39 phrase backing every derived key below.
 *                              Required. Generate a FRESH one for DAGmate —
 *                              never reuse a Dagger seed.
 *   DAGMATE_NETWORK_ID       — 'mainnet' (default) or a testnet id (e.g.
 *                              'testnet-10').
 *   DAGMATE_KASPA_WRPC       — explicit node wRPC URL, required (no silent
 *                              default to some public node we haven't
 *                              ourselves verified is trustworthy/stable for
 *                              this project).
 *   DAGMATE_FEE_ADDRESS      — optional override for where tournament/match
 *                              rake lands; falls back to the operating
 *                              address if unset.
 *
 * Key layout — two HD accounts under the one master seed, cleanly separated
 * by purpose (see docs/DAGMATE_SPEC.md §2.1):
 *   account 0n, index 0  — OPERATING address: funds move anchors + escrow
 *                          funding in spikes, receives change/rake.
 *   account 1n, index=matchId — per-match ARBITER co-signing key. Every
 *                          match gets its own arbiter key, deterministically
 *                          derived from its matchId — nothing to persist.
 */
import { loadKaspa } from '@kronsdk/kron-sdk/wasm';

const NETWORK_ID = process.env.DAGMATE_NETWORK_ID || 'mainnet';
const WRPC = process.env.DAGMATE_KASPA_WRPC;
const MNEMONIC = process.env.DAGMATE_MASTER_MNEMONIC;

if (!MNEMONIC) throw new Error('DAGMATE_MASTER_MNEMONIC not set — the HD master seed is required');
if (!WRPC) throw new Error('DAGMATE_KASPA_WRPC not set — an explicit node URL is required (no silent default)');

export const COVENANT_OPTS = { flags: { covenantsEnabled: true } };

const ACCOUNT_OPERATING = 0n;
const ACCOUNT_ARBITER = 1n;

// Top-level await: by the time anything does `import * as core from './core.js'`,
// WASM is already loaded and `xprv` already derived, so `core.wasm()` and
// `core.deriveArbiter()` etc. are safe to call synchronously right away —
// escrow.js and spikes.mjs both rely on this (see spikes.mjs's `const k = core.wasm();`
// at its own module top level).
const k = await loadKaspa();
const xprv = new k.XPrv(new k.Mnemonic(MNEMONIC).toSeed());

export function wasm() {
  return k;
}

export function netType() {
  return NETWORK_ID === 'mainnet' ? k.NetworkType.Mainnet : k.NetworkType.Testnet;
}

export function network() {
  return NETWORK_ID;
}

/** Deterministically derive the per-match arbiter co-signing key. */
export function deriveArbiter(matchId) {
  const key = new k.PrivateKeyGenerator(xprv, false, ACCOUNT_ARBITER).receiveKey(Number(matchId));
  const address = key.toPublicKey().toAddress(netType()).toString();
  return { key, address };
}

/** DAGmate's single fixed operating address — funds move anchors, escrow
 *  funding in spikes, receives settlement change and (absent an explicit
 *  DAGMATE_FEE_ADDRESS) rake. */
export function operatingAddress() {
  const key = new k.PrivateKeyGenerator(xprv, false, ACCOUNT_OPERATING).receiveKey(0);
  const address = key.toPublicKey().toAddress(netType()).toString();
  return { key, address };
}

/** Rake/fee destination. A plain address string (not a key — this is a
 *  payout target, never something this service signs FROM on its own). */
export function feeAddress() {
  return process.env.DAGMATE_FEE_ADDRESS || operatingAddress().address;
}

// The kaspa wasm module (`k`) is a single shared instance. Building/signing
// covenant txs re-enters its Rust objects, and two of those running
// concurrently trips "recursive use of an object … unsafe aliasing in rust"
// — a proven SDK-level bug (see Dagger's kron-service, which hit and fixed
// this the same way). Serialize every rpc/wasm session through a promise
// chain so only one runs at a time.
let _rpcChain = Promise.resolve();

/** Run `fn(rpc)` against a connected RPC client, one session at a time. */
export async function withRpc(fn) {
  const prev = _rpcChain;
  let release;
  _rpcChain = new Promise((r) => { release = r; });
  await prev.catch(() => {}); // wait our turn (ignore the prior job's outcome)
  const rpc = new k.RpcClient({ url: WRPC, networkId: NETWORK_ID, encoding: k.Encoding.Borsh });
  try {
    await rpc.connect();
    return await fn(rpc);
  } finally {
    await rpc.disconnect();
    release();
  }
}
