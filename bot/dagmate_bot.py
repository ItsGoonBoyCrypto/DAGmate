"""dagger_chess.py — DAGmate (CHESS_SPEC.md).

P2P wagered chess in the TG bot: real 2-of-3 P2SH escrow on Kaspa mainnet
(one address per player, CHESS_SPEC.md §2), pay-per-move anchor txs, an
honest-centralized arbiter (this bot, via a per-match HD key the sidecar
derives — see kron-service/src/chess.js) settling the pot on mate/resign/flag/
draw. Full rules (legality, mate, draws, threefold/50-move claims) come from
python-chess — there is deliberately NO on-chain mate detection; python-chess
already rejects illegal moves before they touch state (CHESS_SPEC.md §4.5).

NAMING: this file is NOT named chess.py on purpose. dagger-bot/ is on
sys.path (it's the script's own directory), so a local chess.py would shadow
the pip `chess` (python-chess) package for every module in the process —
`import chess` from inside a same-named local file resolves to the
half-initialized local module via sys.modules, not the library. Renaming this
module is the fix; CHESS_SPEC.md's `chess.py` filename is aspirational only.

Standalone module (imports only config/database/trade_guard/kron_venue —
never `handlers`), same pattern as bloodline.py: it registers its own
commands/callbacks directly on `app` in bot.py rather than living inside the
giant ConversationHandler, because the challenge → fund → live-game → settle
flow doesn't fit that state machine.

Money-rail note: escrow funding ("Stake" button) is a real KAS outflow, so it
reuses trade_guard's exported guard PRIMITIVES (require_session,
check_spend_limits, wallet_index) directly rather than going through
trade_guard.execute_withdraw — that wrapper's address whitelist and
platform-wide withdrawal cap are both OFF-platform protections and don't apply
here: the escrow address is protocol-generated (not attacker-controlled) and
the funds stay in Dagger's custody (the arbiter key) until settlement, they
never leave the platform.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import os
import time

import chess as pychess
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from telegram.error import Forbidden, BadRequest

import config
import database as db
import trade_guard
import kron_venue as kv
import market_tracker as mt

log = logging.getLogger(__name__)

DAA_PER_SEC = 10  # Kaspa: ~10 blocks/sec (CHESS_SPEC.md §2.3)

STATES_LIVE_LIKE = ("LIVE",)  # states the clock watcher scans


# ── config (read straight from the environment, like bloodline.py — so a
#    config.py redeploy can never strip these and crash a live match) ─────────
def _enabled() -> bool:
    return os.getenv("CHESS_ENABLED", "1") == "1"


def _public() -> bool:
    return os.getenv("CHESS_PUBLIC", "0") == "1"


def _allowlist() -> set[int]:
    raw = os.getenv("CHESS_ALLOWLIST", "")
    out = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


def _max_stake_kas() -> float:
    return float(os.getenv("CHESS_MAX_STAKE_KAS", "500") or 500)


def _rake_bps() -> int:
    return int(os.getenv("CHESS_RAKE_BPS", "200") or 200)


def _move_fee_kas() -> float:
    return float(os.getenv("CHESS_MOVE_FEE_KAS", "0.1") or 0.1)


def _reclaim_days() -> int:
    return int(os.getenv("CHESS_RECLAIM_DAYS", "14") or 14)


def _fund_expiry_hours() -> int:
    return int(os.getenv("CHESS_FUND_EXPIRY_HOURS", "24") or 24)


def _daily_hours() -> int:
    return int(os.getenv("CHESS_DAILY_HOURS", "24") or 24)


def _rapid_main_min() -> int:
    return int(os.getenv("CHESS_RAPID_MAIN_MIN", "10") or 10)


def _rapid_incr_s() -> int:
    return int(os.getenv("CHESS_RAPID_INCREMENT_S", "5") or 5)


def _is_owner(update: Update) -> bool:
    u = update.effective_user
    return bool(u and config.OWNER_USER_ID and u.id == config.OWNER_USER_ID)


def _gated_user(user_id: int) -> bool:
    return bool(user_id == config.OWNER_USER_ID or user_id in _allowlist())


def _open_for(update: Update) -> bool:
    """Public when CHESS_PUBLIC=1; else owner/allowlist can always test."""
    u = update.effective_user
    return _public() or bool(u and _gated_user(u.id))


# ── schema ────────────────────────────────────────────────────────────────────
def ensure_schema():
    with db._lock, db._conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS chess_matches (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          challenger_id INTEGER NOT NULL, opponent_id INTEGER NOT NULL,
          challenger_white INTEGER NOT NULL,
          stake_kas REAL NOT NULL, mode TEXT NOT NULL,
          state TEXT NOT NULL,
          fen TEXT NOT NULL, move_deadline_ts INTEGER,
          clock_w_ms INTEGER, clock_b_ms INTEGER,
          escrow_a TEXT, escrow_b TEXT, redeem_a TEXT, redeem_b TEXT,
          arbiter_index INTEGER, reclaim_daa INTEGER,
          result TEXT, result_reason TEXT, winner_id INTEGER,
          settle_txid TEXT, anchors_on INTEGER DEFAULT 1,
          draw_offer_by INTEGER,
          created_ts INTEGER, ended_ts INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS chess_moves (
          match_id INTEGER, ply INTEGER, uci TEXT, san TEXT, fen_after TEXT,
          moved_ts INTEGER, anchor_txid TEXT,
          PRIMARY KEY (match_id, ply))""")


