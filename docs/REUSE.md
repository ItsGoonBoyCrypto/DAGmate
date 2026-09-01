# DAGmate REUSE Map — what the expansion inherits for free

> Produced per §0 of the expansion spec: *read the repo end-to-end, then map every
> reusable piece before writing a line of new game code.* This is the ground truth
> the expansion spec (`DAGMATE_EXPANSION_SPEC.md`) is built on. Real file paths and
> function names as of 2026-08-31 (commit `7ef406f`).

DAGmate is:
- **Python / FastAPI backend** — `site/backend/` — auth, game state, orchestration.
- **Node sidecar** — `service/` — all Kaspa crypto: escrow build, settlement, RPC, anchoring. Holds the arbiter key. The backend never touches a private key.
- **Vanilla-JS frontend** — `site/frontend/js/app.js` (~1400 lines) — wallet connect + board.
- All money is integer **sompi** (1 KAS = 1e8 sompi). `RAKE_BPS = 0` — winner takes the whole pot minus gas.

The single most important finding: **only the game rules are chess-coupled.** Auth,
escrow, deposits, settlement, reclaim, tournaments, clocks-the-mechanism, and the
whole money path are game-agnostic already. Adding a game is: one new rules module +
a `game` discriminator + board rendering. Nothing on the money path changes.

---

## Reusability at a glance

| # | Capability | Key files | Chess-coupled? | Expansion effort |
|---|-----------|-----------|----------------|------------------|
| 1 | Wallet auth (signMessage) | `auth.py`, `service/auth.js` | none | reuse as-is |
| 2 | Escrow create (2-of-3 + CLTV) | `service/escrow.js`, `service_client.py` | none | reuse as-is |
| 3 | Settlement / payout (arbiter co-sign) | `settlement.py`, `service/escrow.js` | none | reuse as-is |
| 4 | Matchmaking / challenges | `main.py`, `database.py` | none | reuse + carry `game` field |
| 5 | Turn relay (`/move` + polling) | `main.py`, `chess_logic.py` | **YES** | dispatch to engine |
| 6 | Clocks / timeout | `clocks.py` | timeout *result* only | engine hook |
| 7 | Resign / draw / abandon / replay | `main.py`, `database.py` | draw is game-specific | engine flags |
| 8 | Result attestation (who wins) | `chess_logic.py`, `main.py` | **YES** | engine returns outcome |
| 9 | Tournament bracket | `main.py`, `database.py` | mode copy only | reuse as-is |
| 10 | Accounts / (no) leaderboard | `main.py`, `database.py` | none | add per-game ELO |
| 11 | Deposit watcher | `deposits.py`, `service/escrow.js` | none | reuse as-is |
| 12 | UI shell | `app.js` | board render + picker | per-game board module |
| 13 | DB schema | `database.py:ensure_schema` | `fen`/`moves_json`/`turn`/`draw_offer_*` | add `game` + generalise state |
| 14 | On-chain anchoring | `main.py`, `service/escrow.js` | payload string | reuse, adapt payload |

Legend: **reuse as-is** = the expansion should not touch this file's logic.

---

## 1. Wallet connect / login — REUSE AS-IS
- `auth.py`: `issue_nonce`, `login_message` (rebuilt server-side from the stored row — replay defence), `verify`, `account_for_token`, `logout`.
- Endpoints in `main.py`: `POST /api/auth/nonce`, `/verify`, `/logout`; `require_account()` dependency.
- Sidecar: `service/server.js POST /auth/verify-message` → `service/auth.js verifyOwnership` (WASM verify + address-from-pubkey). Backend only sees `{ok}`.
- DB: `auth_nonces` (single-use, guarded consume), `sessions` (SHA-256 of token only), `accounts`.
- **Nothing game-aware.** A draughts or backgammon player logs in identically.

