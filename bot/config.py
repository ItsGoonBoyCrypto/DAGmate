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
WEBHOOK_SHARED_SECRET = os.environ["DAGMATE_WEBHOOK_SECRET"]
WEBHOOK_HOST = os.getenv("DAGMATE_WEBHOOK_HOST", "127.0.0.1")
WEBHOOK_PORT = int(os.getenv("DAGMATE_WEBHOOK_PORT", "8901"))

DB_PATH = os.getenv("DAGMATE_BOT_DB", "state/dagmate_bot.db")
LINK_CODE_TTL_S = int(os.getenv("DAGMATE_LINK_CODE_TTL_S", "600"))