# ── DB helpers ────────────────────────────────────────────────────────────────
def _row(cur) -> dict | None:
    r = cur.fetchone()
    return dict(r) if r else None


def get_match(match_id: int) -> dict | None:
    with db._lock, db._conn() as c:
        return _row(c.execute("SELECT * FROM chess_matches WHERE id=?", (match_id,)))


def user_open_matches(user_id: int) -> list[dict]:
    """This user's matches that aren't in a terminal state — at most one LIVE
    match is meaningful for move routing (see msg_chess_move)."""
    with db._lock, db._conn() as c:
        rows = c.execute(
            "SELECT * FROM chess_matches WHERE (challenger_id=? OR opponent_id=?) "
            "AND state NOT IN ('SETTLED','ABORTED','REFUNDED','DECLINED','EXPIRED') "
            "ORDER BY created_ts DESC", (user_id, user_id)).fetchall()
        return [dict(r) for r in rows]


def user_match_history(user_id: int, limit: int = 20) -> list[dict]:
    with db._lock, db._conn() as c:
        rows = c.execute(
            "SELECT * FROM chess_matches WHERE (challenger_id=? OR opponent_id=?) "
            "AND state IN ('SETTLED','ABORTED','REFUNDED') "
            "ORDER BY ended_ts DESC LIMIT ?", (user_id, user_id, limit)).fetchall()
        return [dict(r) for r in rows]


def _claim_state(match_id: int, expect: str, new: str) -> bool:
    """Atomic state transition — same idiom as limit_orders._claim_order:
    the UPDATE's WHERE clause IS the compare-and-swap; rowcount==1 means THIS
    call won the race."""
    with db._lock, db._conn() as c:
        cur = c.execute("UPDATE chess_matches SET state=? WHERE id=? AND state=?",
                        (new, match_id, expect))
        return cur.rowcount == 1


