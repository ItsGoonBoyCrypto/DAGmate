"""DAGmate site backend — storage (SQLite, own DB, see docs/DAGMATE_SPEC.md).

Accounts are keyed by wallet address (no separate login — connecting a
wallet IS the identity). Everything else (challenges, matches, tournaments,
learn progress) hangs off that.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid

import config

_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    d = os.path.dirname(config.DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_schema():
    with _lock, _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS accounts (
          id TEXT PRIMARY KEY,
          address TEXT UNIQUE NOT NULL,
          pubkey TEXT,
          accept_challenges INTEGER NOT NULL DEFAULT 1,
          is_demo_wallet INTEGER NOT NULL DEFAULT 0,
          created_ts INTEGER NOT NULL)""")

        c.execute("""CREATE TABLE IF NOT EXISTS challenges (
          id TEXT PRIMARY KEY,
          from_account_id TEXT NOT NULL,
          to_account_id TEXT,
          stake_sompi INTEGER NOT NULL,
          mode TEXT NOT NULL,
          gas_only INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'open',
          created_ts INTEGER NOT NULL)""")

        c.execute("""CREATE TABLE IF NOT EXISTS matches (
          id TEXT PRIMARY KEY,
          hd_index INTEGER,
          challenge_id TEXT,
          tournament_id TEXT,
          round INTEGER,
          player_a_account_id TEXT NOT NULL,
          player_b_account_id TEXT NOT NULL,
          stake_sompi INTEGER NOT NULL,
          mode TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'awaiting_deposit',
          fen TEXT NOT NULL,
          moves_json TEXT NOT NULL DEFAULT '[]',
          turn TEXT NOT NULL DEFAULT 'white',
          escrow_a_address TEXT, escrow_a_redeem_hex TEXT,
          escrow_b_address TEXT, escrow_b_redeem_hex TEXT,
          reclaim_daa INTEGER,
          winner_account_id TEXT,
          result TEXT,
          created_ts INTEGER NOT NULL,
          settled_ts INTEGER)""")

        c.execute("""CREATE TABLE IF NOT EXISTS tournaments (
          id TEXT PRIMARY KEY,
          tier_kas INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'open',
          created_ts INTEGER NOT NULL)""")

        c.execute("""CREATE TABLE IF NOT EXISTS tournament_entrants (
          tournament_id TEXT NOT NULL,
          account_id TEXT NOT NULL,
          joined_ts INTEGER NOT NULL,
          PRIMARY KEY (tournament_id, account_id))""")

        c.execute("""CREATE TABLE IF NOT EXISTS learn_progress (
          account_id TEXT NOT NULL,
          level_id TEXT NOT NULL,
          unlocked_ts INTEGER NOT NULL,
          PRIMARY KEY (account_id, level_id))""")

        c.execute("""CREATE TABLE IF NOT EXISTS kns_cache (
          address TEXT PRIMARY KEY,
          names_json TEXT NOT NULL,
          primary_name TEXT,
          fetched_ts INTEGER NOT NULL)""")


def _row(cur) -> dict | None:
    r = cur.fetchone()
    return dict(r) if r else None


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


# ── accounts ─────────────────────────────────────────────────────────────
def get_or_create_account(address: str, pubkey: str | None = None, is_demo: bool = False) -> dict:
    with _lock, _conn() as c:
        row = _row(c.execute("SELECT * FROM accounts WHERE address=?", (address,)))
        if row:
            if pubkey and pubkey != row["pubkey"]:
                c.execute("UPDATE accounts SET pubkey=? WHERE id=?", (pubkey, row["id"]))
                row["pubkey"] = pubkey
            return row
        acct_id = str(uuid.uuid4())
        c.execute("INSERT INTO accounts (id, address, pubkey, is_demo_wallet, created_ts) VALUES (?,?,?,?,?)",
                   (acct_id, address, pubkey, 1 if is_demo else 0, int(time.time())))
        return _row(c.execute("SELECT * FROM accounts WHERE id=?", (acct_id,)))


def get_account(account_id: str) -> dict | None:
    with _lock, _conn() as c:
        return _row(c.execute("SELECT * FROM accounts WHERE id=?", (account_id,)))


def get_account_by_address(address: str) -> dict | None:
    with _lock, _conn() as c:
        return _row(c.execute("SELECT * FROM accounts WHERE address=?", (address,)))


def set_accept_challenges(account_id: str, enabled: bool):
    with _lock, _conn() as c:
        c.execute("UPDATE accounts SET accept_challenges=? WHERE id=?", (1 if enabled else 0, account_id))


