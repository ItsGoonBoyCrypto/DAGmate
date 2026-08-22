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

- `service/` — Kaspa L1 scripting/RPC sidecar: escrow build, wallet-connect
  settlement (`buildSettleUnsigned` + `broadcastSettle`), move anchors, DAA
  lookups. `service/core.js` (WASM/RPC/HD-seed init) is the one piece not
  yet built — see the header of `service/escrow.js` for its exact contract.
- `bot/` — Telegram **alerts-only** bot: `/start` (link code), `/alerts
  on|off`, `/unlink`, plus an internal shared-secret webhook API the site
  backend calls to push notifications. No wallet, no game state, no keys.
- `site/` — dagmate.org (not yet built): wallet-connect UI, matchmaking,
  board, clock, backend API that orchestrates escrow build + settlement.
- `docs/DAGMATE_SPEC.md` — build spec: escrow model, settlement rules, clock
  rules, on-chain anchor format, service REST surface, bot API surface.

Owner-gated / private until GoonBoy has tested it end-to-end and it's ready for
public testing.
