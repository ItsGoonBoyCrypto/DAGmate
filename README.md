# DAGmate

P2P wagered chess on Kaspa L1 — real 2-of-3 P2SH escrow on mainnet, pay-per-move
on-chain anchors, arbiter-settled payouts.

**Status:** early extraction, not yet standalone. This repo currently holds code
pulled out of an internal platform prototype; it still references that
platform's config/database/wallet modules and is not runnable as-is. Being
rebuilt with its own bot/login, database, wallet custody, and Kaspa sidecar —
fully decoupled, sharing no infrastructure, users, or funds with anything else.

- `bot/` — chat/game-flow layer (interface TBD — see open design question below)
- `service/` — Kaspa L1 scripting/RPC sidecar (escrow build + settle, currently
  depends on a wallet-derivation module not yet in this repo)
- `site/` — dagmate.org (not yet built)
- `docs/DAGMATE_SPEC.md` — build spec: escrow model, settlement rules, clock
  rules, on-chain anchor format

**Open design question:** Telegram bot (custodial per-user HD wallet, like the
prototype) vs. a website with wallet-connect (Kasware/Kastle — non-custodial,
users sign their own txs). Deciding before the rebuild continues.

Owner-gated / private until ready for public testing.
