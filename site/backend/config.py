"""DAGmate site backend — config (env-only, see docs/DAGMATE_SPEC.md).

Own env vars, own DB, own port. Shares nothing with Dagger. All money-shaped
values (stakes, gas fees, tournament tiers) are handled in KAS with sompi
(1 KAS = 10**8 sompi) as the on-chain unit, same convention as the rest of
the Kaspa ecosystem.
"""
import os

SOMPI_PER_KAS = 100_000_000

DB_PATH = os.getenv("DAGMATE_SITE_DB", "state/dagmate_site.db")

SERVICE_URL = os.getenv("DAGMATE_SERVICE_URL", "http://127.0.0.1:8910")

# Public origin, used to turn an in-app path like /play/<id> into an absolute
# link in a Telegram alert (a bare path isn't clickable there). No trailing
# slash — bot_client joins it to a leading-slash path.
PUBLIC_URL = os.getenv("DAGMATE_PUBLIC_URL", "https://dagmate.org").rstrip("/")

# The bot's public @handle (without the @), e.g. "DAGmateBot". Used ONLY to
# render a "open the bot" link (https://t.me/<handle>) in the optional alerts
# settings — the bot is never required to play. Empty = the site shows no bot
# link (and, with no webhook secret, no alerts UI pressure at all).
BOT_USERNAME = os.getenv("DAGMATE_BOT_USERNAME", "").lstrip("@")

BOT_WEBHOOK_URL = os.getenv("DAGMATE_BOT_WEBHOOK_URL", "http://127.0.0.1:8901")
# Empty = no alerts bot in this deployment (bot_client skips silently). If it IS
# set, it must be long enough to be a real secret — a 4-char value would pass
# the bot's check but is not a secret. Matches the bot's own >=32 requirement.
BOT_WEBHOOK_SECRET = os.getenv("DAGMATE_WEBHOOK_SECRET", "")
if BOT_WEBHOOK_SECRET and len(BOT_WEBHOOK_SECRET) < 32:
    raise RuntimeError("DAGMATE_WEBHOOK_SECRET is set but shorter than 32 chars — "
                       "use a real secret (openssl rand -hex 32) or leave it unset")


# ⚠️ PLATFORM TAKES NO CUT OF A POT — GoonBoy, 2026-08-22: "no fees taken by the
# platform - just gas, entries or challenges charged." The winner receives the
# entire pot minus the Kaspa network fee. What a player pays is the stake they
# agreed (challenge), the entry fee they chose (tournament), or gas — never a
# slice of their winnings.
#
# The rake MECHANISM stays (spec §2.3, service/escrow.js) but is off, so the
# promise is one number in one place rather than a code path that has to be
# remembered. Everything user-facing derives from this same value, so if it is
# ever turned on the UI says so rather than quietly shaving the payout.
RAKE_BPS = int(os.getenv("DAGMATE_RAKE_BPS", "0"))

# CLTV reclaim window, expressed in DAA-score ticks. The escrow's timelock
# branch opens at (funding DAA + this), after which a depositor can sweep their
# OWN stake with their key alone.
#
# It's ~14 days at Kaspa's ~10 blocks/sec cadence — but DAA is a BLOCK count,
# not a wall-clock, so the real elapsed time drifts with the block rate. If BPS
# ever rose, the same tick count would open sooner in wall-clock; if it fell,
# later. That drift is harmless in practice: a decisive game settles (arbiter +
# winner) within seconds of ending, thousands of times faster than even a
# shrunken window, so the timelock never races settlement. The window only has
# to be comfortably longer than "how long until someone gets around to
# settling", and 14 days is. Env-overridable so an operator can retune it if
# Kaspa's cadence ever changes materially — keep it generous.
#
# ⚠️ Disaster-path note: the timelock guarantees each depositor can recover
# their OWN stake, NOT that a winner can collect their winnings. Collecting a
# decisive pot needs the arbiter (sidecar) online to co-sign; if the service is
# permanently gone, the winner can only reclaim their own stake here, not the
# loser's. That's inherent to the 2-of-3 model (see spec §11 for the covenant
# endgame that removes the arbiter).
RECLAIM_DAA_WINDOW = int(os.getenv("DAGMATE_RECLAIM_DAA_WINDOW", str(14 * 24 * 3600 * 10)))

