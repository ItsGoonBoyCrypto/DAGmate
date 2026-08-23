"""DAGmate alerts bot — storage.

One table, `links`, mapping a Telegram user to a dagmate.org site account for
notification delivery. That's the entire data model: no wallet data, no game
state, no keys (see docs/DAGMATE_SPEC.md §4 — this bot is alerts-only).
"""
from __future__ import annotations

import os
import secrets
import sqlite3
import string
import threading
import time

import config

_lock = threading.Lock()

_LINK_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I ambiguity


def _conn() -> sqlite3.Connection:
    d = os.path.dirname(config.DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema():
    with _lock, _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS links (
          telegram_user_id INTEGER PRIMARY KEY,
          site_account_id TEXT,
          link_code TEXT,
          link_code_expires_ts INTEGER,
          alerts_enabled INTEGER NOT NULL DEFAULT 1,
          created_ts INTEGER NOT NULL,
          linked_ts INTEGER)""")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_links_site_account "
                  "ON links(site_account_id) WHERE site_account_id IS NOT NULL")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_links_code "
                  "ON links(link_code) WHERE link_code IS NOT NULL")


def _row(cur) -> dict | None:
    r = cur.fetchone()
    return dict(r) if r else None


def get_link(telegram_user_id: int) -> dict | None:
    with _lock, _conn() as c:
        return _row(c.execute("SELECT * FROM links WHERE telegram_user_id=?", (telegram_user_id,)))


def get_by_site_account(site_account_id: str) -> dict | None:
    with _lock, _conn() as c:
        return _row(c.execute("SELECT * FROM links WHERE site_account_id=?", (str(site_account_id),)))


def new_link_code(telegram_user_id: int) -> str:
    """Mint a fresh one-time code for this Telegram user and (re)arm its TTL.
    Safe to call again before a code is claimed — it just replaces the old
    one, so a stale /start press can't leave two live codes for one user."""
    code = "".join(secrets.choice(_LINK_CODE_ALPHABET) for _ in range(8))
    now = int(time.time())
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO links (telegram_user_id, link_code, link_code_expires_ts, created_ts) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(telegram_user_id) DO UPDATE SET "
            "link_code=excluded.link_code, link_code_expires_ts=excluded.link_code_expires_ts "
            "WHERE site_account_id IS NULL",
            (telegram_user_id, code, now + config.LINK_CODE_TTL_S, now))
    return code


def claim_link_code(code: str, site_account_id: str) -> bool:
    """Site backend calls this once a logged-in player submits their code.
    Atomic compare-and-set on link_code + not-expired — same idiom as a
    claim-row pattern: the UPDATE's WHERE clause IS the check, so a raced or
    replayed claim can't double-link two site accounts to one code."""
    now = int(time.time())
    try:
        with _lock, _conn() as c:
            cur = c.execute(
                "UPDATE links SET site_account_id=?, link_code=NULL, link_code_expires_ts=NULL, "
                "linked_ts=? WHERE link_code=? AND link_code_expires_ts>=? AND site_account_id IS NULL",
                (str(site_account_id), now, code, now))
            return cur.rowcount == 1
    except sqlite3.IntegrityError:
        # That site account is already linked to a DIFFERENT Telegram (the
        # UNIQUE index on site_account_id). This is a clean "no" — not a 500 —
        # so a player who already linked elsewhere gets a rejection, not a crash.
        return False


def set_alerts(telegram_user_id: int, enabled: bool):
    with _lock, _conn() as c:
        c.execute("UPDATE links SET alerts_enabled=? WHERE telegram_user_id=?",
                 (1 if enabled else 0, telegram_user_id))


def unlink(telegram_user_id: int):
    with _lock, _conn() as c:
        c.execute("DELETE FROM links WHERE telegram_user_id=?", (telegram_user_id,))