def _set(match_id: int, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with db._lock, db._conn() as c:
        c.execute(f"UPDATE chess_matches SET {cols} WHERE id=?",
                 (*fields.values(), match_id))


def record_move(match_id: int, ply: int, uci: str, san: str, fen_after: str,
                anchor_txid: str | None = None):
    with db._lock, db._conn() as c:
        c.execute("INSERT OR REPLACE INTO chess_moves "
                 "(match_id, ply, uci, san, fen_after, moved_ts, anchor_txid) "
                 "VALUES (?,?,?,?,?,?,?)",
                 (match_id, ply, uci, san, fen_after, int(time.time()), anchor_txid))


def match_moves(match_id: int) -> list[dict]:
    with db._lock, db._conn() as c:
        rows = c.execute("SELECT * FROM chess_moves WHERE match_id=? ORDER BY ply",
                         (match_id,)).fetchall()
        return [dict(r) for r in rows]


# ── game helpers ──────────────────────────────────────────────────────────────
def _board(match: dict) -> pychess.Board:
    return pychess.Board(match["fen"])


def _white_id(match: dict) -> int:
    return match["challenger_id"] if match["challenger_white"] else match["opponent_id"]


def _black_id(match: dict) -> int:
    return match["opponent_id"] if match["challenger_white"] else match["challenger_id"]


def _color_of(match: dict, user_id: int) -> bool | None:
    if user_id == _white_id(match):
        return pychess.WHITE
    if user_id == _black_id(match):
        return pychess.BLACK
    return None


def _opponent_of(match: dict, user_id: int) -> int:
    return match["opponent_id"] if user_id == match["challenger_id"] else match["challenger_id"]


def render_board(match: dict, viewer_id: int) -> str:
    board = _board(match)
    color = _color_of(match, viewer_id)
    orientation = pychess.BLACK if color is pychess.BLACK else pychess.WHITE
    return board.unicode(borders=True, orientation=orientation)


def _clock_str(ms: int | None) -> str:
    if ms is None:
        return "—"
    ms = max(0, int(ms))
    if ms >= 3600_000:
        return f"{ms // 3600_000}h{(ms % 3600_000) // 60_000:02d}m"
    if ms >= 60_000:
        return f"{ms // 60_000}m{(ms % 60_000) // 1000:02d}s"
    return f"{ms // 1000}s"


def status_line(match: dict) -> str:
    board = _board(match)
    to_move = "White" if board.turn == pychess.WHITE else "Black"
    stake = match["stake_kas"]
    usd = mt.usd_str(stake)
    bits = [f"💰 {stake:g} KAS{usd} each way", f"Mode: {html.escape(match['mode'])}"]
    if match["mode"] == "rapid":
        bits.append(f"⏱ W {_clock_str(match['clock_w_ms'])} · B {_clock_str(match['clock_b_ms'])}")
    elif match["move_deadline_ts"]:
        left = int(match["move_deadline_ts"]) - int(time.time())
        bits.append(f"⏱ move deadline: {_clock_str(max(0, left) * 1000)} left")
    bits.append(f"To move: {to_move}" + (" (check!)" if board.is_check() else ""))
    return "\n".join(bits)


def _set_next_deadline(match_id: int, match: dict, mover_color: bool):
    """After a move, arm the NEXT side's deadline. mover_color = the side that
    just moved (chess.WHITE/chess.BLACK)."""
    now = int(time.time())
    if match["mode"] == "rapid":
        incr_ms = _rapid_incr_s() * 1000
        prior_deadline = int(match["move_deadline_ts"] or now)
        # Time left on the clock at the instant they moved IS their new
        # remaining budget (see module docstring math in CHESS_SPEC.md §4.4):
        # move_deadline_ts was armed as turn_start + remaining_at_turn_start,
        # so (deadline - now) is exactly what's left right now.
        mover_remaining_ms = max(0, prior_deadline - now) * 1000 + incr_ms
        if mover_color == pychess.WHITE:
            other_remaining = int(match["clock_b_ms"] or 0)
            _set(match_id, clock_w_ms=mover_remaining_ms,
                move_deadline_ts=now + other_remaining // 1000)
        else:
            other_remaining = int(match["clock_w_ms"] or 0)
            _set(match_id, clock_b_ms=mover_remaining_ms,
                move_deadline_ts=now + other_remaining // 1000)
    else:
        _set(match_id, move_deadline_ts=now + _daily_hours() * 3600)


# ── wallet / escrow ───────────────────────────────────────────────────────────
async def _pubkey(user_id: int) -> str:
    return await asyncio.to_thread(kv.chess_pubkey, trade_guard.wallet_index(user_id))


async def _build_escrows(match_id: int, challenger_id: int, opponent_id: int) -> dict:
    """Compute both players' escrow addresses + the shared reclaim deadline.
    Pure/no-signing sidecar calls — safe to redo if it ever needs a retry."""
    pk_challenger, pk_opponent, daa = await asyncio.gather(
        _pubkey(challenger_id), _pubkey(opponent_id),
        asyncio.to_thread(kv.chess_daa_score))
    reclaim_daa = int(daa) + _reclaim_days() * 24 * 3600 * DAA_PER_SEC
    # pkA/pkB convention: A = challenger, B = opponent (arbitrary but must be
    # consistent between the two /chess/escrow calls and chess_settle's split path).
    esc_challenger, esc_opponent = await asyncio.gather(
        asyncio.to_thread(kv.chess_escrow, match_id, pk_challenger, pk_opponent, True, reclaim_daa),
        asyncio.to_thread(kv.chess_escrow, match_id, pk_challenger, pk_opponent, False, reclaim_daa))
    return {
        "escrow_a": esc_challenger["address"], "redeem_a": esc_challenger["redeemHex"],
        "escrow_b": esc_opponent["address"], "redeem_b": esc_opponent["redeemHex"],
        "arbiter_index": match_id, "reclaim_daa": reclaim_daa,
    }


async def _escrow_kas_balance(address: str) -> float:
    try:
        b = await asyncio.to_thread(kv.balance, address)
        return kv.sompi_to_kas(b.get("kasSompi", "0"))
    except Exception as e:
        log.warning(f"chess escrow balance {address}: {e}")
        return 0.0


async def _fund_escrow(user_id: int, escrow_address: str, kas: float) -> dict:
    """Send `kas` from the user's own Dagger wallet into their match escrow.
    Ordering mirrors trade_guard's documented rule — session, shape, balance
    (before the lock, to fail fast), THEN the lock held across the spend-limit
    check and the execute (closes the TOCTOU two concurrent taps could exploit
    to double-fund past a spend cap): this is a real outflow, it just isn't an
    off-platform withdrawal (see module docstring)."""
    s = trade_guard.require_session(user_id)
    if kas <= 0 or kas > _max_stake_kas():
        raise trade_guard.GuardError(400, "bad_stake", "Bad stake amount.")
    bal = await trade_guard.kas_balance(s.kaspa_address)
    if bal < kas + 0.05:
        raise trade_guard.GuardError(400, "insufficient_balance",
                                     f"Need {kas:g} KAS + fee, you have {bal:.4f} KAS.")
    async with db.get_spend_lock(user_id):
        await trade_guard.check_spend_limits(user_id, kas)
        idx = trade_guard.wallet_index(user_id)
        res = await asyncio.to_thread(kv.withdraw, idx, escrow_address, kas, submit=True)
    return res


# ── settlement (CHESS_SPEC.md §4.6) ──────────────────────────────────────────
async def execute_chess_settle(match_id: int, *, winner_id: int | None, split: bool,
                                reason: str) -> dict:
    """One settle per match, ever. Atomic LIVE→SETTLING claim BEFORE any money
    moves (same idiom as limit_orders._claim_order), then trade_guard's
    idempotency wrapper so a retried call (crash mid-flight, watcher double-fire)
    replays the first outcome instead of double-spending the escrow. user_id=0
    is a synthetic "system/arbiter" identity — settlement isn't a user action,
    but with_idempotency's key table needs SOME user_id to namespace under."""
    if not _claim_state(match_id, "LIVE", "SETTLING"):
        # Someone else already claimed it (or it's not LIVE) — not an error,
        # just nothing for THIS caller to do.
        return {"claimed": False}

    async def _run():
        match = get_match(match_id)
        escrows = []
        bal_a = await _escrow_kas_balance(match["escrow_a"])
        bal_b = await _escrow_kas_balance(match["escrow_b"])
        if bal_a > 0:
            escrows.append({"address": match["escrow_a"], "redeemHex": match["redeem_a"],
                            "depositorIndex": trade_guard.wallet_index(match["challenger_id"])})
        if bal_b > 0:
            escrows.append({"address": match["escrow_b"], "redeemHex": match["redeem_b"],
                            "depositorIndex": trade_guard.wallet_index(match["opponent_id"])})
        if not escrows:
            raise trade_guard.GuardError(400, "no_funds", "Neither escrow was ever funded.")

        rake_sompi = 0 if len(escrows) < 2 else int(match["stake_kas"] * 2 * kv.SOMPI * _rake_bps() / 10_000)
        kwargs = dict(matchId=match_id, escrows=escrows, rake_sompi=rake_sompi, submit=True)
        if split or len(escrows) < 2:
            # A genuine draw (both funded) splits the pot; a lone funded escrow
            # has nothing to split against, so it just goes back to its own
            # depositor — same "winner" code path, no rake either way.
            if len(escrows) < 2:
                kwargs = dict(matchId=match_id, escrows=escrows, rake_sompi=0, submit=True,
                             winner_index=escrows[0]["depositorIndex"])
            else:
                kwargs["split"] = True
        else:
            kwargs["winner_index"] = trade_guard.wallet_index(winner_id)

        res = await asyncio.to_thread(kv.chess_settle, **kwargs)
        is_draw = split or len(escrows) < 2
        _set(match_id, state="SETTLED", result=("draw" if is_draw else "win"),
            result_reason=reason, settle_txid=res.get("txid"), ended_ts=int(time.time()),
            winner_id=(None if is_draw else winner_id))
        return res

    try:
        return await trade_guard.with_idempotency(0, f"chess:{match_id}:settle", "chess_settle", _run)
    except Exception:
        # Leave state=SETTLING (not back to LIVE) — a half-settled escrow needs
        # eyes-on before anything auto-retries a fund-moving call. Surfaced to
        # the owner via /chessstats; safe to re-drive manually once diagnosed.
        log.error(f"chess settle FAILED match={match_id} reason={reason}", exc_info=True)
        raise


async def execute_chess_abort(match_id: int, reason: str) -> dict:
    """Unfunded/partially-funded matches that never went LIVE, or a grace-window
    flag (CHESS_SPEC.md §4.4): full refund, no rake, no game recorded."""
    match = get_match(match_id)
    if match["state"] not in ("FUNDING", "LIVE"):
        return {"claimed": False}
    if not _claim_state(match_id, match["state"], "SETTLING"):
        return {"claimed": False}

    async def _run():
        escrows = []
        bal_a = await _escrow_kas_balance(match["escrow_a"]) if match["escrow_a"] else 0
        bal_b = await _escrow_kas_balance(match["escrow_b"]) if match["escrow_b"] else 0
        if bal_a > 0:
            escrows.append({"address": match["escrow_a"], "redeemHex": match["redeem_a"],
                            "depositorIndex": trade_guard.wallet_index(match["challenger_id"])})
        if bal_b > 0:
            escrows.append({"address": match["escrow_b"], "redeemHex": match["redeem_b"],
                            "depositorIndex": trade_guard.wallet_index(match["opponent_id"])})
        if not escrows:
            _set(match_id, state="ABORTED", result_reason=reason, ended_ts=int(time.time()))
            return {"txid": None}
        if len(escrows) == 2:
            res = await asyncio.to_thread(kv.chess_settle, matchId=match_id, escrows=escrows,
                                          split=True, rake_sompi=0, submit=True)
        else:
            res = await asyncio.to_thread(kv.chess_settle, matchId=match_id, escrows=escrows,
                                          winner_index=escrows[0]["depositorIndex"], rake_sompi=0, submit=True)
        _set(match_id, state="REFUNDED", result_reason=reason, settle_txid=res.get("txid"),
            ended_ts=int(time.time()))
        return res

    try:
        return await trade_guard.with_idempotency(0, f"chess:{match_id}:abort", "chess_abort", _run)
    except Exception:
        log.error(f"chess abort FAILED match={match_id} reason={reason}", exc_info=True)
        raise


# ── move anchors (CHESS_SPEC.md §4.7, fire-and-forget) ───────────────────────
async def _anchor_move(match: dict, mover_id: int, ply: int, uci: str, fen_after: str):
    if not match["anchors_on"]:
        return
    digest = hashlib.blake2b(fen_after.encode(), digest_size=8).hexdigest()
    payload = f"DGCHS|1|{match['id']}|{ply}|{uci}|{digest}".encode().hex()
    fee_kas = _move_fee_kas()
    try:
        idx = trade_guard.wallet_index(mover_id)
        txid = await asyncio.to_thread(kv.chess_anchor, idx, payload,
                                       fee_sompi=kv.kas_to_sompi(fee_kas) if fee_kas > 0 else 0)
        with db._lock, db._conn() as c:
            c.execute("UPDATE chess_moves SET anchor_txid=? WHERE match_id=? AND ply=?",
                     (txid, match["id"], ply))
    except Exception as e:
        # Never blocks the game — a missed anchor is just a gap in the audit trail.
        log.warning(f"chess anchor failed match={match['id']} ply={ply}: {e}")


# ── UI ────────────────────────────────────────────────────────────────────────
def _kb_challenge(match_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accept", callback_data=f"chess:accept:{match_id}"),
        InlineKeyboardButton("❌ Decline", callback_data=f"chess:decline:{match_id}"),
    ]])


def _kb_fund(match_id: int, kas: float) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💰 Stake {kas:g} KAS", callback_data=f"chess:fund:{match_id}")],
        [InlineKeyboardButton("🔄 Refresh", callback_data=f"chess:refresh:{match_id}")],
    ])


