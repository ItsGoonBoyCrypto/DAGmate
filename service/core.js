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
 * The master seed (BIP39 phrase) backs every derived key below. In production
 * it's delivered through systemd's credential store (LoadCredential=mnemonic),
 * which places it in a per-service tmpfs readable only by this process and
 * NEVER in the environment — so it can't be read out of /proc/<pid>/environ
 * the way a plain env var can. DAGMATE_MASTER_MNEMONIC remains as a fallback
 * for local dev and non-systemd hosts. Generate a FRESH seed for DAGmate —
 * never reuse a Dagger seed.
 *
 * Env vars (all DAGmate-specific, nothing shared with Dagger):
 *   DAGMATE_MASTER_MNEMONIC  — fallback seed source when not run under systemd
 *                              with a `mnemonic` credential (see above).
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
import { readFileSync } from 'node:fs';

const NETWORK_ID = process.env.DAGMATE_NETWORK_ID || 'mainnet';
const WRPC = process.env.DAGMATE_KASPA_WRPC;

/** Load the master seed, preferring systemd's credential store over the
 *  environment. `LoadCredential=mnemonic:...` exposes the seed in a per-service
 *  tmpfs at $CREDENTIALS_DIRECTORY/mnemonic, readable only by this process and
 *  absent from its environment — closing the /proc/<pid>/environ exposure a
 *  plain env var has. Falls back to DAGMATE_MASTER_MNEMONIC for local dev and
 *  non-systemd hosts. */
function loadMnemonic() {
  const dir = process.env.CREDENTIALS_DIRECTORY;
  if (dir) {
    try { return readFileSync(`${dir}/mnemonic`, 'utf8').trim(); }
    catch (e) { /* not provided as a credential — fall through to the env var */ }
  }
  return process.env.DAGMATE_MASTER_MNEMONIC;
}
const MNEMONIC = loadMnemonic();

if (!MNEMONIC) throw new Error('master seed not set — provide it via systemd LoadCredential '
  + '(mnemonic) or, for local dev, DAGMATE_MASTER_MNEMONIC');

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
  const idx = Number(matchId);
  // The index is a server-assigned rowid, never user input — but a non-hardened
  // BIP-32 index must be an integer in [0, 2^31). Reject anything outside that
  // rather than silently derive a key from a garbage/overflowed index.
  if (!Number.isInteger(idx) || idx < 0 || idx >= 2 ** 31) {
    throw new Error(`invalid matchId for key derivation: ${matchId}`);
  }
  const key = new k.PrivateKeyGenerator(xprv, false, ACCOUNT_ARBITER).receiveKey(idx);
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

// Every rpc session runs through the single serialized `_rpcChain` below, so a
// call that never returns doesn't just fail one operation — it wedges the whole
// sidecar, and because the site backend is the only thing that talks to it,
// that wedges settlement, reclaim and deposit-watching platform-wide. A hung
// socket must therefore be bounded. Connect + health are retryable (we move to
// the next node); the fn timeout is NOT retried — fn may already have broadcast
// a tx — it only exists to release the chain and surface a transport failure.
const CONNECT_TIMEOUT_MS = Number(process.env.DAGMATE_RPC_CONNECT_TIMEOUT_MS || 15000);
const CALL_TIMEOUT_MS = Number(process.env.DAGMATE_RPC_CALL_TIMEOUT_MS || 60000);

/** Reject with a transport-shaped error ("timed out …", matched by
 *  isTransportError) if `promise` hasn't settled within `ms`. The underlying
 *  operation isn't cancellable, but withRpc's `finally` disconnects the client
 *  and releases the queue, so a stuck node can't hold the sidecar hostage. */
function withTimeout(promise, ms, label) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

