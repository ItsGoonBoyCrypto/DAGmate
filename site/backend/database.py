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

        # ── auth (auth.py) ────────────────────────────────────────────────
        # A login challenge. `address` is stored WITH the nonce because the
        # message a player signs is rebuilt server-side from this row and
        # never taken from the request — otherwise a signature harvested over
        # some other string could be replayed as a login.
        c.execute("""CREATE TABLE IF NOT EXISTS auth_nonces (
          nonce TEXT PRIMARY KEY,
          address TEXT NOT NULL,
          issued_ts INTEGER NOT NULL,
          used_ts INTEGER)""")

        # Sessions store a HASH of the token, never the token itself. Anything
        # that can read this DB — a backup, a stray file copy, a query-shaped
        # bug — would otherwise be handing out live logins for every player.
        c.execute("""CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY,
          account_id TEXT NOT NULL,
          issued_ts INTEGER NOT NULL,
          expires_ts INTEGER NOT NULL,
          revoked_ts INTEGER)""")

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

        # Deposit-watcher state (deposits.py). Added by migration rather than
        # in the CREATE above so existing local/testnet DBs pick it up.
        _add_column(c, "matches", "funded_a_sompi", "INTEGER NOT NULL DEFAULT 0")
        _add_column(c, "matches", "funded_b_sompi", "INTEGER NOT NULL DEFAULT 0")
        _add_column(c, "matches", "funded_a_ts", "INTEGER")
        _add_column(c, "matches", "funded_b_ts", "INTEGER")
        _add_column(c, "matches", "deposit_checked_ts", "INTEGER")

        # Clock state (clocks.py). Kept per COLOUR rather than per player slot:
        # player A is always white, but naming them white/black means the
        # timing code never has to remember that mapping to stay correct.
        # Milliseconds throughout — seconds would visibly drift over a game.
        _add_column(c, "matches", "clock_white_ms", "INTEGER")
        _add_column(c, "matches", "clock_black_ms", "INTEGER")
        _add_column(c, "matches", "clock_increment_ms", "INTEGER")
        _add_column(c, "matches", "clock_turn_started_ms", "INTEGER")
        _add_column(c, "matches", "clock_warned_white", "INTEGER NOT NULL DEFAULT 0")
        _add_column(c, "matches", "clock_warned_black", "INTEGER NOT NULL DEFAULT 0")

        # Settlement state (settlement.py). A settle tx is BUILT ONCE and then
        # reused: rebuilding picks different UTXOs/fees and would silently
        # invalidate any signature already collected, which in a draw (where
        # the two players sign at different times, possibly days apart) would
        # mean the first signer's approval quietly stopped counting.
        _add_column(c, "matches", "settle_tx_json", "TEXT")
        _add_column(c, "matches", "settle_inputs_json", "TEXT")   # per-input: escrow + required signer
        _add_column(c, "matches", "settle_sigs_arb_json", "TEXT")  # ours, computed at build time
        _add_column(c, "matches", "settle_sigs_player_json", "TEXT")  # filled in as players sign
        _add_column(c, "matches", "settle_pot_sompi", "INTEGER")
        _add_column(c, "matches", "settle_rake_sompi", "INTEGER")
        _add_column(c, "matches", "settle_prepared_ts", "INTEGER")
        _add_column(c, "matches", "settle_txid", "TEXT")
        _add_column(c, "matches", "settle_broadcast_ts", "INTEGER")

        # Draw offers. `draw_offer_by` is the standing offer (NULL = none);
        # `draw_offer_ply` is the move number it was made at and is NOT cleared
        # when the offer goes away, because it doubles as the anti-spam latch:
        # one offer per position, so a declined offer can't be re-sent
        # immediately and turn the board into a nag box.
        _add_column(c, "matches", "draw_offer_by", "TEXT")
        _add_column(c, "matches", "draw_offer_ply", "INTEGER")

        # Reclaim receipts (reclaim.py). A record only — the chain is the
        # authority on whether an escrow still holds anything. Reclaim keeps no
        # built-tx state on purpose: one signer, one visit, so a rebuild costs
        # nothing and can't orphan a co-signer's signature the way a settle
        # rebuild would.
        _add_column(c, "matches", "reclaim_a_txid", "TEXT")
        _add_column(c, "matches", "reclaim_b_txid", "TEXT")