# ── auth (auth.py) ──────────────────────────────────────────────────────
# Connecting a wallet is a CLAIM; signing a nonce with it is the proof. Until
# this landed, every endpoint took the caller's word for which address they
# were, which meant anyone could resign (and so lose) anyone else's match by
# reading their address off the public match view.
#
# Short nonce TTL: the window in which a captured login challenge is worth
# anything at all. Single-use is the real defence (database.consume_nonce);
# this just keeps the table small and the blast radius short.
AUTH_NONCE_TTL_SECS = int(os.getenv("DAGMATE_AUTH_NONCE_TTL_SECS", "300"))
# Session lifetime. Long enough to "stay connected" across days of casual play
# (a single wallet signs in once and stays in for ~3 months), short enough that
# a stolen token isn't a permanent key. The session token is SHA-256 hashed at
# rest and revoked on logout, so a long window is a UX win, not a key. NB:
# switching between different wallets is a different identity each time and will
# always ask for a fresh sign-in — that's not this TTL.
AUTH_SESSION_TTL_SECS = int(os.getenv("DAGMATE_AUTH_SESSION_TTL_SECS", str(90 * 24 * 3600)))
# Shown in the message the wallet asks the player to sign, so the popup names
# who is asking. Set this to the real host in deployment.
AUTH_DOMAIN = os.getenv("DAGMATE_AUTH_DOMAIN", "dagmate.org")

# ── deposit watcher (deposits.py) ───────────────────────────────────────
# The loop that flips a match awaiting_deposit -> live by looking at what's
# actually on chain. Money-critical, so the defaults are conservative.
DEPOSIT_WATCH_ENABLED = os.getenv("DAGMATE_DEPOSIT_WATCH", "1") == "1"
DEPOSIT_POLL_SECS = int(os.getenv("DAGMATE_DEPOSIT_POLL_SECS", "20"))
# Confirmation depth in DAA before a deposit counts. Kaspa runs ~10 blocks/sec,
# so 100 DAA is roughly 10 seconds — cheap in UX terms, and it means a match
# can never become settleable off a UTXO that's still reorg-able.
DEPOSIT_CONFIRM_DAA = int(os.getenv("DAGMATE_DEPOSIT_CONFIRM_DAA", "100"))
# How long both players get to fund before the match is abandoned. Without
# this a one-sided deposit sits in limbo until the 14-day CLTV, which is a
# terrible outcome for the player who actually paid.
DEPOSIT_DEADLINE_SECS = int(os.getenv("DAGMATE_DEPOSIT_DEADLINE_SECS", str(60 * 60)))

# ── settlement (settlement.py) ──────────────────────────────────────────
# ⚠️ MUST match SETTLE_FEE_SOMPI_PER_INPUT in service/escrow.js
# buildSettleUnsigned(). It's duplicated because the backend has to decide
# whether a pot is even worth settling BEFORE it asks the sidecar to build a tx
# — otherwise the only way a player learns their pot is too small is a raw
# sidecar error. If you change one, change the other, and re-prove the settle
# build on a funded mainnet escrow.
SETTLE_FEE_SOMPI_PER_INPUT = 3_000_000
# A settle spends at least one input per escrow, so this is the floor below
# which releasing the pot would cost more than the pot. Gas-only matches sit
# under it by design: they exist for the on-chain move record, not the money.
SETTLE_MIN_POT_SOMPI = 2 * SETTLE_FEE_SOMPI_PER_INPUT

# ── mutual settlement (settlement.py, roadmap #1 / DAGMATE_SPEC §2.3) ────
# When on, a DECISIVE win settles player-to-player: the winner AND the loser
# co-sign the payout, so DAGmate's arbiter key is NOT used for an honestly
# completed game (the escrow is 2-of-3, and this uses the {playerA, playerB}
# 2-subset instead of {winner, arbiter}). The arbiter stays a stall-breaker:
# if the loser hasn't co-signed within SETTLE_STALL_SECS of the tx being built,
# the winner's claim falls back to the winner+arbiter path (the pre-#1
# behaviour), so a non-cooperating or absent loser can never hold the pot
# hostage. Draws are unaffected (they already release to each depositor).
#
# OFF by default: mutual spends must first be proven on-chain by spike S4
# (service/spikes.mjs) on the target network. Flip DAGMATE_SETTLE_MUTUAL=1 once
# S4 passes there — nothing else about the escrow or the tx changes, only which
# two of the three keys sign, so the flag is a safe, instant on/off.
SETTLE_MUTUAL_ENABLED = os.getenv("DAGMATE_SETTLE_MUTUAL", "0") == "1"
# How long the loser gets to co-sign an honest result before the arbiter
# stall-breaker releases the pot to the winner anyway. Short enough not to make
# a winner wait long when the loser has already closed their tab; long enough
# for a present loser to read "you lost" and click confirm.
SETTLE_STALL_SECS = int(os.getenv("DAGMATE_SETTLE_STALL_SECS", "45"))