def _kb_live(match: dict) -> InlineKeyboardMarkup:
    board = _board(match)
    row2 = [InlineKeyboardButton("🏳 Resign", callback_data=f"chess:resign:{match['id']}"),
            InlineKeyboardButton("½ Offer draw", callback_data=f"chess:draw:{match['id']}")]
    rows = [[InlineKeyboardButton("🔄 Refresh", callback_data=f"chess:refresh:{match['id']}")], row2]
    if board.can_claim_threefold_repetition() or board.can_claim_fifty_moves():
        rows.append([InlineKeyboardButton("⚖️ Claim draw", callback_data=f"chess:claim:{match['id']}")])
    if match.get("draw_offer_by"):
        rows.append([InlineKeyboardButton("🤝 Accept draw", callback_data=f"chess:drawyes:{match['id']}"),
                    InlineKeyboardButton("Decline", callback_data=f"chess:drawno:{match['id']}")])
    return InlineKeyboardMarkup(rows)


async def _send_board(bot: Bot, match: dict, viewer_id: int, *, header: str = ""):
    text = (f"{header}\n" if header else "") + \
           f"<pre>{html.escape(render_board(match, viewer_id))}</pre>\n{status_line(match)}"
    try:
        await bot.send_message(viewer_id, text, parse_mode=ParseMode.HTML,
                               reply_markup=_kb_live(match))
    except (Forbidden, BadRequest) as e:
        log.warning(f"chess DM to {viewer_id} failed: {e}")


