# DAGmate Expansion Spec — Draughts & Backgammon on Kaspa L1

**Status:** v1.0 (implementation-ready). Post-launch effort — build after chess is live
on mainnet and has real players. No rush; multi-week, one small PR per milestone.
**Ground truth:** `REUSE.md` (this repo). **Predecessor:** the v0.1 sketch
`~/Downloads/dagmate-draughts-backgammon-spec.md` (superseded by this document).
**Author's note:** this spec is grounded in the ACTUAL repo — Python/FastAPI + a Node
Kaspa sidecar + a polling vanilla-JS frontend + 2-of-3 arbiter-co-signed escrow. It
deliberately corrects the v0.1 sketch, which was written against a generic
TypeScript/WebSocket/oracle-hash mental model DAGmate does not use.

---

## 0. Ground rules (carried from v0.1 §0, updated)

1. **Read before writing.** `REUSE.md` is the mandatory prerequisite and is done.
2. **Never touch the live chess path.** No edit to `chess_logic.py`, and no behavioural
   change to `make_move`, settlement, deposits, or the bracket that a chess game can
   observe. The dispatch refactor (§2) must leave `game='chess'` byte-for-byte
   equivalent — proven by the existing chess tests staying green untouched.
3. **Tests-first, small PRs.** One milestone (§8) = one PR = its own test file, same
   shape as `tools/test_tournament.py` / `test_settlement.py` (dependency-free, real
   schema, throwaway DB).
4. **Testnet-first.** Every game proven on testnet-10 with two real wallets before it
   is offered on mainnet, exactly as chess was.
5. **Same site, same wallet, same money path.** One `dagmate.org`, one login, one
   escrow/settlement/reclaim system. A player picks the game; everything downstream is
   the code that already runs.
6. **Ship draughts first.** It is a pure perfect-information board game — a near-exact
   structural twin of chess (§1). Backgammon adds dice (randomness + provable fairness),
   so it comes second and carries all of §4's extra machinery.

---

## 1. Feasibility (grounded)

| Game | Fit to DAGmate | Risk | Rel. effort | Why |
|------|----------------|------|-------------|-----|
| **Draughts** (English/American checkers, 8×8) | ~100% | Low | ~1× | Perfect information, two players, deterministic, alternating turns, clock-compatible, decisive-or-draw. Structurally identical to chess; only the rules module + board glyphs differ. |
| **Backgammon** | ~85% | Medium | ~2.5–3× | Two players, alternating turns, decisive (no draws) — all good. The hard part is **dice**: hidden randomness that must be *provably fair* and *tamper-evident* so neither player (nor the server) can cheat the roll. That is the only genuinely new subsystem. |

Everything on the money side is already proven and game-blind (see `REUSE.md` §§1–4,
9, 11). The expansion's entire technical weight is: (a) a game-engine seam, (b) two
rules modules, (c) provably-fair dice for backgammon, (d) two board renderers.

---

## 2. The GameEngine seam (the one architectural change)

### 2.1 Interface
A game is a Python module in `site/backend/games/` exposing a fixed, stateless-by-
convention interface. No engine holds mutable state; state lives in the DB (`matches`
row) exactly as chess's FEN does today.

```python
# site/backend/games/base.py  (protocol — documentation, not enforced at runtime)
class GameEngine(Protocol):
    NAME: str                      # 'chess' | 'draughts' | 'backgammon'
    DRAWS_POSSIBLE: bool           # chess/draughts True, backgammon False
    IS_STOCHASTIC: bool            # backgammon True, others False
    SIDES: tuple[str, str]         # ('white','black') — reuses the clock/turn columns

    def initial_state(self, *, seed: dict | None = None) -> "GameState": ...
    #   seed carries the first dice commitment for backgammon; None otherwise.

    def legal_moves(self, state: "GameState") -> list[str]: ...
    #   opaque move tokens (chess: UCI 'e2e4'; draughts: 'b6c5'/'b6d4x'; backgammon:
    #   an encoded full turn like '8/5 6/5' or 'bar/23 13/8').

    def apply_move(self, state, move, history) -> "GameState": ...
    #   raises IllegalMove on anything not in legal_moves; history is the JSON list
    #   already stored (needed by chess repetition + backgammon dice sequencing).

    def outcome(self, state) -> "Outcome":  ...
    #   -> Outcome(over: bool, winner_side: str|None, result: str, magnitude: int)
    #   magnitude = backgammon gammon/backgammon multiplier (1/2/3); 1 elsewhere.

    def timeout_result(self, state, flagged_side) -> "Outcome": ...
    #   chess keeps FIDE 6.9; draughts/backgammon: flag = loss.

    def serialize(self, state) -> dict:   ...   # -> stored as state_json
    def deserialize(self, data: dict) -> "GameState": ...
```