def _add_column(c: sqlite3.Connection, table: str, column: str, decl: str):
    """Idempotent ALTER TABLE ADD COLUMN (SQLite has no IF NOT EXISTS here)."""
    existing = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _row(cur) -> dict | None:
    r = cur.fetchone()
    return dict(r) if r else None


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


# ── auth: login nonces + sessions ────────────────────────────────────────
def create_nonce(nonce: str, address: str, issued_ts: int):
    with _lock, _conn() as c:
        c.execute("INSERT INTO auth_nonces (nonce, address, issued_ts) VALUES (?,?,?)",
                   (nonce, address, issued_ts))


def consume_nonce(nonce: str, now_ts: int) -> dict | None:
    """Claim a login challenge, once. The guarded UPDATE is the replay defence:
    a signature stays valid forever, so the only thing stopping a captured
    login from being replayed is that its nonce can be spent exactly once, and
    `rowcount == 1` is what tells us THIS call is the one that spent it."""
    with _lock, _conn() as c:
        row = _row(c.execute("SELECT * FROM auth_nonces WHERE nonce=?", (nonce,)))
        if not row:
            return None
        cur = c.execute("UPDATE auth_nonces SET used_ts=? WHERE nonce=? AND used_ts IS NULL",
                        (now_ts, nonce))
        return row if cur.rowcount == 1 else None


def purge_nonces(before_ts: int):
    with _lock, _conn() as c:
        c.execute("DELETE FROM auth_nonces WHERE issued_ts < ?", (before_ts,))


def create_session(token_hash: str, account_id: str, issued_ts: int, expires_ts: int):
    with _lock, _conn() as c:
        c.execute("INSERT INTO sessions (token_hash, account_id, issued_ts, expires_ts) VALUES (?,?,?,?)",
                   (token_hash, account_id, issued_ts, expires_ts))


def account_for_session(token_hash: str, now_ts: int) -> dict | None:
    """The account behind a session token, or None if there isn't a live one.
    Expiry and revocation are enforced in the query rather than by the caller,
    so no endpoint can forget to check them."""
    with _lock, _conn() as c:
        return _row(c.execute(
            "SELECT a.* FROM sessions s JOIN accounts a ON a.id = s.account_id "
            "WHERE s.token_hash=? AND s.revoked_ts IS NULL AND s.expires_ts > ?",
            (token_hash, now_ts)))


def revoke_session(token_hash: str, now_ts: int) -> bool:
    with _lock, _conn() as c:
        cur = c.execute("UPDATE sessions SET revoked_ts=? WHERE token_hash=? AND revoked_ts IS NULL",
                        (now_ts, token_hash))
        return cur.rowcount == 1


# ── accounts ─────────────────────────────────────────────────────────────
def get_or_create_account(address: str, pubkey: str | None = None, is_demo: bool = False) -> dict:
    """⚠️ Only ever call this with a pubkey the sidecar has already proven
    belongs to `address` (auth.verify). The stored pubkey is what escrows are
    built from, so letting an unproven one in would let a stranger install
    their own key on someone else's account."""
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


# ── deposit watching ─────────────────────────────────────────────────────
def list_matches_awaiting_deposit() -> list[dict]:
    """Only matches whose escrows actually exist — one built with an
    unreachable sidecar has NULL addresses and there is nothing to watch."""
    with _lock, _conn() as c:
        return _rows(c.execute(
            "SELECT * FROM matches WHERE status='awaiting_deposit' "
            "AND escrow_a_address IS NOT NULL AND escrow_b_address IS NOT NULL "
            "ORDER BY created_ts"))


def record_deposits(match_id: str, a_sompi: int, b_sompi: int, stake_sompi: int) -> dict:
    """Persist the observed on-chain balance of each escrow. The `*_ts` columns
    latch the FIRST moment a side was seen fully funded and are never cleared,
    so a transient node hiccup that under-reports a balance can't retroactively
    un-fund a side."""
    now = int(time.time())
    with _lock, _conn() as c:
        c.execute(
            "UPDATE matches SET funded_a_sompi=?, funded_b_sompi=?, deposit_checked_ts=?, "
            "funded_a_ts = CASE WHEN funded_a_ts IS NULL AND ? >= ? THEN ? ELSE funded_a_ts END, "
            "funded_b_ts = CASE WHEN funded_b_ts IS NULL AND ? >= ? THEN ? ELSE funded_b_ts END "
            "WHERE id=?",
            (a_sompi, b_sompi, now, a_sompi, stake_sompi, now, b_sompi, stake_sompi, now, match_id))
        return _row(c.execute("SELECT * FROM matches WHERE id=?", (match_id,)))


