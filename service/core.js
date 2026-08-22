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
 *   DAGMATE_KASPA_WRPC       — explicit node wRPC URL. OPTIONAL: unset, the
 *                              SDK's community Resolver picks a public node
 *                              (see `withRpc`). Set it to pin one node.
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

// How many resolved nodes to try before giving up. Only relevant on the
// resolver path: it hands back a random member of a community pool, so one bad
// draw shouldn't fail a settlement when the next node over is fine.
const NODE_ATTEMPTS = 3;

function newClient() {
  // An explicit URL pins one node; otherwise the SDK's Resolver fetches a
  // public one from the community pool for this network. Nothing about DAGmate
  // needs a node of its own — every call here is a plain read plus
  // submitTransaction, and the transactions are already signed before they get
  // near a node. The node choice is a liveness dependency, never a trust one:
  // a hostile node can refuse to answer or refuse to relay, but it cannot
  // forge a UTXO set we act on (deposits are re-read and re-checked) and it
  // cannot alter a tx without invalidating its signatures.
  return WRPC
    ? new k.RpcClient({ url: WRPC, networkId: NETWORK_ID, encoding: k.Encoding.Borsh })
    : new k.RpcClient({ resolver: new k.Resolver(), networkId: NETWORK_ID, encoding: k.Encoding.Borsh });
}

/** Refuse a node that would quietly give wrong answers.
 *
 * All three of these are money-critical rather than cosmetic:
 *
 * - **not synced** — its virtual DAA score is in the past. That score is what
 *   decides whether a deposit is confirmed and whether a timelock has opened,
 *   so a lagging node writes a wrong reclaim DAA into an escrow that then
 *   locks funds for longer than 14 days, or reports a deposit unconfirmed
 *   forever.
 * - **no utxoindex** — `getUtxosByAddresses` is the only way we see a deposit
 *   or fund a spend. Without the index it can't answer at all.
 * - **wrong network** — only reachable via an explicit URL (the resolver picks
 *   per network). A mainnet-configured service pointed at a testnet node
 *   derives mainnet addresses and then reads a testnet UTXO set: every escrow
 *   looks empty, and every "deposit" it does see is play money.
 */
async function assertUsable(rpc) {
  const si = await rpc.getServerInfo();
  if (!si.isSynced) throw new Error(`node ${rpc.url} is not synced`);
  if (!si.hasUtxoIndex) throw new Error(`node ${rpc.url} has no utxoindex`);
  if (si.networkId && String(si.networkId) !== NETWORK_ID) {
    throw new Error(`node ${rpc.url} is on ${si.networkId}, this service is configured for ${NETWORK_ID}`);
  }
}

/** Run `fn(rpc)` against a connected, health-checked RPC client, one session
 *  at a time. */
export async function withRpc(fn) {
  const prev = _rpcChain;
  let release;
  _rpcChain = new Promise((r) => { release = r; });
  await prev.catch(() => {}); // wait our turn (ignore the prior job's outcome)
  try {
    // Retry only covers connect + health check — never `fn`. Once fn has run,
    // it may have broadcast a transaction, and running it twice against a
    // second node could double-spend or double-anchor.
    const attempts = WRPC ? 1 : NODE_ATTEMPTS;
    let lastErr;
    for (let i = 0; i < attempts; i++) {
      const rpc = newClient();
      try {
        await rpc.connect();
        await assertUsable(rpc);
      } catch (e) {
        lastErr = e;
        await rpc.disconnect().catch(() => {});
        continue;
      }
      try {
        return await fn(rpc);
      } finally {
        await rpc.disconnect().catch(() => {});
      }
    }
    throw new Error(`no usable Kaspa node for ${NETWORK_ID}: ${lastErr?.message ?? lastErr}`);
  } finally {
    release();
  }
}
