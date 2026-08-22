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

BOT_WEBHOOK_URL = os.getenv("DAGMATE_BOT_WEBHOOK_URL", "http://127.0.0.1:8901")
BOT_WEBHOOK_SECRET = os.getenv("DAGMATE_WEBHOOK_SECRET", "")

FEE_ADDRESS = os.getenv("DAGMATE_FEE_ADDRESS")  # optional; service falls back to operating address
RAKE_BPS = int(os.getenv("DAGMATE_RAKE_BPS", "300"))  # 3% default, basis points

# CLTV reclaim window: ~14 days of DAA at Kaspa's ~10 blocks/sec cadence.
RECLAIM_DAA_WINDOW = 14 * 24 * 3600 * 10

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

# Tournament fee tiers, KAS. Config-driven per spec §8 — easy to add/remove.
TOURNAMENT_TIERS_KAS = [20, 100, 250, 500]
TOURNAMENT_MIN_ENTRANTS = int(os.getenv("DAGMATE_TOURNAMENT_MIN_ENTRANTS", "8"))

# Gas-only challenges: dust-level stake, still anchors every move on-chain.
GAS_ONLY_STAKE_SOMPI = 1000

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

# Local dev/testing convenience ONLY (see frontend "demo wallet" fallback) —
# never used in the real wallet-connect signing path. Off by default; the
# site backend itself doesn't gate on this (the demo-wallet route is always
# available), but it's flagged here so it's easy to find and rip out.
DEMO_WALLET_ENABLED = os.getenv("DAGMATE_DEMO_WALLET", "1") == "1"
