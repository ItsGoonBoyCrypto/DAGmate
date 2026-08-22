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

const PORT = Number(process.env.DAGMATE_SERVICE_PORT || 8910);
const HOST = process.env.DAGMATE_SERVICE_HOST || '127.0.0.1';
// Dev-only convenience routes (throwaway keypair generation, so the site can
// be clicked through without a real Kasware/Kastle extension installed).
// On by default for local testing; set DAGMATE_DEV_ROUTES=0 in any real
// deployment — these routes have no place on a production sidecar.
const DEV_ROUTES = process.env.DAGMATE_DEV_ROUTES !== '0';

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

app.post('/escrow/anchor', wrap((req) => escrow.anchor(req.body)));

app.get('/escrow/daa', wrap(() => escrow.daaScore()));

app.get('/health', (req, res) => res.json({ ok: true, network: process.env.DAGMATE_NETWORK_ID || 'mainnet' }));

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
}

app.listen(PORT, HOST, () => {
  console.log(`DAGmate service listening on http://${HOST}:${PORT}`);
});