# ── commands ──────────────────────────────────────────────────────────────────
async def cmd_chess(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _enabled():
        return await update.effective_message.reply_text("♟ Chess is offline right now.")
    if not _open_for(update):
        return await update.effective_message.reply_text("♟ Chess is in closed testing right now.")
    args = context.args or []
    user = update.effective_user
    if not args:
        matches = user_open_matches(user.id)
        if not matches:
            return await update.effective_message.reply_text(
                "♟ <b>DAGmate</b>\nChallenge someone: <code>/chess @user 50 [rapid|daily]</code>",
                parse_mode=ParseMode.HTML)
        lines = [f"#{m['id']} — {m['state']} — {m['stake_kas']:g} KAS" for m in matches]
        return await update.effective_message.reply_text(
            "♟ Your open matches:\n" + "\n".join(lines))

    if len(args) < 2:
        return await update.effective_message.reply_text(
            "Usage: <code>/chess @user 50 [rapid|daily]</code>", parse_mode=ParseMode.HTML)
    term, stake_str = args[0], args[1]
    mode = (args[2].lower() if len(args) > 2 else "daily")
    if mode not in ("daily", "rapid"):
        mode = "daily"
    try:
        stake = float(stake_str)
    except ValueError:
        return await update.effective_message.reply_text("Bad stake amount.")
    if stake <= 0 or stake > _max_stake_kas():
        return await update.effective_message.reply_text(
            f"Stake must be between 0 and {_max_stake_kas():g} KAS.")

    hits = await asyncio.to_thread(db.lookup_users, term, 1)
    if not hits:
        return await update.effective_message.reply_text(f"Couldn't find {html.escape(term)}.")
    opponent_id = int(hits[0]["user_id"])
    if opponent_id == user.id:
        return await update.effective_message.reply_text("You can't challenge yourself.")
    if not _public() and not (_gated_user(user.id) and _gated_user(opponent_id)):
        return await update.effective_message.reply_text(
            "♟ Chess is in closed testing — both players need to be on the allowlist.")

    now = int(time.time())
    with db._lock, db._conn() as c:
        cur = c.execute(
            "INSERT INTO chess_matches (challenger_id, opponent_id, challenger_white, "
            " stake_kas, mode, state, fen, created_ts) VALUES (?,?,?,?,?, 'OPEN', ?, ?)",
            (user.id, opponent_id, 1 if (now % 2 == 0) else 0, stake, mode,
             pychess.STARTING_FEN, now))
        match_id = cur.lastrowid

    try:
        await context.bot.send_message(
            opponent_id,
            f"♟ <b>{html.escape(user.first_name or 'Someone')}</b> challenged you to "
            f"{stake:g} KAS chess ({mode}). Accept?",
            parse_mode=ParseMode.HTML, reply_markup=_kb_challenge(match_id))
    except (Forbidden, BadRequest):
        _set(match_id, state="DECLINED", ended_ts=now)
        return await update.effective_message.reply_text(
            f"Couldn't reach {html.escape(term)} — they need to start the bot first.")

    await update.effective_message.reply_text(f"♟ Challenge #{match_id} sent — {stake:g} KAS, {mode}.")


async def cb_chess_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    match_id = int(q.data.split(":")[2])
    match = get_match(match_id)
    if not match or q.from_user.id != match["opponent_id"] or match["state"] != "OPEN":
        return await q.answer("Not available.", show_alert=True)
    await q.answer()
    try:
        esc = await _build_escrows(match_id, match["challenger_id"], match["opponent_id"])
    except Exception as e:
        log.error(f"chess escrow build failed match={match_id}: {e}", exc_info=True)
        return await q.edit_message_text("⚠️ Couldn't build the escrow — try again shortly.")
    if not _claim_state(match_id, "OPEN", "FUNDING"):
        return await q.edit_message_text("Already handled.")
    _set(match_id, **esc)
    match = get_match(match_id)
    await q.edit_message_text(f"♟ Accepted — fund your escrow to start (#{match_id}).")
    for uid in (match["challenger_id"], match["opponent_id"]):
        try:
            addr = match["escrow_a"] if uid == match["challenger_id"] else match["escrow_b"]
            await context.bot.send_message(
                uid,
                f"♟ Match #{match_id} — stake {match['stake_kas']:g} KAS.\n"
                f"Tap Stake to fund from your Dagger wallet, or send exactly "
                f"{match['stake_kas']:g} KAS to:\n<code>{addr}</code>\n"
                f"Unfunded matches expire in {_fund_expiry_hours()}h.",
                parse_mode=ParseMode.HTML, reply_markup=_kb_fund(match_id, match["stake_kas"]))
        except (Forbidden, BadRequest) as e:
            log.warning(f"chess fund DM to {uid} failed: {e}")


async def cb_chess_decline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    match_id = int(q.data.split(":")[2])
    match = get_match(match_id)
    if not match or q.from_user.id != match["opponent_id"] or match["state"] != "OPEN":
        return await q.answer("Not available.", show_alert=True)
    await q.answer()
    _claim_state(match_id, "OPEN", "DECLINED")
    _set(match_id, ended_ts=int(time.time()))
    await q.edit_message_text("Declined.")
    try:
        await context.bot.send_message(match["challenger_id"], f"♟ Challenge #{match_id} was declined.")
    except (Forbidden, BadRequest):
        pass


async def cb_chess_fund(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    match_id = int(q.data.split(":")[2])
    match = get_match(match_id)
    if not match or match["state"] != "FUNDING" or q.from_user.id not in (match["challenger_id"], match["opponent_id"]):
        return await q.answer("Not available.", show_alert=True)
    await q.answer("Sending…")
    addr = match["escrow_a"] if q.from_user.id == match["challenger_id"] else match["escrow_b"]
    try:
        res = await _fund_escrow(q.from_user.id, addr, match["stake_kas"])
    except trade_guard.GuardError as e:
        return await context.bot.send_message(q.from_user.id, f"⚠️ {e.message}")
    except Exception as e:
        log.error(f"chess fund failed match={match_id} uid={q.from_user.id}: {e}", exc_info=True)
        return await context.bot.send_message(q.from_user.id, "⚠️ Funding failed — try again.")
    await context.bot.send_message(q.from_user.id, f"✅ Staked. txid: <code>{res.get('txid')}</code>",
                                   parse_mode=ParseMode.HTML)
    await _maybe_go_live(context.bot, match_id)


async def _maybe_go_live(bot: Bot, match_id: int):
    match = get_match(match_id)
    if match["state"] != "FUNDING":
        return
    bal_a, bal_b = await asyncio.gather(
        _escrow_kas_balance(match["escrow_a"]), _escrow_kas_balance(match["escrow_b"]))
    if bal_a < match["stake_kas"] or bal_b < match["stake_kas"]:
        return
    now = int(time.time())
    if match["mode"] == "rapid":
        budget = _rapid_main_min() * 60_000
        _set(match_id, clock_w_ms=budget, clock_b_ms=budget, move_deadline_ts=now + budget // 1000)
    else:
        _set(match_id, move_deadline_ts=now + _daily_hours() * 3600)
    if not _claim_state(match_id, "FUNDING", "LIVE"):
        return
    match = get_match(match_id)
    for uid in (match["challenger_id"], match["opponent_id"]):
        await _send_board(bot, match, uid, header=f"♟ Match #{match_id} is LIVE!")


async def cb_chess_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    match_id = int(q.data.split(":")[2])
    match = get_match(match_id)
    if not match:
        return await q.answer("Gone.", show_alert=True)
    await q.answer()
    if match["state"] == "FUNDING":
        await _maybe_go_live(context.bot, match_id)
        match = get_match(match_id)
    if match["state"] == "LIVE":
        try:
            await q.edit_message_text(
                f"<pre>{html.escape(render_board(match, q.from_user.id))}</pre>\n{status_line(match)}",
                parse_mode=ParseMode.HTML, reply_markup=_kb_live(match))
        except BadRequest:
            pass  # message unchanged — Telegram no-ops this, not an error
    else:
        try:
            await q.edit_message_text(f"Match #{match_id}: {match['state']}")
        except BadRequest:
            pass


async def cb_chess_resign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split(":")
    match_id = int(parts[2])
    match = get_match(match_id)
    if not match or match["state"] != "LIVE" or q.from_user.id not in (match["challenger_id"], match["opponent_id"]):
        return await q.answer("Not available.", show_alert=True)
    if parts[1] == "resign":
        await q.answer()
        return await q.edit_message_reply_markup(InlineKeyboardMarkup([[
            InlineKeyboardButton("⚠️ Confirm resign", callback_data=f"chess:resignok:{match_id}"),
            InlineKeyboardButton("Cancel", callback_data=f"chess:refresh:{match_id}")]]))
    await q.answer("Resigning…")
    winner_id = _opponent_of(match, q.from_user.id)
    await execute_chess_settle(match_id, winner_id=winner_id, split=False, reason="resign")
    await _announce_result(context.bot, match_id)


async def cb_chess_draw_offer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    match_id = int(q.data.split(":")[2])
    match = get_match(match_id)
    if not match or match["state"] != "LIVE" or q.from_user.id not in (match["challenger_id"], match["opponent_id"]):
        return await q.answer("Not available.", show_alert=True)
    await q.answer("Draw offered.")
    _set(match_id, draw_offer_by=q.from_user.id)
    opp = _opponent_of(match, q.from_user.id)
    try:
        await context.bot.send_message(opp, f"♟ Match #{match_id}: draw offered.",
                                       reply_markup=InlineKeyboardMarkup([[
                                           InlineKeyboardButton("🤝 Accept", callback_data=f"chess:drawyes:{match_id}"),
                                           InlineKeyboardButton("Decline", callback_data=f"chess:drawno:{match_id}")]]))
    except (Forbidden, BadRequest):
        pass


async def cb_chess_draw_resp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split(":")
    match_id = int(parts[2])
    match = get_match(match_id)
    if not match or match["state"] != "LIVE" or q.from_user.id not in (match["challenger_id"], match["opponent_id"]):
        return await q.answer("Not available.", show_alert=True)
    if q.from_user.id == match.get("draw_offer_by"):
        return await q.answer("You offered it.", show_alert=True)
    await q.answer()
    _set(match_id, draw_offer_by=None)
    if parts[1] == "drawno":
        return await q.edit_message_text("Draw declined.")
    await q.edit_message_text("Draw accepted.")
    await execute_chess_settle(match_id, winner_id=None, split=True, reason="draw_agreed")
    await _announce_result(context.bot, match_id)


async def cb_chess_claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    match_id = int(q.data.split(":")[2])
    match = get_match(match_id)
    if not match or match["state"] != "LIVE" or q.from_user.id not in (match["challenger_id"], match["opponent_id"]):
        return await q.answer("Not available.", show_alert=True)
    board = _board(match)
    if not (board.can_claim_threefold_repetition() or board.can_claim_fifty_moves()):
        return await q.answer("No claim available.", show_alert=True)
    await q.answer()
    await execute_chess_settle(match_id, winner_id=None, split=True, reason="draw_claim")
    await _announce_result(context.bot, match_id)


async def _announce_result(bot: Bot, match_id: int):
    match = get_match(match_id)
    stake_usd = mt.usd_str(match["stake_kas"] * 2)
    for uid in (match["challenger_id"], match["opponent_id"]):
        try:
            if match["state"] == "SETTLED":
                if match["result"] == "draw":
                    text = f"🤝 Draw — pot split ({match['stake_kas']:g} KAS{stake_usd})."
                elif uid == match["winner_id"]:
                    text = f"🏆 You won {match['stake_kas'] * 2:g} KAS{stake_usd}!"
                else:
                    text = "😔 You lost."
                text += f" ({match['result_reason']})\ntxid: <code>{match['settle_txid']}</code>"
            else:
                text = f"Match #{match_id}: {match['state']}"
            await bot.send_message(uid, f"♟ {text}", parse_mode=ParseMode.HTML)
        except (Forbidden, BadRequest):
            pass


# ── move input (free text — see module docstring on handler ordering) ────────
async def msg_chess_move(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Only claims a message when the sender has EXACTLY ONE live match where
    it's their turn (CHESS_SPEC.md §4.3's explicit routing rule) — never
    swallows text another handler owns. Register this AFTER the main
    ConversationHandler in bot.py so an active conversation state still wins."""
    if not _enabled():
        return
    user = update.effective_user
    text = (update.effective_message.text or "").strip()
    if not text or text.startswith("/"):
        return
    with db._lock, db._conn() as c:
        rows = c.execute(
            "SELECT * FROM chess_matches WHERE state='LIVE' AND (challenger_id=? OR opponent_id=?)",
            (user.id, user.id)).fetchall()
    my_turn = []
    for r in rows:
        m = dict(r)
        board = _board(m)
        if _color_of(m, user.id) == board.turn:
            my_turn.append(m)
    if not my_turn:
        return  # not this handler's message — let it fall through (nothing else claims it either)
    if len(my_turn) > 1:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"#{m['id']}", callback_data=f"chess:refresh:{m['id']}")]
                                   for m in my_turn])
        return await update.effective_message.reply_text("Which match?", reply_markup=kb)

    match = my_turn[0]
    board = _board(match)
    move = None
    for parser in (board.parse_san, lambda t: pychess.Move.from_uci(t)):
        try:
            candidate = parser(text)
            if candidate in board.legal_moves:
                move = candidate
                break
        except Exception:
            continue
    if move is None:
        return await update.effective_message.reply_text("Illegal or unrecognised move.")

    san = board.san(move)
    uci = move.uci()
    mover_color = board.turn
    ply = len(match_moves(match["id"])) + 1
    board.push(move)
    fen_after = board.fen()
    _set(match["id"], fen=fen_after, draw_offer_by=None)
    record_move(match["id"], ply, uci, san, fen_after)
    asyncio.create_task(_anchor_move(match, user.id, ply, uci, fen_after))

    if board.is_checkmate():
        winner_id = user.id
        await execute_chess_settle(match["id"], winner_id=winner_id, split=False, reason="checkmate")
        return await _announce_result(context.bot, match["id"])
    if board.is_stalemate() or board.is_insufficient_material() or board.is_seventyfive_moves() or board.is_fivefold_repetition():
        await execute_chess_settle(match["id"], winner_id=None, split=True, reason="draw_auto")
        return await _announce_result(context.bot, match["id"])

    _set_next_deadline(match["id"], match, mover_color)
    match = get_match(match["id"])
    await update.effective_message.reply_text(f"✅ {san}")
    opp = _opponent_of(match, user.id)
    await _send_board(context.bot, match, opp, header=f"♟ Match #{match['id']}: {san}")


# ── history / stats ───────────────────────────────────────────────────────────
async def cmd_chessgames(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    rows = await asyncio.to_thread(user_match_history, user.id, 20)
    if not rows:
        return await update.effective_message.reply_text("No finished games yet.")
    lines = [f"#{r['id']} — {r['state']} — {r['stake_kas']:g} KAS — {r['result_reason'] or ''}"
            for r in rows]
    await update.effective_message.reply_text("♟ Your chess history:\n" + "\n".join(lines))


async def cmd_chessstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    with db._lock, db._conn() as c:
        counts = c.execute("SELECT state, COUNT(*) FROM chess_matches GROUP BY state").fetchall()
        volume = c.execute(
            "SELECT COALESCE(SUM(stake_kas*2),0) FROM chess_matches WHERE state='SETTLED'").fetchone()[0]
    lines = [f"{s}: {n}" for s, n in counts]
    lines.append(f"Settled pot volume: {volume:g} KAS")
    await update.effective_message.reply_text("♟ Chess stats:\n" + "\n".join(lines))


# ── background watchers ───────────────────────────────────────────────────────
async def chess_clock_loop(bot: Bot):
    """Scans LIVE matches every 30s for expired move_deadline_ts. First 2 plies
    (nobody has moved on either side yet, per CHESS_SPEC.md §4.4) are GRACE —
    a flag there aborts+refunds rather than awarding a win."""
    log.info("Chess clock watcher: started")
    while True:
        try:
            await asyncio.sleep(30)
            now = int(time.time())
            with db._lock, db._conn() as c:
                rows = c.execute(
                    "SELECT id FROM chess_matches WHERE state='LIVE' AND move_deadline_ts IS NOT NULL "
                    "AND move_deadline_ts <= ?", (now,)).fetchall()
            for (mid,) in rows:
                try:
                    match = get_match(mid)
                    if not match or match["state"] != "LIVE":
                        continue
                    plies = len(match_moves(mid))
                    if plies < 2:
                        await execute_chess_abort(mid, "grace_flag")
                        for uid in (match["challenger_id"], match["opponent_id"]):
                            try:
                                await bot.send_message(uid, f"♟ Match #{mid}: aborted (no move in time) — refunded.")
                            except (Forbidden, BadRequest):
                                pass
                        continue
                    board = _board(match)
                    flagged_color = board.turn  # side to move ran out of time
                    flagged_id = _white_id(match) if flagged_color == pychess.WHITE else _black_id(match)
                    winner_id = _opponent_of(match, flagged_id)
                    await execute_chess_settle(mid, winner_id=winner_id, split=False, reason="flag")
                    await _announce_result(bot, mid)
                except Exception as e:
                    log.error(f"chess clock watcher match={mid}: {e}", exc_info=True)
        except Exception as e:
            log.warning(f"chess clock watcher loop: {e}")
            await asyncio.sleep(5)


async def chess_funding_loop(bot: Bot):
    """FUNDING matches unfunded past CHESS_FUND_EXPIRY_HOURS auto-abort+refund
    whatever WAS deposited. Also polls for late-arriving funding so a match
    goes LIVE even if nobody taps Refresh."""
    log.info("Chess funding watcher: started")
    while True:
        try:
            await asyncio.sleep(60)
            now = int(time.time())
            with db._lock, db._conn() as c:
                rows = c.execute("SELECT id, created_ts FROM chess_matches WHERE state='FUNDING'").fetchall()
            for mid, created_ts in rows:
                try:
                    if now - int(created_ts) > _fund_expiry_hours() * 3600:
                        match = get_match(mid)
                        await execute_chess_abort(mid, "unfunded_expiry")
                        if match:
                            for uid in (match["challenger_id"], match["opponent_id"]):
                                try:
                                    await bot.send_message(uid, f"♟ Match #{mid}: expired unfunded — any stake refunded.")
                                except (Forbidden, BadRequest):
                                    pass
                    else:
                        await _maybe_go_live(bot, mid)
                except Exception as e:
                    log.error(f"chess funding watcher match={mid}: {e}", exc_info=True)
        except Exception as e:
            log.warning(f"chess funding watcher loop: {e}")
            await asyncio.sleep(5)
