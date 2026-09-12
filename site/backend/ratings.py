"""Glicko-2 ratings + leaderboard for DAGmate.

What gets rated: only a STAKED, played game between two distinct real (non-demo) wallets. A free game,
a no-show, a walkover, a deposit timeout, or a bye never moves a rating — that keeps ratings meaningful
AND is the first anti-Sybil line (a fake rating costs real staked, fee-paying games; see
docs/DAGMATE_INTEGRITY.md).

How: `flush_unrated()` walks settled-but-unrated matches in settled_ts order (Glicko-2 is sequential),
rating each exactly once (idempotent via matches.rated_ts). Each game is applied as a one-game rating
period per player, with rd first inflated for whole rating periods the wallet sat idle. It's called
best-effort after a settle and again when the leaderboard is read, so it's self-healing regardless of
which settle path ended the game.

Leaderboard: ranked by the CONSERVATIVE estimate rating − 2·RD (a wallet must be both highly rated and
well-established), gated on a minimum number of rated games. This is what stops a lucky/smurf newcomer
topping the board.
"""
from __future__ import annotations
import time

import config
import database as db
import glicko2

# Settled results that are NOT a played game, so never rated.
_NON_GAME = {"walkover", "no_show", "bye", "deposit_timeout"}


def is_rateable(m: dict) -> bool:
    if m.get("status") != "settled":
        return False
    if (m.get("stake_sompi") or 0) <= 0:            # free games are unrated
        return False
    a, b = m.get("player_a_account_id"), m.get("player_b_account_id")
    if not a or not b or a == b:
        return False
    res = (m.get("result") or "")
    if res in _NON_GAME:
        return False
    if m.get("winner_account_id"):                  # a decided win/loss
        return True
    return res.startswith("draw")                   # an actual drawn game


def _idle_periods(last_ts: int | None, now_ts: int) -> int:
    """Whole rating periods with NO game between a wallet's last rated game and this one."""
    if not last_ts:
        return 0
    return max(0, int((now_ts - last_ts) // config.RATING_PERIOD_SECS) - 1)


def _rate_one(match: dict) -> None:
    """Fold a single rateable match into both players' ratings. Assumes it hasn't been rated yet."""
    a = db.get_account(match["player_a_account_id"])
    b = db.get_account(match["player_b_account_id"])
    ts = match["settled_ts"] or int(time.time())
    if not a or not b or a["is_demo_wallet"] or b["is_demo_wallet"]:
        return  # caller still stamps rated_ts so we don't rescan it

    # Pre-game state (defaults cover any pre-migration row), rd inflated for idle periods.
    ra = float(a["rating"] if a["rating"] is not None else glicko2.DEFAULT_R)
    rda = glicko2.inflate(float(a["rd"] if a["rd"] is not None else glicko2.DEFAULT_RD),
                          float(a["vol"] if a["vol"] is not None else glicko2.DEFAULT_VOL),
                          _idle_periods(a["last_rating_ts"], ts))
    vola = float(a["vol"] if a["vol"] is not None else glicko2.DEFAULT_VOL)
    rb = float(b["rating"] if b["rating"] is not None else glicko2.DEFAULT_R)
    rdb = glicko2.inflate(float(b["rd"] if b["rd"] is not None else glicko2.DEFAULT_RD),
                          float(b["vol"] if b["vol"] is not None else glicko2.DEFAULT_VOL),
                          _idle_periods(b["last_rating_ts"], ts))
    volb = float(b["vol"] if b["vol"] is not None else glicko2.DEFAULT_VOL)

    # Score from A's (white's) perspective.
    if match["winner_account_id"] == a["id"]:
        score_a = 1.0
    elif match["winner_account_id"] == b["id"]:
        score_a = 0.0
    else:
        score_a = 0.5

    # Each player is rated against the OTHER's pre-game (inflated) rating.
    na = glicko2.rate(ra, rda, vola, [(rb, rdb, score_a)], tau=config.GLICKO_TAU)
    nb = glicko2.rate(rb, rdb, volb, [(ra, rda, 1.0 - score_a)], tau=config.GLICKO_TAU)
    db.update_account_rating(a["id"], na[0], na[1], na[2], ts)
    db.update_account_rating(b["id"], nb[0], nb[1], nb[2], ts)


def flush_unrated() -> int:
    """Rate every settled-but-unrated match in order. Idempotent + self-healing. Returns how many rateable
    games it applied. Best-effort: one bad row never blocks the rest (it's stamped and skipped)."""
    applied = 0
    for m in db.unrated_settled_matches():
        try:
            if is_rateable(m):
                _rate_one(m)
                applied += 1
        except Exception as e:  # never let a rating glitch wedge the queue or a request
            import logging
            logging.getLogger("dagmate.ratings").warning("rating skipped for %s: %s", m.get("id"), e)
        finally:
            db.mark_match_rated(m["id"], int(time.time()))
    return applied


def is_provisional(rd: float) -> bool:
    return rd is None or float(rd) > config.RATING_PROVISIONAL_RD


def account_rating_public(acct: dict) -> dict:
    """The rating block surfaced on a player/match/challenge — safe defaults for an unrated wallet."""
    rd = float(acct["rd"]) if acct.get("rd") is not None else glicko2.DEFAULT_RD
    games = int(acct.get("rated_games") or 0)
    return {
        "rating": round(float(acct["rating"]) if acct.get("rating") is not None else glicko2.DEFAULT_R),
        "rd": round(rd),
        "provisional": is_provisional(rd) or games == 0,
        "ratedGames": games,
    }


def leaderboard(limit: int | None = None) -> list[dict]:
    """Ranked board: flush pending ratings, then rank eligible wallets by rating − 2·RD (desc)."""
    flush_unrated()
    limit = limit or config.LEADERBOARD_SIZE
    accts = db.list_rated_accounts(config.LEADERBOARD_MIN_GAMES)
    if not accts:
        return []

    # W/L/D from rated staked matches.
    wld = {a["id"]: {"w": 0, "l": 0, "d": 0} for a in accts}
    for r in db.rated_match_results():
        pa, pb, win = r["player_a_account_id"], r["player_b_account_id"], r["winner_account_id"]
        drawn = not win and (r["result"] or "").startswith("draw")
        for pid in (pa, pb):
            if pid not in wld:
                continue
            if drawn:
                wld[pid]["d"] += 1
            elif win == pid:
                wld[pid]["w"] += 1
            elif win:
                wld[pid]["l"] += 1

    names = db.primary_names_for([a["address"] for a in accts])
    rows = []
    for a in accts:
        rd = float(a["rd"])
        rec = wld[a["id"]]
        played = rec["w"] + rec["l"] + rec["d"]
        rows.append({
            "address": a["address"],
            "name": names.get(a["address"]),
            "rating": round(float(a["rating"])),
            "rd": round(rd),
            "conservative": round(float(a["rating"]) - 2.0 * rd),  # the rank key
            "provisional": is_provisional(rd),
            "games": int(a["rated_games"]),
            "wins": rec["w"], "losses": rec["l"], "draws": rec["d"],
            "winRate": round(100.0 * rec["w"] / played) if played else 0,
        })
    rows.sort(key=lambda x: x["conservative"], reverse=True)
    for i, row in enumerate(rows[:limit], start=1):
        row["rank"] = i
    return rows[:limit]