# ── covenant escrow v2 (settlement_v2.py, roadmap #2 / DAGMATE_COVENANT_V2.md) ──
# When on, NEW matches build KIP-10 introspection covenants instead of the v1
# 2-of-3 P2SH. DAGmate becomes a write-once oracle: at game-end it signs "A won"
# / "B won" and the escrow SCRIPT releases the pot to that winner, in full, with
# no arbiter co-sign and no player signature — the winner is paid automatically.
#
# OFF by default, and per-match: a match records its escrow_version at creation
# and NEVER switches, so flipping this only affects matches created afterwards;
# in-flight v1 matches finish on v1. Enable only after the v2 full-flow is proven
# on the target network (the opcode primitives + settle + adversarial matrix are
# already proven on mainnet dust — see service/spikes_covenant.mjs).
ESCROW_V2_ENABLED = os.getenv("DAGMATE_ESCROW_V2", "0") == "1"
# ⚠️ MUST match SETTLE_V2_FEE_SOMPI_PER_INPUT / SETTLE_V2_MAXFEE_SOMPI in
# service/escrow_v2.js — duplicated for the same reason as the v1 settle fee
# (the backend quotes/gates before the sidecar builds). The covenant rejects an
# output below input − MAXFEE, so FEE must stay < MAXFEE.
SETTLE_V2_FEE_SOMPI_PER_INPUT = 5_000_000
SETTLE_V2_MAXFEE_SOMPI = 15_000_000

# ── covenant escrow v3 — trustless DAA forfeit (roadmap #3a / DAGMATE_ROADMAP_3A.md) ──
# v2 still trusts DAGmate's oracle to DECLARE the winner. v3 removes the oracle for
# the abandonment/clock-flag case: a co-signed off-chain checkpoint (move_channel)
# + the proven forfeit covenant legs (S8–S11b, mainnet) let a player collect a
# timeout forfeit with NO oracle signature. OFF by default and per-match (same
# never-switches rule as v2). Only flip on once the v3 full flow is proven on the
# target network. The covenant PRIMITIVES are already proven on mainnet dust
# (service/spikes_forfeit.mjs S8/S9/S10/S11/S11b/S11n).
ESCROW_V3_ENABLED = os.getenv("DAGMATE_ESCROW_V3", "0") == "1"
# The forfeit legs use the same fee/maxFee shape as v2 (output ≥ input − MAXFEE).
SETTLE_V3_FEE_SOMPI_PER_INPUT = 5_000_000
SETTLE_V3_MAXFEE_SOMPI = 15_000_000

# ── DAA clock (daa_clock.py) — the on-chain deadline unit for v3 ─────────
# On-chain deadlines are DAA scores, not wall-clock: the covenant reads the tx's
# DAA-locktime, so a checkpoint's deadline must be expressed in DAA. MEASURED on
# mainnet 2026-09-05: ~9.61 DAA/s (post-Crescendo 10 BPS) — NOT the ~1/s that
# pre-Crescendo docs assumed. We set the conversion rate on the HIGH side of the
# measurement so a DAA deadline is never SHORTER in real time than the wall clock
# a player sees (deadlineDaa = now + secs·rate; a higher rate = more chain DAA to
# elapse = more real seconds), which means clock drift can only ever give a player
# MORE time, never flag them early. Per-network override for testnet BPS.
DAA_PER_SEC = float(os.getenv("DAGMATE_DAA_PER_SEC", "10.0"))
# Extra fixed cushion (in DAA) added to every deadline, on top of the high rate —
# absorbs node-to-node virtual-DAA lag at the moment of a claim.
DAA_DEADLINE_MARGIN = int(os.getenv("DAGMATE_DAA_DEADLINE_MARGIN", "150"))  # ~15s at 10/s
# Fischer increment granted per move, in seconds, converted to DAA for the S9
# unilateral-move lower bound. Mirrors the wall-clock increment of the mode.
# Optimistic challenge window W (seconds → DAA): how long a flagged opponent has to
# CANCEL a bogus forfeit with a newer co-signed state before the claimant finalises.
# Generous (hours) so self-defence via watch-and-defend is always easy.
CHALLENGE_WINDOW_SECS = int(os.getenv("DAGMATE_CHALLENGE_WINDOW_SECS", str(2 * 3600)))  # 2h