// ── which node, and STAYING on it ───────────────────────────────────────
//
// An explicit URL pins one node; otherwise the SDK's Resolver fetches a public
// one from the community pool. Nothing about DAGmate needs a node of its own:
// every call is a read plus submitTransaction, and transactions are fully
// signed before a node sees them. The node is a liveness dependency, never a
// trust one — a hostile one can refuse to answer or refuse to relay, but it
// cannot alter a signed tx and it cannot invent a deposit at an address we
// re-read and re-check ourselves.
//
// ⚠️ But we must STICK to one node once we have found a good one, and that is
// not a performance nicety. Two reasons, both learned the hard way in Dagger's
// kron-service:
//
// 1. **Mempool ancestry.** `withRpc` opens and closes a connection per call, so
//    without a pin, consecutive calls land on different nodes. Move anchors
//    chain — anchor N+1 spends the change from anchor N — so a fast game
//    submits a child to a node that has never heard of its parent, and it
//    orphans. Dagger hit exactly this on 2026-08-20 after a swap.
// 2. **Cost per call.** Resolving is an HTTP round-trip before the WebSocket
//    handshake, and `withRpc` serializes, so every operation queues behind the
//    last one's discovery. Dagger blew a 30s withdraw timeout this way.
//
// So: resolve once, remember the URL, and only let go of it when the node
// itself is the thing that failed.
let _resolver = null;
let _pinnedUrl = null;

function newClient() {
  if (WRPC) return new k.RpcClient({ url: WRPC, networkId: NETWORK_ID, encoding: k.Encoding.Borsh });
  if (_pinnedUrl) return new k.RpcClient({ url: _pinnedUrl, networkId: NETWORK_ID, encoding: k.Encoding.Borsh });
  // No config object: `new Resolver({ tls: true })` throws "Invalid or missing
  // resolver URL" despite `urls` being typed optional — the tls flag is only
  // usable alongside your own resolver list. TLS is enforced on the resolved
  // URL instead (see assertUsable), which is the stronger check anyway: it
  // asserts what we actually got rather than what we asked for.
  _resolver ??= new k.Resolver();
  return new k.RpcClient({ resolver: _resolver, networkId: NETWORK_ID, encoding: k.Encoding.Borsh });
}

/** Is this the node failing, as opposed to the chain rejecting what we sent?
 *
 * ⚠️ The distinction is the whole point. Re-resolving on a *rejected
 * transaction* is what strands mempool ancestry: the rejection is usually the
 * node correctly telling us something about our own tx, and hopping to a
 * different node in response means the next tx we build is a child of a parent
 * that node has never seen. Only transport failures — the node is gone, not
 * disagreeing — are allowed to drop the pin.
 */
function isTransportError(e) {
  const m = String(e?.message ?? e).toLowerCase();
  return /connect|websocket|socket|timed out|timeout|unreachable|not synced|utxoindex|is on /.test(m);
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
 * - **plaintext transport** — the resolver may hand back a `ws://` endpoint.
 *   Every address this service queries goes over that link, which is the full
 *   set of live match escrows: readable by anyone on the path, and their
 *   answers tamperable. Refused unless the operator asked for it explicitly by
 *   setting DAGMATE_KASPA_WRPC themselves (a private/localhost node is a
 *   legitimate reason to run without TLS).
 */
async function assertUsable(rpc) {
  if (!WRPC && !String(rpc.url).startsWith('wss://')) {
    throw new Error(`resolver returned a plaintext endpoint (${rpc.url}) — refusing to query escrows over it`);
  }
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
        await withTimeout(rpc.connect(), CONNECT_TIMEOUT_MS, 'node connect');
        await withTimeout(assertUsable(rpc), CONNECT_TIMEOUT_MS, 'node health check');
      } catch (e) {
        lastErr = e;
        // The pinned node is gone, hung, or out of sync. Let it go, so the next
        // attempt resolves a fresh one rather than retrying a corpse. (A connect
        // timeout lands here and is retried, which is safe — nothing broadcast.)
        _pinnedUrl = null;
        await rpc.disconnect().catch(() => {});
        continue;
      }
      // Healthy. Remember it — subsequent calls reconnect straight here, which
      // is what keeps a chain of move anchors on one mempool.
      if (!WRPC) _pinnedUrl = rpc.url;
      try {
        // NOT retried on timeout: fn may already have submitted a transaction,
        // so running it a second time could double-spend/double-anchor. The
        // timeout only bounds how long a stuck node can hold the queue.
        return await withTimeout(fn(rpc), CALL_TIMEOUT_MS, 'rpc call');
      } catch (e) {
        // ⚠️ Only unpin if the NODE failed. A rejected transaction is the chain
        // disagreeing with us, and moving nodes over it orphans our own
        // children — see isTransportError.
        if (isTransportError(e)) _pinnedUrl = null;
        throw e;
      } finally {
        await rpc.disconnect().catch(() => {});
      }
    }
    throw new Error(`no usable Kaspa node for ${NETWORK_ID}: ${lastErr?.message ?? lastErr}`);
  } finally {
    release();
  }
}