# ── challenges ───────────────────────────────────────────────────────────
def create_challenge(from_account_id: str, to_account_id: str | None, stake_sompi: int, mode: str, gas_only: bool) -> dict:
    with _lock, _conn() as c:
        cid = str(uuid.uuid4())
        c.execute("INSERT INTO challenges (id, from_account_id, to_account_id, stake_sompi, mode, gas_only, created_ts) "
                   "VALUES (?,?,?,?,?,?,?)",
                   (cid, from_account_id, to_account_id, stake_sompi, mode, 1 if gas_only else 0, int(time.time())))
        return _row(c.execute("SELECT * FROM challenges WHERE id=?", (cid,)))


def list_open_challenges(for_account_id: str | None = None) -> list[dict]:
    with _lock, _conn() as c:
        if for_account_id:
            return _rows(c.execute(
                "SELECT * FROM challenges WHERE status='open' "
                "AND (to_account_id IS NULL OR to_account_id=? OR from_account_id=?) "
                "ORDER BY created_ts DESC", (for_account_id, for_account_id)))
        return _rows(c.execute("SELECT * FROM challenges WHERE status='open' ORDER BY created_ts DESC"))


def get_challenge(challenge_id: str) -> dict | None:
    with _lock, _conn() as c:
        return _row(c.execute("SELECT * FROM challenges WHERE id=?", (challenge_id,)))


def set_challenge_status(challenge_id: str, status: str):
    with _lock, _conn() as c:
        c.execute("UPDATE challenges SET status=? WHERE id=?", (status, challenge_id))


