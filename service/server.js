/**
 * DAGmate — service/ HTTP wrapper (docs/DAGMATE_SPEC.md §3).
 *
 * Localhost-bound REST surface over escrow.js, for the site backend to call.
 * No auth beyond network isolation (127.0.0.1 by default) — this process
 * holds the arbiter/operating keys, so it should never be exposed directly
 * to the internet; only the site backend talks to it.
 *
 * Run: `node server.js` — needs DAGMATE_MASTER_MNEMONIC + DAGMATE_KASPA_WRPC
 * (see core.js). DAGMATE_SERVICE_PORT defaults to 8910.
 */
import { randomBytes } from 'node:crypto';
import express from 'express';
import * as escrow from './escrow.js';
import * as core from './core.js';
import * as auth from './auth.js';

const PORT = Number(process.env.DAGMATE_SERVICE_PORT || 8910);
const HOST = process.env.DAGMATE_SERVICE_HOST || '127.0.0.1';
// Dev-only convenience routes (throwaway keypair generation, so the site can
// be clicked through without a real Kasware/Kastle extension installed).
//
// OFF unless explicitly switched on. Opt-in rather than opt-out because the
// failure modes are asymmetric: forgetting to set this on costs a developer
// two minutes, forgetting to set it OFF puts key-minting on a public host.
// A deployment that silently inherits a dev default is the whole problem.
//
// And never on mainnet, whatever the env says. A "demo wallet" there is a
// throwaway key holding real money, handed to someone who has been told it's
// for testing — there is no legitimate reason to want that, so an env var
// isn't allowed to ask for it.
const NETWORK = process.env.DAGMATE_NETWORK_ID || 'mainnet';
const DEV_ROUTES = process.env.DAGMATE_DEV_ROUTES === '1' && !NETWORK.startsWith('mainnet');

const app = express();
app.use(express.json());

function wrap(handler) {
  return async (req, res) => {
    try {
      const result = await handler(req);
      res.json(result);
    } catch (e) {
      console.error(e);
      res.status(400).json({ error: String(e?.message ?? e) });
    }
  };
}

app.get('/escrow/arbiter-pubkey', wrap((req) =>
  escrow.arbiterPubkey({ matchId: req.query.matchId })));

app.post('/escrow/build', wrap((req) => escrow.buildEscrow(req.body)));

app.post('/escrow/balances', wrap((req) => escrow.addressBalances(req.body)));

app.post('/escrow/settle-unsigned', wrap((req) => escrow.buildSettleUnsigned(req.body)));

app.post('/escrow/settle-broadcast', wrap((req) => escrow.broadcastSettle(req.body)));

app.post('/escrow/reclaim-unsigned', wrap((req) => escrow.buildReclaimUnsigned(req.body)));

app.post('/escrow/reclaim-broadcast', wrap((req) => escrow.broadcastReclaim(req.body)));

app.post('/escrow/anchor', wrap((req) => escrow.anchor(req.body)));

app.get('/escrow/daa', wrap(() => escrow.daaScore()));

// Wallet-ownership proof. Always 200 with {ok:false, reason} on a failed
// check rather than an error status — a rejected login is a normal outcome
// here, and the backend shouldn't have to tell "you didn't prove it" apart
// from "the sidecar is broken".
app.post('/auth/verify-message', wrap((req) => auth.verifyOwnership(req.body)));

// `devRoutes` is reported so the backend (and through it the UI) can derive
// what's available instead of keeping its own guess in sync with this one.
app.get('/health', (req, res) => res.json({ ok: true, network: NETWORK, devRoutes: DEV_ROUTES }));

if (DEV_ROUTES) {
  // Throwaway keypair — NEVER a substitute for real wallet-connect signing,
  // just lets the site's full click-through flow (challenge → escrow →
  // board → moves) be exercised without a browser extension installed.
  app.post('/dev/demo-keypair', wrap(() => {
    const k = core.wasm();
    const key = new k.PrivateKey(randomBytes(32).toString('hex'));
    const address = key.toPublicKey().toAddress(core.netType()).toString();
    const pub = key.toPublicKey();
    const xo = pub.toXOnlyPublicKey ? pub.toXOnlyPublicKey() : pub;
    return { address, pubkey: String(xo.toString()).replace(/^0x/, ''), privateKeyHex: key.toString() };
  }));

  // Lets the demo wallet answer a login challenge, so local testing exercises
  // the real auth path instead of a bypass around it — a login flow that only
  // ever runs in production is a login flow nobody has tested.
  app.post('/dev/sign-message', wrap((req) => auth.signWithDemoKey(req.body)));
}

app.listen(PORT, HOST, () => {
  console.log(`DAGmate service listening on http://${HOST}:${PORT} (${NETWORK})`);
  if (DEV_ROUTES) console.warn('⚠️  DEV ROUTES ENABLED — /dev/* is live. Never do this on a public host.');
});