# ── reclaim (reclaim.py) ────────────────────────────────────────────────
# ⚠️ MUST match RECLAIM_FEE_SOMPI_PER_INPUT in service/escrow.js. Same reason
# as the settle fee above: the backend quotes the payout before the sidecar
# builds the tx, so a mismatch means the UI states a number the chain doesn't
# deliver. Much lower than the settle fee because the CLTV branch is a
# single-sig spend, not a 2-of-3 CHECKMULTISIG.
RECLAIM_FEE_SOMPI_PER_INPUT = 10_000_000

# Tournament fee tiers, KAS. Config-driven per spec §8 — easy to add/remove.
TOURNAMENT_TIERS_KAS = [20, 100, 250, 500]
TOURNAMENT_MIN_ENTRANTS = int(os.getenv("DAGMATE_TOURNAMENT_MIN_ENTRANTS", "8"))

# Gas-only challenges: dust-level stake, still anchors every move on-chain.
GAS_ONLY_STAKE_SOMPI = 1000

# Stake bounds for a real (non-gas-only) challenge. The floor keeps a "money"
# match above the settle fee so the winner actually nets something — a stake
# below SETTLE_MIN_POT_SOMPI would cost more to release than it pays out. The
# ceiling is a blast-radius limit, not a business rule: this is wallet-connect
# P2P with no platform custody, and a fat-fingered 10,000,000-KAS challenge
# should bounce at the form rather than mint an escrow nobody meant to fund.
# Both are env-overridable so a deployment can tune them without a code change.
MIN_STAKE_SOMPI = int(os.getenv("DAGMATE_MIN_STAKE_SOMPI", str(1 * SOMPI_PER_KAS)))
MAX_STAKE_SOMPI = int(os.getenv("DAGMATE_MAX_STAKE_SOMPI", str(1_000_000 * SOMPI_PER_KAS)))

# ── free play (no-wager PvP) ────────────────────────────────────────────
# A stake of 0 creates a FREE match: no escrow, no deposit, no settlement — the
# two players just play chess for the win. It goes live the instant it's
# accepted (skips the whole money path, and doesn't even need the Kaspa
# sidecar), which also sidesteps the storage-mass rule that killed the old
# "gas-only dust stake" idea. Top-of-funnel: let people play before they wager.
# On by default; env-toggleable so a deployment can close it.
FREE_PLAY_ENABLED = os.getenv("DAGMATE_FREE_PLAY", "1") == "1"

# ── move anchoring (main.make_move → service/escrow.js anchor) ──────────
# Every ply, when on, is written to Kaspa L1 as a dust tx carrying a DGMT
# move payload, paid from DAGmate's OWN operating address (never a player's
# wallet — see escrow.js anchor()). That makes it a real, recurring operating
# cost: an N-ply game is N dust txs out of the operating address.
#
# ⚠️ DEFAULT OFF, and deliberately so. Whether to anchor at all, whether it's
# every ply or only the result, and who ultimately funds it are GoonBoy's calls,
# not something a code change should quietly commit real KAS to. The mechanism
# is fully built and mainnet-proven (spike S1); this switch is the one place
# that decides if it runs. While it's off, the site says so (see /api/meta →
# anchorsMoves and the frontend copy) rather than advertising a feature that
# isn't happening. Turn it on only once the operating address is funded and
# the per-move cost is a decision you've made on purpose.
ANCHOR_MOVES = os.getenv("DAGMATE_ANCHOR_MOVES", "0") == "1"