## 2. Escrow create (2-of-3 P2SH + CLTV) — REUSE AS-IS
- `service/escrow.js buildEscrow` → IF branch `2 pkA pkB pkArb 3 CHECKMULTISIG`, ELSE branch `reclaimDaa CLTV depositorPk CHECKSIG`. `toXOnly`/`xOnlyHex` normalise wallet keys.
- Per-match arbiter key: `service/core.js deriveArbiter(matchId)` — HD-derived from the match rowid (`hd_index`); nothing persisted.
- Python: `service_client.build_escrow`; orchestrated by `main._create_match_from_pair` + `main._reclaim_daa` (fails closed if the node is unreachable — never builds a stale timelock).
- The escrow knows stake + two pubkeys + a reclaim DAA. **It does not know the game.**

## 3. Settlement / payout (arbiter co-sign) — REUSE AS-IS
- `settlement.py`: `prepare` (build once), `submit` (extract player sigs from the wallet-signed tx, broadcast when complete), `_signer_for` (decisive → winner signs all inputs; draw → each depositor signs their own escrow), `_rake_sompi` (0 unless `RAKE_BPS>0`).
- Sidecar: `buildSettleUnsigned`, `settleSigScript`, `broadcastSettle`, `extractSigs`.
- Guarded once each: `save_settlement_build`, `save_settlement_sigs`, `mark_settlement_broadcast`.
- Payout is `winner gets pot − fee` or an even split. **It only needs a winner (or a draw), which the engine supplies.** Zero game logic here.

## 4. Matchmaking / challenges — REUSE, carry `game`
- `main.py`: `new_challenge`, `list_challenges`, `accept_challenge` (atomic claim → build escrows → notify), `decline_challenge`.
- `database.py`: `create_challenge`, `claim_challenge_for_accept` (open→accepting guarded), `release_challenge_to_open`, `create_match`.
- `mode` is free-text metadata already. The expansion adds a sibling `game` field carried the same way. No structural change.