def mark_match_live(match_id: str, *, initial_ms: int, increment_ms: int, now_ms: int) -> bool:
    """Guarded transition: only ever fires from awaiting_deposit, and returns
    whether THIS call is the one that made the change. The watcher uses that to
    decide whether to notify, so a re-poll (or two workers) can't double-start
    a match or double-DM its players.

    Starting the clock is part of the SAME statement: a match that is live but
    has no running clock is exactly the abandonment hole clocks exist to close,
    so the two must never be separately observable."""
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE matches SET status='live', clock_white_ms=?, clock_black_ms=?, "
            "clock_increment_ms=?, clock_turn_started_ms=? "
            "WHERE id=? AND status='awaiting_deposit'",
            (initial_ms, initial_ms, increment_ms, now_ms, match_id))
        return cur.rowcount == 1


def list_live_matches() -> list[dict]:
    with _lock, _conn() as c:
        return _rows(c.execute("SELECT * FROM matches WHERE status='live'"))


def apply_move_with_clock(match_id: str, fen: str, moves: list[str], turn: str,
                          *, mover_color: str, mover_remaining_ms: int, now_ms: int) -> bool:
    """Commit a move and the mover's new clock in one guarded statement.

    The `turn` guard is what makes this safe under concurrent requests: two
    moves racing for the same position both read `turn='white'`, but only the
    first UPDATE matches, so the second is rejected rather than silently
    overwriting the board or double-charging a clock.

    Clearing `draw_offer_by` is part of the SAME statement, and has to be:
    playing on is how you decline an offer, and an offer that outlived the
    position it was made in could be accepted twenty moves later by whoever
    turned out to be losing."""
    col = "clock_white_ms" if mover_color == "white" else "clock_black_ms"
    with _lock, _conn() as c:
        cur = c.execute(
            f"UPDATE matches SET fen=?, moves_json=?, turn=?, {col}=?, clock_turn_started_ms=?, "
            "draw_offer_by=NULL "
            "WHERE id=? AND status='live' AND turn=?",
            (fen, json.dumps(moves), turn, mover_remaining_ms, now_ms, match_id, mover_color))
        return cur.rowcount == 1


def mark_clock_warned(match_id: str, color: str) -> bool:
    """Latch so the low-time warning DMs once, not every poll."""
    col = "clock_warned_white" if color == "white" else "clock_warned_black"
    with _lock, _conn() as c:
        cur = c.execute(f"UPDATE matches SET {col}=1 WHERE id=? AND {col}=0", (match_id,))
        return cur.rowcount == 1


def settle_match_if_live(match_id: str, *, result: str, winner_account_id: str | None) -> bool:
    """Guarded end-of-game write, for endings decided by a background loop
    (a clock flag) that could otherwise race a move landing in the same
    instant. Returns whether this call is the one that ended the match."""
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE matches SET status='settled', result=?, winner_account_id=?, settled_ts=? "
            "WHERE id=? AND status='live'",
            (result, winner_account_id, int(time.time()), match_id))
        return cur.rowcount == 1


# ── draw offers ──────────────────────────────────────────────────────────
# A draw splits the pot, so agreeing to one is a money decision by both
# players and every statement here is guarded accordingly. `ply` is the number
# of moves played when the offer was made — the caller passes it so the
# one-offer-per-position rule is enforced by the same UPDATE that stores the
# offer, not by a read-then-write the opponent could slip between.
def offer_draw(match_id: str, account_id: str, ply: int) -> bool:
    """Put a draw offer on the board. False if one already stands (either
    player's) or if this position has already had its offer."""
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE matches SET draw_offer_by=?, draw_offer_ply=? "
            "WHERE id=? AND status='live' AND draw_offer_by IS NULL "
            "AND (draw_offer_ply IS NULL OR draw_offer_ply < ?)",
            (account_id, ply, match_id, ply))
        return cur.rowcount == 1