`GameState` is a small dataclass a game defines for itself (board + side-to-move +
game-specific extras like dice). `serialize`/`deserialize` round-trip it through the DB.

### 2.2 Registry & dispatch
```python
# site/backend/games/__init__.py
from . import chess as _chess, draughts as _draughts, backgammon as _bg
ENGINES = {e.NAME: e for e in (_chess, _draughts, _bg)}
def engine_for(game: str) -> GameEngine:
    return ENGINES[game]
```

`chess.py` is a **thin adapter** wrapping the existing `chess_logic.py` — the real chess
code is not rewritten, just presented through the interface. This is what keeps rule 2
honest: chess's behaviour is literally the same functions.

### 2.3 What changes in `main.py`
`make_move` today calls `chess_logic` directly. It becomes:
```python
eng = engine_for(m["game"])
state = eng.deserialize(json.loads(m["state_json"]))
state = eng.apply_move(state, body.move, json.loads(m["moves_json"]))   # raises -> 400
out = eng.outcome(state)
db.apply_move_with_clock(match_id, eng.serialize(state), moves+[body.move],
                         out_turn, mover_side, mover_remaining_ms, now_ms)
if out.over: await _settle_game_over(match_id, out.result, out.winner_side, out.magnitude)
```
`_settle_game_over`, `clocks.forfeit_if_flagged`, and `_replay_tournament_draw` gain a
`magnitude`/engine lookup but keep their control flow. Everything below them
(settlement, deposits, bracket) is untouched.

### 2.4 What changes in the DB
- Add `game TEXT NOT NULL DEFAULT 'chess'` to `matches` and `challenges`.
- Add `state_json TEXT` to `matches`. **Chess keeps writing `fen`**; the chess adapter
  reads/writes `fen` for back-compat, new games use `state_json`. (Decision D1 below can
  instead migrate chess into `state_json`; default recommendation: leave chess on `fen`,
  zero migration risk.)
- `turn` is reused verbatim (`white`/`black` = first/second mover).
- `draw_offer_*` used only when `DRAWS_POSSIBLE`.
- No other table changes except the per-game ELO table (§6).

---

## 3. Draughts engine (`games/draughts.py`)

**Variant:** English draughts / American checkers — 8×8, 12 pieces a side, dark squares
only, the most-recognised ruleset. (Decision D-DR1: not international 10×10; smaller,
more familiar, and every rule below is for the 8×8 game.)

### 3.1 Rules to enforce (server-authoritative)
- Men move one diagonal step forward; capture by jumping an adjacent enemy to the empty
  square beyond, in either diagonal direction.
- **Compulsory capture:** if any capture is available, a capturing move must be played.
  (D-DR2: enforce *a* capture, not the *maximal* capture — American rule. Simpler, and
  matches the casual audience. Note in UI.)
- **Multi-jumps:** a capture that lands adjacent to another capturable enemy must
  continue in the same turn; the move token encodes the full chain (`b6d4f6x`).
- Reaching the far rank crowns a **king**; kings move and capture both diagonal
  directions. A man that crowns *ends* its turn (no continuing as a king that move).
- **Win:** opponent has no legal move (no pieces, or all blocked).
- **Draw:** 40-move rule with no capture and no crowning → draw (D-DR3: adopt a simple
  40-ply-no-progress rule, tracked in state; no repetition table needed).