## 5. Turn relay (`/move` + polling) — **CHESS-COUPLED, dispatch to engine**
- `main.make_move` [`POST /api/matches/{id}/move`]: flag check → validate/apply → clock charge → commit → notify → optional anchor.
- `chess_logic.py`: `apply_uci`, `legal_uci_moves`, `status_of`, `board_from_history` (repetition needs full history), `timeout_result`, `STARTING_FEN`.
- `database.apply_move_with_clock` — position + moves + turn + clock + clear draw offer in one guarded UPDATE (guards on `turn`, so two racing moves can't both land).
- Polling: frontend GETs `/api/matches/{id}` every ~4s. **No WebSockets.** The expansion keeps polling.
- **This is the seam.** `make_move` must dispatch on `game` to the right engine instead of calling `chess_logic` directly.

## 6. Clocks / timeout — REUSE mechanism, engine supplies the *result*
- `clocks.py`: `now_ms`, `settings_for(mode)`, `remaining_ms`, `flagged_color`, `charge_move`, `public`, `forfeit_if_flagged`, `watch_loop` (background forfeit even with no UI open).
- Only `chess_logic.timeout_result` (FIDE 6.9 insufficient-material draw) is game-specific. Draughts/backgammon: a flag is simply a loss.
- Clock columns are `clock_white_ms`/`clock_black_ms`/`clock_increment_ms`/`clock_turn_started_ms`/`clock_warned_*`. Backgammon reuses white=first-mover, black=second.

## 7. Resign / draw / abandon / replay — REUSE, engine flags draw-ability
- `main.py`: `resign`, `_settle_game_over`, `draw_offer`/`draw_accept`/`draw_decline`, `_replay_tournament_draw`.
- Tournament games refuse draw offers (a bracket can't promote a draw); a board draw replays on the same escrows via `reset_match_for_replay` (guarded on `live`, bumps `replay_count`).
- **Backgammon has no draws** — the engine will report `draws_possible = False`; the draw endpoints simply won't be offered for it. Resign/abandon stay generic.

## 8. Result attestation — **CHESS-COUPLED, engine returns outcome**
- Authority is never the calling player. It's the game state (`chess_logic.status_of` over FEN+moves), the timekeeper (`clocks.forfeit_if_flagged`), or a server action (resign).
- `_settle_game_over(match_id, result, winner_color)` maps a colour to a player id and hands settlement a winner.
- The expansion swaps the *source of truth* (engine `outcome()`), not the plumbing.

## 9. Tournament bracket — REUSE AS-IS
- `main.py`: `join_tournament`, `_start_tournament`, `advance_tournament` (position-stable pair-walk), `_carry_rep`, `_announce_champion`, `_announce_tournament_void`.
- `database.py`: `round_winners_if_complete` + `_round_match_terminal`, `claim_round_advance` (PK-collision guard), `set_tournament_champion`, `create_tournament_carry` (bye/dead rows), `void_tournament`.
- Doubling-stake winner-takes-pool + walkover + board-draw replay + neither-funds void/bye all proven live (`7ef406f`). **Entirely game-agnostic** — it only needs "who won this match". Draughts/backgammon tournaments work the day the engine lands.

## 10. Accounts / leaderboard — REUSE, add per-game ELO
- `accounts` table: `id`, `address` (unique), `pubkey`, `accept_challenges`, `is_demo_wallet`. No rating today (deliberate gap).
- The expansion adds **separate ELO per game** (a chess rating and a draughts rating are not interchangeable) — see spec §6. New table, no change to `accounts`.

## 11. Deposit watcher — REUSE AS-IS
- `deposits.py`: `watch_loop`, `poll_once`, `_check_match` (each escrow independently ≥ stake, confirmed ≥ `DEPOSIT_CONFIRM_DAA`; latched `funded_*_ts`; deadline → expire / walkover / void).
- `service_client.escrow_balances`, `database.record_deposits`, `mark_match_live`.
- Pure on-chain balance logic. **Never sees the game.**

## 12. UI shell — REUSE frame, per-game board module
- `app.js` generic (reuse): wallet connect, auth, session/localStorage, `api()` fetch helper, challenge board, match list, settlement/reclaim panels, learn page, toast, KNS `displayName`, `kasFromSompi` BigInt formatting, the 4s polling loop.
- Chess-specific (branch): `fenToBoard`/`renderBoard`, the click→legal-targets move picker, clock render, draw UI.
- Plan: a small per-game board renderer selected off the match's `game` field; the shell around it is untouched.

## 13. DB schema (`database.py ensure_schema`) — add `game`, generalise state
Game-agnostic columns on `matches` (keep): `id`, `hd_index`, `challenge_id`, `tournament_id`, `round`, `player_a_account_id`, `player_b_account_id`, `stake_sompi`, `mode`, `status`, all `escrow_*`, `reclaim_daa`, all `funded_*`, all `clock_*`, all `settle_*`, `reclaim_*_txid`, `winner_account_id`, `result`, `replay_count`, `created_ts`, `settled_ts`.

Chess-specific columns (generalise): `fen` (→ opaque `state_json`), `moves_json` (already generic JSON), `turn` (reused; backgammon maps white/black to first/second mover), `draw_offer_by`/`draw_offer_ply` (only used by draw-capable games).

**Minimal migration:** add `game TEXT NOT NULL DEFAULT 'chess'` to `matches` and `challenges`. Existing rows read as chess. `fen` stays the chess state; new games write their own state into the same or a parallel column (spec §2 decides). Other tables (`accounts`, `challenges`, `tournaments`, `tournament_entrants`, `tournament_rounds`, `sessions`, `auth_nonces`, `learn_progress`, `kns_cache`) need no game column except `challenges`.

## 14. On-chain anchoring — REUSE, adapt payload
- `config.ANCHOR_MOVES` (default OFF, forced off on mainnet), `main._anchor_move`, `service_client.anchor`, sidecar `POST /escrow/anchor`.
- Payload today: `DGMT|{matchId}|{ply}|{uci}` as hex, best-effort dust tx from the operating address.
- The expansion reuses this verbatim for **backgammon dice commit-reveal tamper-evidence** — anchor the dice commitment hash the same way (spec §4a). Adapt only the payload string.

---

## The seam, in one paragraph
Every new game is a Python module exposing a tiny fixed interface (`initial_state`,
`legal_moves`, `apply_move`, `outcome`, `serialize`/`deserialize`, plus flags like
`draws_possible`, `is_stochastic`). `make_move` and `_settle_game_over` dispatch on the
match's `game` field to that module; the frontend picks a board renderer off the same
field. **Money, escrow, deposits, settlement, reclaim, and the tournament bracket are
already game-blind and stay untouched.** That is the whole expansion in one sentence.
