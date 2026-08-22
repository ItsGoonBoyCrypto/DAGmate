# DAGMATE — Build Spec v1

**Target:** weekend build (Fri 22 → Sun 24 Aug 2026), **mainnet**, owner-gated beta (GoonBoy + allowlist).
**Product:** P2P wagered chess inside the Dagger Telegram bot. Pot escrowed on Kaspa L1 in a real 2-of-3 script address, every move optionally anchored on-chain as a payload tx with a micro-fee, winner takes pot minus rake.

---

## 0. Scope for THIS weekend

**IN:**
- 1v1 challenge with KAS stake (or 0-stake friendly), both modes: `rapid` (live, bot clock) and `daily` (correspondence, 24h/move)
- Real on-chain escrow: one **2-of-3 P2SH address per player per match** (player A, player B, per-match arbiter key), with a unilateral CLTV reclaim branch after 14 days (worst case if VPS dies: everyone gets their own stake back — nobody can steal the other side's)
- Full rules via `python-chess`: legality, mate, stalemate, auto-draws, threefold/50-move claims, resign, draw offers, flag (timeout) wins
- Unicode board UI in TG DM, typed SAN moves (`e4`, `Nf3`, `O-O`)
- Settlement: pot − rake → winner, rake → fee wallet; draws split 50/50 − rake
- Per-move on-chain anchor tx (payload = match/ply/move/board-hash) with tiny fee — the "pay per move" + immutable PGN on the blockDAG
- trade_guard-style rails: kill switch, idempotency, caps, owner gate

**OUT (next phases, sketched at the end):**
- Tournaments (v1.1 — bot-run bracket over the same match engine, ~1 day of work once 1v1 is solid)
- Tap-grid move input, PNG board cards
- Trustless covenant chess-clock (Silverscript v1 / testnet-10 first), ZK legality disputes (KIP-16 / vprogs)

**Trust model v0 (be honest in the docs/UX):** Dagger wallets are custodial, and the bot is the referee. The escrow is real on-chain 2-of-3 and the reclaim branch is real trust-minimization against server loss, but game adjudication is centralized in v0. That's the same trust level as every other Dagger feature — say so, don't oversell.

---

## 1. Architecture

```
Telegram (both players DM the bot)
   │
   ├─ dagger-bot/chess.py            NEW — match engine, board UI, clock, DB
   │     uses python-chess for all rules
   │     uses trade_guard idempotency + limits for all money movement
   │
   └─ kron-service (sidecar :8791)   +4 small routes
         /chess/pubkey        — schnorr pubkey for a wallet index / arbiter index
         /chess/escrow        — build per-player escrow address (script + addr)
         /chess/settle        — spend both escrow UTXOOs → winner + rake  (2-of-3 sign)
         /chess/anchor        — dust tx with DGCHS payload from mover's wallet
Chain: Kaspa mainnet. Escrow addrs are watched with the existing UTXO polling
(get_utxos in kaspa_client.py) — no new indexer needed.
```

---

## 2. Escrow design (the on-chain part)

### 2.1 Keys

- **Player keys:** each player's existing Dagger wallet key (schnorr, HD `receiveKey(wallet_index(user_id))`). ⚠️ HD-index pin rule applies — never re-derive differently.
- **Arbiter key:** per-match, from a **separate HD account** so it can never collide with user indices:
  `new PrivateKeyGenerator(xprv, false, 1n).receiveKey(match_id)`  ← account **1n** (users are on 0n). One key per match limits blast radius and makes match audit trails clean.

### 2.2 Script — one escrow address **per player** per match

Two branches, hand-built with `ScriptBuilder` (same COVENANT_OPTS signing path kron.js already uses for KRON covenants):

```
OP_IF
    OP_2 <pk_playerA> <pk_playerB> <pk_arbiter> OP_3 OP_CHECKMULTISIG   # settle: any 2 of 3
OP_ELSE
    <reclaim_locktime> OP_CHECKLOCKTIMEVERIFY OP_DROP
    <pk_depositor> OP_CHECKSIG                                          # depositor reclaims after 14d
OP_ENDIF
```

P2SH address = standard Kaspa pay-to-script-hash over the redeem script.

Why per-player addresses (not one shared pot address): the CLTV branch names **that player's own key**, so an abandoned match degrades to "everyone reclaims their own stake, unilaterally, no arbiter needed." A shared address can't distinguish depositors without covenant introspection — that's the v2 upgrade, not this weekend.

- `reclaim_locktime` = current DAA score + ~14 days of DAA (Kaspa = 10 blocks/sec → `14*24*3600*10` ≈ 12,096,000). **Spike item S3 confirms the exact locktime encoding** (DAA-score domain vs the unix-ms domain — Kaspa locktime is u64 with a threshold split like Bitcoin's; verify against rusty-kaspa-ref before trusting it, `dagger/rusty-kaspa-ref` is checked out locally).

### 2.3 Flows

**Deposit** — stake is pulled from each player's Dagger balance with a guarded internal transfer (wallet → own escrow addr), idem key `chess:<match_id>:dep:<user_id>`. External top-up to the shown address also works (address is displayed with the usual copy card). Watcher confirms both UTXOs ≥ stake → match goes LIVE.

**Settle (normal)** — one tx spending BOTH escrow UTXOs via the IF branch:
- inputs: escrow_A utxo(s) + escrow_B utxo(s), each unlock = `<sig_arbiter> <sig_winner> OP_TRUE <redeem>` (2 sigs + branch selector; exact stack order per OP_CHECKMULTISIG semantics — spike S2 nails it with a dust tx)
- outputs: `pot − rake − txfee → winner`, `rake → fee wallet`
- both required privkeys (winner + arbiter) are custodially derivable, so settlement is automatic on game end.
- Draw: two outputs, `(pot − rake)/2` to each.

**Abort/refund** (opponent never funds, or mutual abort before move 2): arbiter + depositor co-sign each escrow back to its owner. Rake **not** taken on aborts; anchor fees are not refunded.

**Disaster path** (VPS gone): after 14 days each player sweeps their own escrow via the ELSE branch with any wallet that can sign a custom script. Ship `dagger/tools/chess_reclaim.js` (20 lines against the sidecar's kron.js helpers) so this is provably real — that script existing IS the marketing line ("your stake is recoverable even if Dagger vanishes").

---

## 3. Sidecar additions (`kron-service/src/`)

New file `chess.js`, mounted in server.js. All routes owner-token-gated the same way as the existing money routes.

```js
// GET  /chess/pubkey?index=<n>&account=<0|1>
//   → { pubkey }              // schnorr x-only/compressed as ScriptBuilder needs it
// POST /chess/escrow  { matchId, pkA, pkB, depositorIsA, reclaimDaa }
//   → { address, redeemHex, arbiterIndex }        // pure function, no chain calls
// POST /chess/settle  { matchId, escrows:[{address,redeemHex,depositorIndex}],
//                       winnerIndex|split:{aIndex,bIndex}, rakeSompi, submit }
//   → { txid, potSompi, rakeSompi }
//   // gathers UTXOs for both addresses, builds one tx, signs each input with
//   // arbiter key (account 1, receiveKey(matchId)) + winner/depositor key,
//   // assembles <sigArb> <sigWin> TRUE <redeem> unlocks, submits.
//   // submit:false → dry-run returning the summary (use for the confirm screen)
// POST /chess/anchor  { index, payloadHex, feeSompi }
//   → { txid }
//   // dust self-tx (or to fee wallet if feeSompi>0) from wallet[index]
//   // carrying `payload` — createTransactions({ ..., payload }) supports this;
//   // spike S1 confirms on this SDK version (0.17.0).
```

Reuse: `createTransactions` + submit path from `withdraw()`, custom-redeem signing from the existing covenant sign path (the `ScriptBuilder.fromScript(scriptHex, COVENANT_OPTS)` block). Nothing new conceptually — the covenant trades already sign non-standard scripts daily.

---

## 4. Bot module (`dagger-bot/chess.py`)

### 4.1 Dependencies

`requirements.txt`: add `chess` (python-chess, pure python, no system deps — safe on the VPS).

### 4.2 DB (new tables, migration in database.py style)

```sql
CREATE TABLE IF NOT EXISTS chess_matches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  challenger_id INTEGER NOT NULL, opponent_id INTEGER NOT NULL,
  challenger_white INTEGER NOT NULL,          -- coin flip at accept
  stake_kas REAL NOT NULL, mode TEXT NOT NULL,          -- 'rapid' | 'daily'
  state TEXT NOT NULL,   -- OPEN|FUNDING|LIVE|SETTLING|SETTLED|ABORTED|REFUNDED|DECLINED|EXPIRED
  fen TEXT NOT NULL, move_deadline_ts INTEGER,
  clock_w_ms INTEGER, clock_b_ms INTEGER,               -- rapid mode remaining time
  escrow_a TEXT, escrow_b TEXT, redeem_a TEXT, redeem_b TEXT,
  arbiter_index INTEGER, reclaim_daa INTEGER,
  result TEXT, result_reason TEXT,                      -- '1-0','0-1','1/2' + mate|resign|flag|agree|stalemate|...
  settle_txid TEXT, anchors_on INTEGER DEFAULT 1,
  created_ts INTEGER, ended_ts INTEGER
);
CREATE TABLE IF NOT EXISTS chess_moves (
  match_id INTEGER, ply INTEGER, uci TEXT, san TEXT, fen_after TEXT,
  moved_ts INTEGER, anchor_txid TEXT,
  PRIMARY KEY (match_id, ply)
);
```

⚠️ Access rows the way the live handlers do (`sqlite3.Row`) and test with that exact type — the `/orders` lesson.

### 4.3 Commands & flow

- `/chess` — menu: New challenge · My games · How it works
- `/chess @user 50` / `/chess @user 50 rapid` — challenge (daily default). 0 stake allowed → skips escrow entirely, straight to LIVE.
- Opponent gets DM: stake, mode, rake %, [♟ Accept] [✖ Decline]. Accept → colors coin-flipped → FUNDING: both sides get a one-tap **[Stake 50 KAS]** button (guarded transfer to own escrow) + the escrow address for external funding. Challenge expires in 24h unfunded → auto-refund whatever landed.
- LIVE: each player's DM shows the board + status line (stake, clocks, whose move). **Moves are typed SAN or UCI as a plain DM message** while it's your turn (`e4`, `Nf3`, `O-O`, `e7e8q`). Illegal/ambiguous input → ephemeral error, no state change. Buttons under the board: [🏳 Resign] (with confirm) · [½ Offer draw] · [🔄 Refresh] · claim buttons for threefold/50-move when python-chess says they're available.
- Board render: `<pre>` Unicode board, flipped to each viewer's color, coordinates on both axes, last move highlighted with `·` markers. (PNG cards = stretch; if built, **render on the VPS**, never Windows — font rule.)
- `/chessgames` — list my active/recent matches. Owner: `/chessstats`.

Routing note: bot.py already routes free-text (sell amounts etc.) — chess move parsing must only claim a message when the sender has exactly one match where it's their turn, else ask which match via buttons. Never swallow text that another handler owns; hook in after the existing text handlers, not before.

### 4.4 Clock / flag rules

- `daily`: 24h/move (`move_deadline_ts`). Miss it → flag loss.
- `rapid`: 10 min + 5 s increment per side, decremented server-side between moves (`clock_w_ms/clock_b_ms`); deadline = now + remaining. Both players must be "at the board" — accept flow warns rapid needs both online.
- One asyncio watcher task (register alongside `deposit_watch_loop`) scans LIVE matches every 30 s: flag → settle as win; FUNDING past expiry → refund; nudge DM at T−1h for daily.
- First 2 plies are grace: if flagged before each side has moved once → ABORT + refund instead of a win (stops instant-flag griefs on accept).

### 4.5 Game end matrix

| End | Result | Pot |
|---|---|---|
| Checkmate | winner | pot − rake → winner |
| Resign | opponent wins | same |
| Flag (incl. daily timeout) | opponent wins | same |
| Draw: agreement / stalemate / insufficient / 75-move / fivefold / claimed 3-fold / claimed 50-move | ½–½ | (pot − rake) / 2 each |
| Abort (pre-move-2 / unfunded) | none | full refund, no rake |

There is deliberately **no on-chain mate detection**: a mated/stalling player either resigns or flags — chess-native, chain-simple. Illegal moves can't exist (python-chess rejects them before they touch state).

### 4.6 Settlement (money path — full rails)

`execute_chess_settle(match_id)` lives behind trade_guard conventions:
- `with_idempotency(arbiter_uid, f"chess:{match_id}:settle", ...)` — one settle per match, ever; state gate SETTLING→SETTLED in the same transaction pattern as limit-order claims (atomic claim first, then money).
- Kill switch `CHESS_ENABLED=0` freezes new matches AND settlements (refunds/reclaims stay possible).
- `CHESS_MAX_STAKE_KAS` (default 500) caps stakes at challenge time; spend-limit checks apply on the deposit leg like any withdraw.
- Result card DM'd to both (KAS + USD via the shared `usd_str` helper — dual-currency rule): winner, reason, pot, rake, settle txid → KasCov link on the escrow addresses.

### 4.7 Move anchors — the pay-per-move layer

After each accepted move (matches with `anchors_on`, default ON for staked games, OFF for 0-stake):
- fire-and-forget `/chess/anchor` from the **mover's** wallet: `CHESS_MOVE_FEE_KAS` (default **0.1**) → fee wallet, payload:
  `DGCHS|1|<match_id>|<ply>|<uci>|<blake8(fen_after)>`  (version byte first; blake8 = first 8 hex of blake2b of the FEN — enough to pin the position).
- Anchor txid saved on the move row; board footer shows `⛓ ply 24 anchored`. Anchor failure NEVER blocks the game (log + retry once; move stands).
- This is the immutable PGN-on-the-blockDAG + the micro-revenue stream. At 0.1 KAS × ~80 plies it's ~8 KAS/game — priced as flavor, not rent; both knobs are env config.

Fees summary: `CHESS_RAKE_BPS` (default **200** = 2%) + `CHESS_MOVE_FEE_KAS` (0.1). Both land in the existing fee wallet via existing sweep ops.

### 4.8 Gating

`CHESS_PUBLIC=0`: only `OWNER_ID` + `CHESS_ALLOWLIST` (comma'd TG ids) can create/accept. Same pattern as sniper/copyboard gates. Command menu entry added only when enabled (json perms + menu like copyboard did).

---

## 5. Day-0 spikes (Friday evening — de-risk before writing the bot)

All on **mainnet with dust** (≤ 2 KAS total), from a scratch script in `dagger/tools/`:

- **S1 — payload:** `createTransactions({..., payload})` on SDK 0.17.0 → confirm payload visible on the explorer/API. (Fallback: anchor without payload-dependent features, encode in an extra dust output? No — payload is expected to work; KCC-20 lives on payloads.)
- **S2 — 2-of-3 spend:** build the IF-branch redeem via ScriptBuilder, fund the P2SH addr with 1 KAS, spend with 2 sigs. Nails: pubkey encoding, CHECKMULTISIG stack order/dummy-element behavior on Kaspa, sighash reuse from the covenant sign path. **This is the only genuinely unknown unknown — do it first.**
- **S3 — CLTV branch:** fund a second dust escrow with `reclaim_daa = current + 600` (~1 min), confirm ELSE-branch spend fails before and succeeds after. Verify locktime domain against `rusty-kaspa-ref` opcodes first.
- **S4 — pubkey route:** wallet.py's derived address == sidecar `receiveKey(index)` address for a test index (should already hold; assert it).

If S2/S3 fight back hard: **fallback escrow v0** = per-match arbiter-key address (account-1 single-sig, per-match key, everything else identical). Ship the weekend on that, land the 2-of-3 as a fast-follow. The bot layer doesn't change at all — escrow builder is one swappable function.

### 5.1 Spike results (2026-08-22, all on mainnet dust) — confirmed rules for the real escrow builder

**S1 (payload), S2 (2-of-3 spend), S3 (CLTV reclaim) all PASSED on mainnet.** S4 (pubkey parity) was dropped as unneeded — production signing only ever goes through the sidecar's own `deriveUser()`; wallet.py's address derivation is legacy/unused for real spends, so there's nothing to cross-check.

Hard-won Kaspa script rules, all real consensus behavior (not SDK quirks) — bake these into `chess.js`'s escrow/settle builders or they will silently reject on mainnet:

1. **`sigOpCount` is a `createTransactions()`/`createTransaction()` option, not a post-hoc field.** Set it at creation time. Underfunding it → `script units exceeded the amount committed in the input`.
2. **CHECKMULTISIG is billed by pubkey count (n), not required-sig count (m).** A 2-of-3 script needs `sigOpCount: 3`, not `2` — Kaspa (like legacy Bitcoin) can't statically know how many EC checks a lazy-matching CHECKMULTISIG will actually perform, so it bills conservatively for all n keys.
3. **Sig order in the witness must match pubkey order in the script**, not signer/role order. Pubkeys pushed `pkA, pkB, pkArb` → sigs must be pushed `sigA` before `sigArb` (whichever sigs are present, in ascending index order of their matching pubkey), or CHECKMULTISIG fails with `not all signatures empty on failed checkmultisig` (NULLFAIL).
4. **Kaspa's `OpCheckMultiSig` has NO Bitcoin-style off-by-one dummy element.** Do not push an extra `OP_0`/`OP_FALSE` before the sigs — it's never consumed and fails the tx as `stack contains 1 unexpected items` (non-clean stack).
5. **Kaspa's `OpCheckLockTimeVerify` POPS the locktime value off the stack** (`vm.dstack.pop_raw()` in rusty-kaspa — unlike Bitcoin's peek-only CLTV). Do **not** add an `OP_DROP` after it — that convention is Bitcoin-only and here it drops the wrong stack item (typically the signature), breaking the subsequent `OpCheckSig`.
6. **`PendingTransaction.transaction` is a snapshot, not a live handle.** Mutating `pendingTx.transaction.lockTime` / `.inputs[i].sequence` does **not** persist into what `.submit()` actually sends. For anything needing a custom `lockTime` or `sequence` (i.e. the CLTV reclaim path), build the tx with the low-level `k.createTransaction(entries, outputs, priorityFee, payload?, sigOpCount?)` (returns a real `Transaction`, no auto change output — size outputs to consume the full input, mind the leftover-becomes-fee behavior), mutate it directly, sign with the module-level `k.createInputSignature(txn, idx, key)`, and submit via `rpc.submitTransaction({ transaction: txn, allowOrphan: false })`.
7. **CLTV enforcement is two separate checks, not one:** the in-script `OpCheckLockTimeVerify` opcode only verifies `stack_value <= tx.lock_time` (i.e. the spender can't lie about which threshold they're claiming) — the actual "can't be mined before real time/DAA passes" gate is `check_tx_is_finalized` (consensus, block-inclusion-time, `tx_validation_in_header_context.rs`), which compares `tx.lock_time` against the **including block's real DAA score**. Both require `input.sequence != MAX_TX_IN_SEQUENCE_NUM` to even engage (else CLTV auto-rejects with "transaction input is finalized").
8. **Test-harness gotcha, not a protocol gotcha:** an "early reclaim" test needs a REAL margin (we used 60s / 600 DAA-score) between "now" and the reclaim deadline — `fundFromFeeWallet` + `waitUtxo`'s polling alone can burn 10–30s, so a tight margin (e.g. 2s) makes the "expect REJECT" case false-pass because real chain time has already caught up by the time the attempt runs. For the real 14-day chess reclaim window this is a non-issue; worth remembering for any future spike/test scripts.

Working spike code (reference implementation for all of the above): `kron-service/src/chess_spike.mjs`.

## 6. Build order

**Fri eve:** spikes S1–S4 → `/chess/pubkey` + `/chess/escrow` + `/chess/anchor` routes.
**Sat:** chess.py — DB migration, challenge/accept/funding flow, board UI + SAN input, clock watcher, resign/draw; `_t_chess_rules.py` (mate, stalemate, flag, claims, illegal input, grace-abort) + `_t_chess_flow.py` (state machine on real sqlite3.Row).
**Sun:** `/chess/settle` + `execute_chess_settle` + refunds + result cards + anchors wired in; `_t_chess_escrow.py` (mainnet dust E2E), `_t_chess_guard.py` (idem replay, kill switch, stake cap); deploy → **owner E2E**: GoonBoy vs FullFace (or second account), 5-KAS stake, daily mode, real mate, verify settle txid + rake on KasCov, then a rapid game with a deliberate flag, then an abort-refund. Update TG user guide (auto-regen rule) — feature is GoonBoy-gated so guide note can wait for public flip.

## 6.5 Post-core, same-weekend-if-time (GoonBoy 2026-08-21): UI upgrade + teaching portal

GoonBoy's reference is the lichess-style graphical board (green/cream squares, cartoon piece set). Order of work AFTER the core E2E passes:

1. **Board UI upgrade:** replace/augment the Unicode board with a PNG per position — `chess.svg.board(board, lastmove=…, orientation=…, colors={green/cream})` → cairosvg → PNG, sent as a photo with the action buttons underneath. ⚠️ Render on the **VPS only** (Windows font trap rule applies to any text overlays); add `cairosvg` to requirements and confirm its native deps (cairo) install cleanly on the VPS before committing to it. Piece theme: python-chess ships merida-style SVG pieces; close enough to the reference — custom set later.
2. **Interactive web board (stretch):** daggertrade.com/chess or Mini App page with a tap-to-move board mirroring the DM game (reuse miniapp auth). Watch licensing if importing a board widget (chessground is GPL-3 — prefer self-built or MIT).
3. **Teaching portal (`/learn`)** — for complete non-players, Dagger-branded:
   - Bot-side lesson track: guided interactive lessons (how each piece moves → check/mate ideas → a full guided mini-game), each lesson = position PNG + tap/typed answers, progress saved per user; small points reward on completion (existing points system) as the hook.
   - Practice vs bot: stockfish on the VPS at very low skill levels via python-chess's engine API (`apt install stockfish`), free, no escrow — also serves as matchmaking practice before wagering.
   - Web mirror of the lessons on daggertrade.com later.
   - Funnel logic: learn → practice vs bot → 0-stake friendly → wagered match.

## 7. Later phases (park, don't build)

- **v1.1 Tournaments:** `/chessopen <entry> <size>` — entry fees to one arbiter escrow, bot runs single-elim bracket of normal matches (0-stake, pot from pool), payout 70/20/10 − rake. Pure bot logic on top of this engine.
- **v2 Covenant chess-clock:** stateful covenant (Toccata covenant IDs) holds pot + board-hash + deadline; mutual-signed off-chain states, on-chain flag claims — trustless referee. Testnet-10 under Silverscript now; mainnet at Silverscript v1. KCC-0402 vouchers = per-move billing without dust txs.
- **v3 ZK disputes:** RISC0/Groth16 "legal successor" proof verified in-script (KIP-16 active on mainnet); look at `vprogs`. The "provably fair on-chain chess, sub-second moves, only on Kaspa" headline.
- Compliance note before `CHESS_PUBLIC=1`: wagering real money on skill games is jurisdiction-sensitive — sanity-check before opening past the allowlist.
