"""DAGmate alerts bot — config (env-only, see docs/DAGMATE_SPEC.md §4).

No wallet, no game state, no keys live here or anywhere in this bot — it is
a pure Telegram notification relay for the dagmate.org site backend. Every
value comes from the environment on purpose (same convention as the rest of
the project): nothing secret is ever hardcoded or committed.
"""
import os

BOT_TOKEN = os.environ["DAGMATE_BOT_TOKEN"]

# Shared secret the dagmate.org site backend must send on every internal
# call (link-code claim + all notify_* routes). This API is localhost-bound
# by default — WEBHOOK_HOST should only be widened if the site backend runs
# on a different host than this bot, behind its own network controls.
#
# Require real length: a short or empty secret is refused at startup rather
# than silently authenticating a present-but-empty header (see _authed, which
# also compares in constant time). This is the auth for every internal route,
# so a blank one is a fail-open door even behind loopback.
WEBHOOK_SHARED_SECRET = os.environ["DAGMATE_WEBHOOK_SECRET"]
if len(WEBHOOK_SHARED_SECRET) < 32:
    raise RuntimeError("DAGMATE_WEBHOOK_SECRET must be at least 32 chars "
                       "(generate with: openssl rand -hex 32)")
WEBHOOK_HOST = os.getenv("DAGMATE_WEBHOOK_HOST", "127.0.0.1")
WEBHOOK_PORT = int(os.getenv("DAGMATE_WEBHOOK_PORT", "8901"))

# Absolute default so the bot doesn't try to write a relative `state/` dir it
# can't create under ProtectSystem=strict (the unit's ReadWritePaths is
# /var/lib/dagmate/bot). Overridden by DAGMATE_BOT_DB in bot.env.
DB_PATH = os.getenv("DAGMATE_BOT_DB", "/var/lib/dagmate/bot/dagmate_bot.db")
LINK_CODE_TTL_S = int(os.getenv("DAGMATE_LINK_CODE_TTL_S", "600"))