# ── practice engine (engine.py, main.practice_bot_move) ─────────────────
# The negamax search is the one CPU-bound endpoint, and it runs in the
# threadpool of the SAME single-worker process that hosts the deposit watcher
# and the clock watcher. Under the GIL, CPU-bound threads starve the event
# loop, and a stalled deposit watcher past DEPOSIT_DEADLINE_SECS expires funded
# matches — a money outcome. So the number of searches allowed to run at once
# is capped; requests over the cap get a fast 429 instead of piling into the
# pool. (bot-move also now requires a session — the practice board is behind a
# wallet connect anyway — so this is a ceiling on authenticated use, not the
# only line of defence.)
PRACTICE_MAX_CONCURRENCY = int(os.getenv("DAGMATE_PRACTICE_MAX_CONCURRENCY", "2"))

# ── learn payments (main.unlock_level) ──────────────────────────────────
# On-chain gas-payment verification for paid learn levels is NOT wired. Until
# it is, levels unlock free and the UI is told to say "free" rather than
# advertise a price DAGmate never collects (GoonBoy, 2026-08-22: "unlocks free,
# just gas fees would be good"). Flip this on only once a real payment check
# exists — while it's on, the unlock endpoint refuses paid levels rather than
# hand them out for free under a price tag. Surfaced in /api/meta so the
# frontend price copy follows the same switch.
LEARN_REQUIRE_PAYMENT = os.getenv("DAGMATE_LEARN_REQUIRE_PAYMENT", "0") == "1"

# ── clocks (clocks.py) ──────────────────────────────────────────────────
# Fischer increment, one mechanism for both modes — `daily` is just a very
# slow rapid game, which is far less to get wrong than a second per-move
# deadline system. Clocks only run while a match is `live`.
#
# Clocks are what stop a losing player simply walking away: without them an
# abandoned game sits until the 14-day CLTV and both stakes are refunded,
# which makes quitting a free undo on any lost position.
CLOCK_MODES = {
    "rapid": {"label": "Rapid 10+5", "initial_secs": 10 * 60, "increment_secs": 5},
    "daily": {"label": "Daily 3d+12h", "initial_secs": 3 * 24 * 3600, "increment_secs": 12 * 3600},
}
CLOCK_POLL_SECS = int(os.getenv("DAGMATE_CLOCK_POLL_SECS", "5"))
# Warn a player once when their remaining time drops below this fraction of
# the mode's starting bank (the alerts bot's notify_clock_warning, which until
# now was dead code nothing called).
CLOCK_WARN_FRACTION = float(os.getenv("DAGMATE_CLOCK_WARN_FRACTION", "0.1"))

# Learn page: leveled curriculum, each level a plain gas send (no escrow).
# The catalogue itself lives in curriculum.py (one source of truth for both the
# index and the paid bodies) — this is only the index view, which never carries
# level content and so is safe to serve to anyone.
import curriculum  # noqa: E402  (kept below the env block for readability)

LEARN_TIERS = curriculum.TIERS
LEARN_LEVELS = curriculum.level_index()

# KNS (.kas domain) lookups — same public indexer Dagger uses. Third-party,
# no key/auth; every call is defensive (short timeout, DB-cached, never
# raises) — see kns.py.
KNS_ENABLED = os.getenv("DAGMATE_KNS_ENABLED", "1") == "1"
KNS_API_URL = os.getenv("DAGMATE_KNS_API_URL", "https://api.knsdomains.org/mainnet/api/v1")
KNS_TIMEOUT = float(os.getenv("DAGMATE_KNS_TIMEOUT", "6"))
KNS_CACHE_TTL = int(os.getenv("DAGMATE_KNS_CACHE_TTL", "21600"))  # 6h

# ── dev routes ──────────────────────────────────────────────────────────
# ONE switch for every testing affordance on this process: the demo wallet,
# its message-signing helper, and `dev-mark-funded` (which flips a match to
# live without anybody paying — i.e. it prints a pot).
#
# OFF unless explicitly switched on, and deliberately opt-in rather than
# opt-out: forgetting to turn it on costs a developer two minutes, forgetting
# to turn it off hands a public site a free-money button. Anything that reads
# "convenient by default" here is a deployment waiting to inherit it.
#
# Also hard-off on mainnet regardless of the env var, mirroring the same guard
# in service/server.js. A "demo wallet" on mainnet is a throwaway key holding
# real funds, handed to someone told it's for testing.
NETWORK_ID = os.getenv("DAGMATE_NETWORK_ID", "mainnet")
DEV_ROUTES = os.getenv("DAGMATE_DEV_ROUTES") == "1" and not NETWORK_ID.startswith("mainnet")
