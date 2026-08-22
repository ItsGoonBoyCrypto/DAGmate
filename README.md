# DAGmate

P2P wagered chess on Kaspa L1 — real 2-of-3 P2SH escrow on mainnet, pay-per-move
on-chain anchors, arbiter-settled payouts.

**Standalone product.** Own domain (dagmate.org), own repo, own bot, own
service, own database. Shares no code, infrastructure, users, or funds with
any other project.

**Trust model: non-custodial wallet-connect.** Players connect their own
wallet (Kasware, Kastle at launch — Kaspire once its SDK is verified;
Kaspium for funding only) and sign every transaction themselves. DAGmate
never holds a player's private key or general wallet balance. The one piece
of centralization is a per-match **arbiter co-signing key** (server-side,
DAGmate's own HD seed) needed to release the 2-of-3 escrow on game end — if
the service ever goes dark, each player can still unilaterally reclaim their
own stake after a 14-day CLTV timeout. See `docs/DAGMATE_SPEC.md` for the
full design.

**Identity is a signature, not an address.** Addresses are public — they're
printed on the match view — so every mutating endpoint takes its account from
a session token earned by signing a server-issued nonce with the wallet
(`signMessage`, never `signPskt`). See `docs/DAGMATE_SPEC.md` §1.1.

- `service/` — Kaspa L1 scripting/RPC sidecar: WASM/RPC/HD-seed init
  (`core.js`), escrow build, wallet-connect settlement (`buildSettleUnsigned`
  + `broadcastSettle`), move anchors, DAA lookups, wallet-ownership proof
  (`auth.js`). Holds the arbiter and operating keys, so it binds to 127.0.0.1
  and only the site backend talks to it.
- `bot/` — Telegram **alerts-only** bot: `/start` (link code), `/alerts
  on|off`, `/unlink`, plus an internal shared-secret webhook API the site
  backend calls to push notifications. No wallet, no game state, no keys.
- `site/` — dagmate.org: wallet-connect UI, matchmaking, board, clocks, and
  the FastAPI backend (python-chess is the rules authority) that orchestrates
  escrow build, deposit watching and settlement. Serves the frontend too, so
  `uvicorn main:app` is the one process to run.
- `docs/DAGMATE_SPEC.md` — build spec: escrow model, settlement rules, clock
  rules, on-chain anchor format, service REST surface, bot API surface.

## Running it

Both processes need env; there are no secrets in this repo and no defaults
that reach a live node.

    service/   DAGMATE_MASTER_MNEMONIC   (required)
               DAGMATE_KASPA_WRPC        (required — no default node URL)
               DAGMATE_NETWORK_ID        (default mainnet)
    site/      DAGMATE_SERVICE_URL       (default http://127.0.0.1:8910)
               DAGMATE_NETWORK_ID        (default mainnet)

⚠️ **`DAGMATE_DEV_ROUTES` is opt-in and mainnet-refused.** It gates the demo
wallet and `dev-mark-funded`, the latter of which starts a match nobody paid
for. It defaults to off in both processes, and is ignored outright on mainnet,
because the failure modes are asymmetric: forgetting to switch it on costs two
minutes, forgetting to switch it off puts a free-money button on a public
host. `site/backend/dev.cmd` turns it on for local work.

Tests are dependency-free — `python site/backend/tools/test_auth.py` (and
`test_settlement`, `test_deposits`, `test_clocks`). Real schema, real DB
accessors, throwaway database; only the chain is stubbed.

Owner-gated / private until GoonBoy has tested it end-to-end and it's ready for
public testing.