def clear_draw_offer(match_id: str) -> bool:
    """Decline (opponent) or withdraw (offerer). `draw_offer_ply` deliberately
    survives: it's what stops the offer being re-sent in the same position."""
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE matches SET draw_offer_by=NULL "
            "WHERE id=? AND status='live' AND draw_offer_by IS NOT NULL", (match_id,))
        return cur.rowcount == 1


def accept_draw_if_offered(match_id: str, accepter_id: str) -> bool:
    """Agree to a standing draw offer and end the match, in ONE statement.

    Checking the offer and settling separately would leave a window where the
    offerer's move (which withdraws the offer) or a clock flag lands between
    the two, and the match would be recorded as an agreed draw that nobody
    currently agreed to — with the pot split accordingly. So the offer's
    existence is a WHERE clause on the settlement itself.

    `draw_offer_by <> ?` is the other half of "both must agree": without it,
    the player who made the offer could accept it themselves and take half the
    pot out of a game they were losing."""
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE matches SET status='settled', result='draw_agreed', winner_account_id=NULL, "
            "settled_ts=?, draw_offer_by=NULL "
            "WHERE id=? AND status='live' AND draw_offer_by IS NOT NULL AND draw_offer_by <> ?",
            (int(time.time()), match_id, accepter_id))
        return cur.rowcount == 1


def expire_match(match_id: str) -> bool:
    """Same guarded-transition contract as mark_match_live. `expired` means the
    funding window closed without both sides paying; any stake that DID land is
    still the depositor's — it's recoverable through the escrow's own CLTV
    reclaim branch, not by us."""
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE matches SET status='expired', result='deposit_timeout', settled_ts=? "
            "WHERE id=? AND status='awaiting_deposit'", (int(time.time()), match_id))
        return cur.rowcount == 1


# ── settlement ───────────────────────────────────────────────────────────
def save_settlement_build(match_id: str, *, tx_json: str, inputs: list[dict],
                          sigs_arb: list[str], pot_sompi: int, rake_sompi: int) -> bool:
    """Store the built settle tx, once. Guarded on `settle_tx_json IS NULL`:
    two players hitting Claim at the same moment must not each build their own
    tx, because the second build would replace the first and orphan any
    signature already collected against it. The loser of that race reads back
    the winner's tx and signs that instead."""
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE matches SET settle_tx_json=?, settle_inputs_json=?, settle_sigs_arb_json=?, "
            "settle_sigs_player_json=?, settle_pot_sompi=?, settle_rake_sompi=?, settle_prepared_ts=? "
            "WHERE id=? AND settle_tx_json IS NULL",
            (tx_json, json.dumps(inputs), json.dumps(sigs_arb),
             json.dumps([None] * len(inputs)), pot_sompi, rake_sompi,
             int(time.time()), match_id))
        return cur.rowcount == 1


def save_settlement_sigs(match_id: str, sigs_player: list) -> bool:
    """Write back the player-signature array. Guarded on the tx still being the
    one those signatures were made against — a signature is only valid for the
    exact tx it signed, so if the build changed underneath, these are worthless
    and must not be stored as if they weren't."""
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE matches SET settle_sigs_player_json=? WHERE id=? AND settle_txid IS NULL",
            (json.dumps(sigs_player), match_id))
        return cur.rowcount == 1


def mark_settlement_broadcast(match_id: str, txid: str) -> bool:
    """Guarded: only the first call records a txid. Two tabs (or a double
    click) finishing the signature set at once must not both submit — the
    second would be a double-spend attempt against an already-spent escrow."""
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE matches SET settle_txid=?, settle_broadcast_ts=? "
            "WHERE id=? AND settle_txid IS NULL", (txid, int(time.time()), match_id))
        return cur.rowcount == 1


# ── reclaim ──────────────────────────────────────────────────────────────
def mark_reclaim_broadcast(match_id: str, side: str, txid: str) -> bool:
    """Record which tx drained this player's escrow. Guarded on the column
    still being NULL so a double-click can't overwrite the real txid with a
    second (rejected) attempt's — the first submission is the one that spent
    the UTXO, and that's the receipt the player should keep."""
    if side not in ("a", "b"):
        raise ValueError("side must be 'a' or 'b'")
    col = f"reclaim_{side}_txid"
    with _lock, _conn() as c:
        cur = c.execute(f"UPDATE matches SET {col}=? WHERE id=? AND {col} IS NULL",
                        (txid, match_id))
        return cur.rowcount == 1


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