# ── matches ──────────────────────────────────────────────────────────────
def create_match(*, challenge_id: str | None, tournament_id: str | None, round_no: int | None,
                  player_a_account_id: str, player_b_account_id: str, stake_sompi: int, mode: str,
                  fen: str, escrow_a: dict | None, escrow_b: dict | None, reclaim_daa: int | None) -> dict:
    with _lock, _conn() as c:
        mid = str(uuid.uuid4())
        cur = c.execute("""INSERT INTO matches (id, challenge_id, tournament_id, round, player_a_account_id,
          player_b_account_id, stake_sompi, mode, fen, escrow_a_address, escrow_a_redeem_hex,
          escrow_b_address, escrow_b_redeem_hex, reclaim_daa, created_ts)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (mid, challenge_id, tournament_id, round_no, player_a_account_id, player_b_account_id,
                   stake_sompi, mode, fen,
                   (escrow_a or {}).get("address"), (escrow_a or {}).get("redeemHex"),
                   (escrow_b or {}).get("address"), (escrow_b or {}).get("redeemHex"),
                   reclaim_daa, int(time.time())))
        # SQLite's implicit rowid, pinned into its own column: the per-match
        # arbiter key (service/core.js `deriveArbiter(matchId)`) is derived
        # from this integer, so it needs to be a stable, never-reused value —
        # the UUID `id` above is the public/API identifier, this is purely
        # the HD derivation index.
        c.execute("UPDATE matches SET hd_index=? WHERE id=?", (cur.lastrowid, mid))
        return _row(c.execute("SELECT * FROM matches WHERE id=?", (mid,)))


def get_match(match_id: str) -> dict | None:
    with _lock, _conn() as c:
        return _row(c.execute("SELECT * FROM matches WHERE id=?", (match_id,)))


def list_matches_for_account(account_id: str) -> list[dict]:
    with _lock, _conn() as c:
        return _rows(c.execute(
            "SELECT * FROM matches WHERE player_a_account_id=? OR player_b_account_id=? ORDER BY created_ts DESC",
            (account_id, account_id)))


def apply_move(match_id: str, fen: str, moves: list[str], turn: str):
    with _lock, _conn() as c:
        c.execute("UPDATE matches SET fen=?, moves_json=?, turn=? WHERE id=?",
                   (fen, json.dumps(moves), turn, match_id))


def settle_match(match_id: str, *, status: str, result: str, winner_account_id: str | None):
    with _lock, _conn() as c:
        c.execute("UPDATE matches SET status=?, result=?, winner_account_id=?, settled_ts=? WHERE id=?",
                   (status, result, winner_account_id, int(time.time()), match_id))


def set_match_status(match_id: str, status: str):
    with _lock, _conn() as c:
        c.execute("UPDATE matches SET status=? WHERE id=?", (status, match_id))


def set_match_escrows(match_id: str, escrow_a: dict, escrow_b: dict):
    with _lock, _conn() as c:
        c.execute("UPDATE matches SET escrow_a_address=?, escrow_a_redeem_hex=?, "
                   "escrow_b_address=?, escrow_b_redeem_hex=? WHERE id=?",
                   (escrow_a.get("address"), escrow_a.get("redeemHex"),
                    escrow_b.get("address"), escrow_b.get("redeemHex"), match_id))


# ── tournaments ──────────────────────────────────────────────────────────
def get_or_create_open_tournament(tier_kas: int) -> dict:
    with _lock, _conn() as c:
        row = _row(c.execute("SELECT * FROM tournaments WHERE tier_kas=? AND status='open' ORDER BY created_ts DESC LIMIT 1",
                              (tier_kas,)))
        if row:
            return row
        tid = str(uuid.uuid4())
        c.execute("INSERT INTO tournaments (id, tier_kas, created_ts) VALUES (?,?,?)",
                   (tid, tier_kas, int(time.time())))
        return _row(c.execute("SELECT * FROM tournaments WHERE id=?", (tid,)))


def get_or_create_open_tournament_readonly(tier_kas: int) -> dict | None:
    """Like get_or_create_open_tournament but never inserts — for a plain
    GET /api/tournaments listing that shouldn't spin up empty lobby rows
    just from being viewed."""
    with _lock, _conn() as c:
        return _row(c.execute(
            "SELECT * FROM tournaments WHERE tier_kas=? AND status IN ('open','running') "
            "ORDER BY created_ts DESC LIMIT 1", (tier_kas,)))


def join_tournament(tournament_id: str, account_id: str) -> bool:
    """Idempotent join — returns False if already entered (no error, no dupe row)."""
    with _lock, _conn() as c:
        try:
            c.execute("INSERT INTO tournament_entrants (tournament_id, account_id, joined_ts) VALUES (?,?,?)",
                       (tournament_id, account_id, int(time.time())))
            return True
        except sqlite3.IntegrityError:
            return False


def count_entrants(tournament_id: str) -> int:
    with _lock, _conn() as c:
        return c.execute("SELECT COUNT(*) FROM tournament_entrants WHERE tournament_id=?",
                          (tournament_id,)).fetchone()[0]


def list_entrants(tournament_id: str) -> list[dict]:
    with _lock, _conn() as c:
        return _rows(c.execute("SELECT * FROM tournament_entrants WHERE tournament_id=?", (tournament_id,)))


def set_tournament_status(tournament_id: str, status: str):
    with _lock, _conn() as c:
        c.execute("UPDATE tournaments SET status=? WHERE id=?", (status, tournament_id))


def list_tournaments() -> list[dict]:
    with _lock, _conn() as c:
        return _rows(c.execute("SELECT * FROM tournaments WHERE status IN ('open','running') ORDER BY tier_kas"))


def get_tournament(tournament_id: str) -> dict | None:
    with _lock, _conn() as c:
        return _row(c.execute("SELECT * FROM tournaments WHERE id=?", (tournament_id,)))


# ── learn progress ───────────────────────────────────────────────────────
def unlock_level(account_id: str, level_id: str):
    with _lock, _conn() as c:
        c.execute("INSERT OR IGNORE INTO learn_progress (account_id, level_id, unlocked_ts) VALUES (?,?,?)",
                   (account_id, level_id, int(time.time())))


def unlocked_levels(account_id: str) -> set[str]:
    with _lock, _conn() as c:
        rows = c.execute("SELECT level_id FROM learn_progress WHERE account_id=?", (account_id,)).fetchall()
        return {r[0] for r in rows}


# ── KNS (.kas domain) cache ─────────────────────────────────────────────
def kns_get(address: str) -> tuple[list[str] | None, int]:
    """(names, age_seconds). names is None on a cache miss (age is then a
    large sentinel so callers' `age < TTL` checks naturally treat it as
    stale)."""
    with _lock, _conn() as c:
        row = _row(c.execute("SELECT * FROM kns_cache WHERE address=?", (address,)))
        if not row:
            return None, 10**9
        return json.loads(row["names_json"]), int(time.time()) - row["fetched_ts"]


def kns_put(address: str, names: list[str], primary: str):
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO kns_cache (address, names_json, primary_name, fetched_ts) VALUES (?,?,?,?) "
            "ON CONFLICT(address) DO UPDATE SET names_json=excluded.names_json, "
            "primary_name=excluded.primary_name, fetched_ts=excluded.fetched_ts",
            (address, json.dumps(names), primary, int(time.time())))


def kns_get_many(addresses: list[str]) -> dict:
    if not addresses:
        return {}
    with _lock, _conn() as c:
        qmarks = ",".join("?" for _ in addresses)
        rows = _rows(c.execute(f"SELECT * FROM kns_cache WHERE address IN ({qmarks})", addresses))
        return {r["address"]: json.loads(r["names_json"]) for r in rows}
