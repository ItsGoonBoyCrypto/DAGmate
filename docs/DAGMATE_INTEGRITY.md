# DAGmate — Competitive Integrity: ratings, anti-cheat & anti-Sybil

Design proposal, 2026-09-12. Prompted by community feedback on the launch post:
1. *"Anti-bot measures? Elo-based matchmaking?"*
2. *"AI could do a post-game look-back for cheating — release funds to the winner only if the
   anti-cheat score is below 50%, otherwise funds go back to each player."*
3. (Liam) *"We need a leaderboard — competitiveness, all based on a wallet's matches, score, etc."*

This doc is grounded in how the real platforms actually do this (Lichess Irwin/Kaladin, Chess.com Fair
Play, Ken Regan's FIDE model, TrueSkill/Glicko, poker & skill-gaming dispute handling). Sources at the end.

---

## TL;DR / recommendation

- **Leaderboard + ratings: build now.** Use **Glicko-2** (what Lichess runs; best for a small pool where
  people play infrequently). Rank the board by a **conservative score `rating − 2·RD`**, not raw rating,
  and require a **minimum number of rated games** before a wallet appears. This one choice also happens to
  be the cheapest, strongest brake on smurfs and Sybils.
- **The "AI auto-refunds if cheat-score > 50%" idea is the right instinct but the wrong mechanism** — as
  literally described it would misfire on real money. **No serious platform makes a cheating verdict from a
  single game**, because a legit player having one brilliant (or forced) game is a textbook false positive.
  A per-game automatic money-gate would (a) void honest winners' payouts on false positives, and (b) be
  *griefable* — a losing player would deliberately trip the flag to escape the loss.
- **The sound version of the same idea:** pay out on the game result in the normal case; only a
  **high-confidence** flag moves the pot into **escrow-and-hold for review** (never an automatic refund);
  on a **confirmed** cheat (reviewed, high bar) **forfeit the cheat's stake to the honest opponent** and
  ban the wallet. This fits DAGmate's existing non-custodial escrow perfectly — a "hold" is just the oracle
  declining to sign the winner's settle yet; the funds stay locked and reclaimable.
- **Anti-bot/Sybil: you can't make wallets scarce, so make *fake* accounts unprofitable.** The stake +
  per-game fee already do most of the work (self-play/wash-matches bleed money). Add provisional-rating
  quarantine, minimum-games gates, velocity limits, and on-chain funding-graph clustering (Kaspa's
  transparency is an asset here). No KYC required.

Suggested order: **Phase 0** (instrument move times + cheap deterrents) → **Phase 1** (Glicko-2 + leaderboard
+ rating-banded matchmaking) → **Phase 2** (Stockfish-based detection + escrow-hold review) → **Phase 3**
(optional hardening).

---

## Part A — Ratings, matchmaking & leaderboard

### Why Glicko-2 (not plain Elo)
Plain Elo stores only a number and has no idea how *confident* it is. Glicko-2 stores three things per
wallet — **rating `r`, rating deviation `RD` (uncertainty), and volatility `σ`** — which gives us:
- **Fast, fair convergence for new/sparse players** (most of our players, on a young platform): an
  uncertain rating moves in big steps, an established one in small steps, automatically.
- **Idle handling:** RD grows over real time, so a returning wallet is correctly treated as "less certain"
  again. Elo can't do this.
- It's the **Lichess** system — proven at chess, 1v1, exactly our case.

(TrueSkill is the alternative and is better *only* if we later add team/N-player arena modes. Keep it in
reserve; Glicko-2 for 1v1.)

### Parameters
- Seeds (unrated wallet): `r = 1500`, `RD = 350`, `σ = 0.06`.
- System constant **`τ = 0.4`** (range 0.3–0.5). Lower = more stable, resists a couple of upsets swinging a
  money-relevant rating — the money-safe choice.
- RD bounds: floor ~40 (so established players stay adjustable), cap 350 (= "effectively unrated again").
- **Rating period:** Glicko-2 assumes several games per period (target ~10–15 games/player). On a small
  platform that means a *longer* wall-clock period — start with **weekly batch updates**, or treat each
  game as a mini-period if volume is very low. (Full equations: see Glickman's spec; we port the standard
  algorithm — no need to reinvent it.)

### Provisional ratings & the leaderboard
- While `RD` is high, the rating is **provisional** — show it with a `?` and **keep the wallet off the
  leaderboard**.
- **Rank the leaderboard by `r − 2·RD`, not `r`.** This is TrueSkill's `μ − 3σ` insight: "we're ~95% sure
  the wallet is at least this good." A lucky newcomer with a huge but *uncertain* rating is held down until
  they've actually proven it over many games — which is also precisely what stops a smurf topping the board.
- **Minimum games gate** (e.g. ≥ 20–30 rated games) before a wallet lists at all.
- **Rating floor** (USCF-style) to blunt sandbagging (deliberately losing to farm weaker/cheaper opponents
  — a real incentive when money's involved).

### Matchmaking (the "Elo-based matchmaking" ask)
For **open** challenges, match by **rating band + stake tier** so you play someone near your level for a
comparable stake. This makes the game feel fair *and* caps how much of the population a smurf can prey on
(they can only reach their current band). Direct wallet-to-wallet challenges stay unrestricted (you chose
your opponent).

### Leaderboard data (all per wallet, from data we already store + the new rating fields)
Matches played, W/L/D, current rating (+ `?` if provisional), `r − 2·RD` (rank key), win-rate, net KAS
won, current streak. Public page + `/api/leaderboard`. Optionally show KNS `.kas` names where set.

---

## Part B — Anti-cheat (engine assistance)

### The hard truth from the research
Every serious platform treats **single-game detection as statistically unsafe** and keeps a **human in the
loop before consequences**:
- **Lichess** runs two detectors (Irwin, engine-analysis-based; Kaladin, behavior-based) that assess
  **players across many games**, not single games; all appeals go to humans.
- **Chess.com** combines "100+ factors," auto-bans only when a result is *extremely improbable*, holds a
  **99.99% certainty bar** ("willing to go to court"), and human-reviews the rest.
- **Ken Regan / FIDE** use a conservative `z ≈ 4.5` (~1 in 300,000) threshold on **aggregated** data. Regan:
  *"from 10 moves you are not going to get anything solid."*
- **Peer-reviewed proof of the danger:** a study of 120,000 games played *before* strong engines existed
  found a Regan-style detector would have **falsely flagged ≥ 92 players**. One brilliant game ≠ cheating.

**Implication for the community's idea:** a per-game, automatic "cheat-score → refund both" gate would
produce false accusations at a rate that's unacceptable when it's cancelling real payouts, and it's
**directly griefable** — if a losing player can trip the flag to void the match, you've built a free
"undo my wager" button. It's also **threshold-gameable** (cheat just under X).

### The sound model (what real money-games actually do)
Documented pattern across poker, skill-gaming and the one on-point crypto-chess precedent (ChessBit):
1. **Default:** pay out on the game result immediately (keeps the trustless, instant-settle UX).
2. **High-confidence flag only → escrow-and-hold:** the pot is *held*, not refunded, pending review. In
   DAGmate terms this is trivially clean: the **oracle simply doesn't sign the winner's settle yet.** Funds
   stay in the non-custodial escrow; the reclaim timelock still protects both players if review stalls.
3. **Human review** (Liam, via an admin view) before any money moves. Never automatic on suspicion.
4. **On a *confirmed* cheat:** **forfeit the cheat's stake to the honest opponent** (poker's
   victim-compensation model) + ban the wallet + mark it on the leaderboard/ratings. On a clear: release to
   the winner as normal.
5. **Flags never move money by themselves, and a player can never self-trigger a refund** — a flag opens an
   investigation, nothing more.

This keeps the community's good instinct — *cheaters don't get paid, and the system decides, not vibes* —
without wiring real money to a noisy score.

### Detection signals (Phase 2, needs Stockfish server-side)
Computed post-game from `moves_json` + the new per-ply times, with a multi-PV Stockfish pass (fixed
**nodes**, e.g. Irwin's 4.5M/move, for reproducibility in a dispute):
- **Engine top-N match %**, normalized by position *forcedness/ambiguity* (a forced only-move is weak
  evidence).
- **Win-probability loss** per move (Lichess/Regan prefer this to raw centipawns) + its **variance**
  (cheaters are unnaturally consistent).
- **Time vs. complexity** (using Phase-0 times): playing a hard only-move as fast as a trivial recapture is
  a strong tell; flat time signatures matter as much as means.
- **Only-move find rate**, **opening-deviation point**, **accuracy-vs-rating anomaly** (the aggregate metric).

Crucially, **accumulate a per-wallet score across games** — that's the statistically valid unit — and use a
single game's score only as a *flag trigger*, never a verdict. (Note: a ZK/on-chain proof can verify the
moves were *legal and consistent*, but **cannot** detect a human quietly consulting Stockfish — engine
detection is inherently statistical and off-chain.)

---

## Part C — Anti-bot / Sybil / smurf resistance

A Kaspa address is free, so identity isn't scarce. The goal is to make a *useful fake* **unprofitable**:
- **Economic friction (our natural moat):** every account that affects ratings/economy must **stake real
  KAS**, and every match takes a **fee/rake** — so self-play/wash-matches to pump a rating *bleed money per
  game*. Free-to-play games don't have this; we do.
- **Provisional-RD quarantine:** while a new wallet's RD is high — keep it off the leaderboard, optionally
  cap its stake size, and don't let its results heavily move established opponents' ratings. Caps the damage
  a fresh account can do before it's proven.
- **Minimum-games gate** before leaderboard / high-stakes tiers (also the smurf brake from Part A).
- **On-chain funding-graph clustering:** Kaspa's transparent ledger lets us flag wallets that share a
  funding source or only ever play each other (collusion / wash matches). This is a *crypto advantage*, not
  a limitation.
- **Velocity limits:** cap account-creation and rating-climb rate per device/IP; treat device/IP collisions
  as a *weighted signal* (our users use VPNs), not proof.
- **Optional, no-KYC identity for high-stakes tiers only:** a **Human Passport / BrightID** humanity-score
  gate. Reserve biometric proof-of-personhood (Worldcoin) as an opt-in high-trust badge, never the default.

You will **not** get perfect uniqueness without KYC — and that's fine. Design so fakes are unprofitable,
not impossible.

---

## Part D — Phased plan

**Phase 0 — instrument + cheap deterrents (small, low-risk, no engine).**
- Record **per-ply elapsed time** (extend `moves_json` entries or a parallel array). This unlocks all timing
  analysis later; it must be captured *now* or the data is gone.
- Self-play / collusion guards: same-pair repeat detection, on-chain shared-funding clustering, account
  velocity limits. Pure backend + Kaspa graph.

**Phase 1 — ratings + leaderboard + matchmaking (the quick win, high player value, independent of anti-cheat).**
- Glicko-2 fields on `accounts`; update job per rating period; provisional flag; rating floor.
- `/api/leaderboard` ranked by `r − 2·RD` + min-games gate; public leaderboard page; per-wallet W/L/score/net-KAS.
- Rating-banded matchmaking for open challenges.
- *Ships fast, visible, competitive — and doubles as the first anti-smurf layer.*

**Phase 2 — engine-assisted detection + escrow-hold review (the real anti-engine layer; bigger).**
- Stockfish analysis worker (box or sidecar); per-game feature extraction; per-wallet aggregate cheat score.
- New match state `held_review`: on a high-confidence flag the oracle withholds the winner's settle; funds
  stay escrowed; admin review UI for Liam; confirm → forfeit-to-victim + ban; clear → release.
- Aggregate score also gates leaderboard eligibility / high-stakes tiers (soft, non-money consequence).

**Phase 3 — optional hardening.**
- Human Passport / BrightID gate for high-stakes tiers; slashable good-behavior bond; funding-graph dashboards.

---

## Decisions — CONFIRMED by Liam 2026-09-12
1. ✅ **Anti-cheat money model:** escrow-and-hold-on-high-confidence-flag + human review +
   forfeit-to-victim-on-confirm — NOT the literal per-game auto-refund.
2. ✅ **Build order:** Phase 0 + Phase 1 (leaderboard/ratings/matchmaking) first, Phase 2 (detection) next.
3. ✅ **Confirmed-cheat payout:** forfeit the cheat's stake to the victim **only if it can be proven**, and
   **both players get a clear message explaining the decision** (the cheat: why they were penalised; the
   victim: that they were compensated and why). → STANDING RULE: **every integrity action — hold, release,
   forfeit, ban — carries a plain-English explanation to the affected players.**
4. ✅ **Reviewer:** Liam-only admin panel to start.

---

## Sources
Cheat detection: Lichess Irwin (`github.com/clarkerubber/irwin`) + Kaladin (`github.com/lichess-org/kaladin`);
Lichess two-agent writeup; Chess.com Fair Play (`chess.com/cheating`); Regan interview (chess.com/blog);
Oxera "limits of algorithmic detection"; Barnes & Hernández-Castro, *Computers & Security* 2015 (92/120k
false positives). Ratings: Glickman Glicko-2 spec (`glicko.net/glicko/glicko2.pdf`); TrueSkill (Microsoft
Research); Dehpanah et al. (arXiv 2008.06787). Money-game dispute handling: ChessBit (escrow+review);
ChessStake (instant, no gate); PokerStars victim refunds (GPWA); Skillz ToS (seizure) + fraud suit;
Riot LP restoration; Bugnet/arXiv on false-positive cost. Sybil: Formo (crypto Sybil); Human Passport;
Ghost Trilemma (arXiv 2308.02202); Smogon Glicko-anti-smurf; Turbosmurfs (Valorant new-account detection).