### 3.2 State
```python
@dataclass
class DraughtsState:
    board: int           # bitboard-friendly encoding over the 32 dark squares
    kings: int           # bitmask of which occupied squares are kings
    side: str            # 'white'|'black'
    since_progress: int  # plies since last capture/crown (for the 40-ply draw)
```
`serialize` → `{"b":..,"k":..,"side":..,"sp":..}`. Compact, integer-only.

### 3.3 Move token grammar
`<from><to>` for a step (`b6c5`); `<from>(<mid>)*<to>x` for a jump chain, `x` suffix
marks a capture, intermediate landing squares listed (`b6d4x`, `b6d4f6x`). `legal_moves`
returns the full enumerated set so the frontend never computes rules.

### 3.4 Tests (`tools/test_draughts.py`)
Perft-style move counts from the start and from crafted positions; compulsory-capture
enforcement (a non-capturing move is rejected when a capture exists); multi-jump chains;
crowning ends the turn; win-by-no-moves; the 40-ply draw. Dependency-free, throwaway DB
only for the one integration check that a draughts match settles a winner through the
existing settlement path.

---

## 4. Backgammon engine (`games/backgammon.py`)

**Rules:** standard 15-checker backgammon — bear-in/off across 24 points, hitting to the
bar, forced bar re-entry, must use both dice if legally possible (and the larger die if
only one can be used), doubles = four moves. **Win:** first to bear off all 15.
**Magnitude:** single (1×), gammon (2×, loser bore off none), backgammon (3×, loser bore
off none and has a checker on the bar or in the winner's home). **No draws**
(`DRAWS_POSSIBLE=False`).

### 4.1 The turn model vs. the polling frontend
Chess = one ply per `/move`. Backgammon = **roll, then move all dice, then pass turn**.
To keep the existing `/move` + polling shape:
- A turn is submitted as **one move token** encoding the full checker sequence for the
  current dice (`bar/23 13/8` etc.). The engine validates the whole sequence against the
  rolled dice in one shot. This keeps one DB write per turn — no new endpoint, no
  partial-turn state to guard against races.
- The **dice for the turn are revealed by the server** at the moment the turn opens (see
  §4a), stored in `state_json`, and returned by the match GET the frontend already polls.
- The frontend renders the revealed dice and lets the player build the sequence locally,
  then POSTs the finished token. Illegal/unused-die sequences are rejected 400.

### 4.2 State
```python
@dataclass
class BackgammonState:
    points: tuple[int, ...]   # 24 signed counts (+white, -black), plus bar/off tallies
    bar: tuple[int, int]      # (white_on_bar, black_on_bar)
    off: tuple[int, int]      # (white_borne_off, black_borne_off)
    side: str
    dice: tuple[int, ...]     # the currently-revealed roll (2 or 4 for doubles)
    commit: str               # HMAC commitment for the NEXT roll (§4a)
    roll_no: int              # monotonic; indexes the dice audit trail
```

## 4a. Provably-fair dice (the one new subsystem)

**Threat:** whoever generates a roll can cheat it. The server must not be trusted to roll
honestly, and neither player may see or influence the roll before committing to their
position. Standard fix: **commit-reveal with HMAC-SHA256**, seeded so the outcome is
fixed in advance but unknowable until revealed, and **anchored on-chain** for tamper-
evidence using the existing `ANCHOR_MOVES` dust-tx mechanism.

**Scheme (server-seed + client-entropy, per match):**
1. At match start the server draws a secret `server_seed` (32 random bytes) and publishes
   `commit_0 = SHA256(server_seed)` into `state_json` and, if anchoring is on, on-chain
   via the DGMT payload (`REUSE.md` §14). Each player contributes a `client_seed` at
   join (their signed login already proves identity; the seed is just a public string).
2. Roll `n` is `HMAC-SHA256(server_seed, f"{match_id}:{client_seed_a}:{client_seed_b}:{n}")`.
   The first bytes map to two (or, on doubles, four) dice via rejection sampling to keep
   the distribution uniform (1–6, no modulo bias).
3. The server reveals dice for turn `n` by publishing the roll; because it is a pure
   function of the pre-committed `server_seed` + both public client seeds + the turn
   index, **no party can have chosen it after seeing the board.**
4. **Verification:** at match end the server reveals `server_seed`; anyone can recompute
   every roll and check it against `commit_0` (and the anchored hash, if enabled). A
   mismatch is public, on-chain proof of a rigged game.

**Where it lives:**
- `server_seed` and the reveal are held by the **Node sidecar** (it already holds secrets
  and does the crypto), exposed as sidecar routes `POST /dice/commit` (returns
  `commit_0`) and `POST /dice/roll {matchId, n}` (returns the roll; only after the prior
  turn is committed). The backend never sees `server_seed` until the reveal — same trust
  posture as the arbiter key.
- The audit trail (each `roll_no`, dice, and the running commitment) is stored in a new
  `dice_log` table and mirrored on-chain when `ANCHOR_MOVES=1`.
- **Reuse, don't reinvent:** the on-chain half is literally `_anchor_move` with a
  `DGMT|{matchId}|dice|{n}|{commit}` payload. No new chain code.

D-BG1 (open): whether to *require* on-chain dice anchoring for real-stake backgammon
(strong tamper-evidence, small per-turn KAS cost from the operating address) or keep it
DB-only with end-of-match reveal (free, still cryptographically sound, weaker public
auditability). **Recommendation:** DB-only by default; offer anchored dice as a labelled
"provably-fair on-chain" match type once the cost is measured on testnet.

## 4b. Doubling cube — DEFERRED to v2
The cube changes the *stake mid-game*, which means re-opening a funded escrow for a
larger amount — a real money-path change (new deposits, new deadlines, new settlement
math). **Out of scope for v1.** v1 backgammon is flat-pot winner-take-all with
gammon/backgammon affecting **rating only, not payout** (§5). The cube gets its own spec
after backgammon is live.

### 4c. Tests (`tools/test_backgammon.py`)
Dice-usage enforcement (must play both dice when possible; must play the larger single
when only one is legal; doubles = four); bar re-entry forced before any other move;
bear-off legality; gammon/backgammon magnitude detection; **and the provably-fair core**:
that `roll(n)` is deterministic from the seed, that `commit_0` verifies the full sequence,
and that a tampered roll fails verification. The dice tests are pure and dependency-free.

---

## 5. Escrow & settlement deltas

- **Draughts:** *zero* deltas. Decisive → winner signs, or draw → split, exactly like
  chess. Tournament replay-on-draw works unchanged.
- **Backgammon v1:** winner takes the **flat pot** (pot − gas), same as a decisive chess
  game. **No draws** → the draw/split settlement path is simply never taken; the draw
  endpoints are not exposed for `game='backgammon'`. Gammon/backgammon **do not change
  the payout** in v1 (that needs the cube / variable stakes) — they only feed rating
  (§6). This keeps backgammon strictly on the already-proven decisive settlement path.
- Reclaim (14-day CLTV) is identical for all games — it is a property of the escrow, not
  the game.

**Net:** the settlement code (`settlement.py`, sidecar `escrow.js`) is not modified by
this expansion at all. That is the whole point of the seam.

---

## 6. Data & leaderboard

- `matches.game` discriminates every row; `matches.state_json` holds new-game state;
  `moves_json` stays the generic history (backgammon stores `{"dice":[..],"move":"..."}`
  entries so a game fully replays).
- **Separate ELO per game.** New table:
  ```sql
  CREATE TABLE IF NOT EXISTS ratings (
    account_id TEXT NOT NULL,
    game TEXT NOT NULL,
    rating INTEGER NOT NULL DEFAULT 1200,
    games_played INTEGER NOT NULL DEFAULT 0,
    updated_ts INTEGER NOT NULL,
    PRIMARY KEY (account_id, game)
  );
  ```
  A chess rating and a draughts rating are independent. Backgammon magnitude scales the
  rating swing (a gammon moves rating more than a single) even though it doesn't move the
  pot. Rating is updated in `_settle_game_over` after the winner is known, best-effort,
  never on the money path.
- **Dice audit:** `dice_log(match_id, roll_no, dice_json, commit, revealed_seed, ts)` —
  written per roll, seed filled at match end, the public record backing §4a.

---

## 7. Testing & rollout

Per game, in order, gated:
1. **Engine unit tests** (perft / dice) — pure, in `tools/`.
2. **DB-integration test** — one match of the new game settles a winner through the real
   settlement accessors on a throwaway DB (same harness as `test_settlement.py`).
3. **Live testnet e2e** — two real wallets, one game played to a decisive result, funds
   settled to the winner and swept back, exactly the harness pattern used for chess and
   the tournament void/bye proof (`systemd-run` Node script driving the real HTTP API).
4. **GoonBoy's 2-wallet browser game** on testnet — the human UX gate, same as chess.
5. Only then: enable the game on mainnet (a config flag listing enabled games; default
   `['chess']`).

Feature-flag: `config.ENABLED_GAMES` (env, default `chess`). A game is invisible in the
UI and rejected by `new_challenge` until listed. This lets each game ship dark, be
tested live, and be turned on independently.

---

## 8. Milestones (one small PR each)

| M | Deliverable | Gate |
|---|-------------|------|
| **M1** | GameEngine seam + chess adapter; `game`/`state_json` columns; dispatch in `make_move`/`_settle_game_over`. **Chess behaviour identical** — all existing chess/tournament tests pass unedited. | No user-visible change. |
| **M2** | `games/draughts.py` + `tools/test_draughts.py` (perft + rules). Engine only, not wired to UI. | Unit tests green. |
| **M3** | Draughts board renderer in `app.js` + `game` picker in challenge/lobby UI; draughts DB-integration + live testnet e2e. | Two-wallet testnet draughts game settles. |
| **M4** | `games/backgammon.py` engine (no dice fairness yet — deterministic test seed) + `tools/test_backgammon.py` rules. | Rules tests green. |
| **M5** | Provably-fair dice (§4a): sidecar commit/reveal routes, `dice_log`, optional `ANCHOR_MOVES` payload, verification test. | Tampered-roll test fails verification; honest sequence verifies. |
| **M6** | Backgammon board renderer + dice UI; backgammon live testnet e2e; per-game `ratings` table + leaderboard surface; enable draughts (then backgammon) on mainnet via `ENABLED_GAMES`. | GoonBoy's 2-wallet browser game per game, then flip the flag. |

Doubling cube (v2) and international 10×10 draughts (if ever) are explicitly **out of
scope** and get their own specs later.

---

## 9. Open decisions (with recommendations)

- **D1 — chess state storage.** Migrate chess into `state_json`, or leave it on `fen`?
  → **Leave on `fen`.** The chess adapter reads/writes `fen`; zero migration, zero risk
  to the live game. New games use `state_json`.
- **D-DR1 — draughts variant.** English 8×8 vs international 10×10? → **English 8×8.**
  More familiar, smaller, every §3 rule targets it.
- **D-DR2 — capture rule.** Any-capture vs maximal-capture? → **Any-capture (American).**
  Simpler, casual-friendly; state it in the UI.
- **D-DR3 — draw rule.** → **40-ply no-progress draw**, tracked in state; no repetition
  table.
- **D-BG1 — dice anchoring.** On-chain per-turn vs DB-only end-reveal? → **DB-only by
  default; offer on-chain "provably-fair" matches** once per-turn cost is measured on
  testnet.
- **D-BG2 — doubling cube.** → **Deferred to v2** (variable-stake escrow is a money-path
  change).
- **D-BG3 — magnitude & payout.** Does a gammon pay more? → **v1: rating only, flat pot.**
  Variable payout waits on the cube spec.

---

## 10. One-paragraph summary
Draughts is a drop-in: a perfect-information twin of chess that needs only a rules module
and a board renderer, reusing the entire money path unchanged. Backgammon is the same
plus one genuinely new subsystem — provably-fair commit-reveal dice (HMAC-SHA256, seed
committed up front, verifiable at the end, optionally anchored on-chain using the dust-tx
mechanism that already exists) — and it deliberately stays on the decisive
winner-takes-pot settlement path by deferring the doubling cube to v2. The only
architectural change to the live system is a thin GameEngine seam that dispatches
`make_move`/`outcome` on a new `game` field; escrow, deposits, settlement, reclaim, and
the tournament bracket are already game-blind and are not touched. Ship draughts, prove
it on testnet then mainnet, then repeat for backgammon.
