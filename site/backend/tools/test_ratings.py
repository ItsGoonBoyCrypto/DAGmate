"""Tests for Glicko-2 ratings + the leaderboard. Run: python tools/test_ratings.py

Covers: the pure Glicko-2 math against Glickman's OWN published worked example (the definitive vector);
idle-RD inflation; and the real rating pipeline on an isolated DB — only staked, played, two-real-player
games move ratings (free games / no-shows don't), the flush is idempotent and self-healing, and the
leaderboard ranks by rating - 2*RD behind a minimum-games gate.
"""
from __future__ import annotations
import os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DAGMATE_SITE_DB"] = os.path.join(tempfile.mkdtemp(prefix="dagmate-rt-"), "t.db")

import config  # noqa: E402
config.DB_PATH = os.environ["DAGMATE_SITE_DB"]
assert config.DB_PATH.startswith(tempfile.gettempdir())
config.LEADERBOARD_MIN_GAMES = 3   # small so the test can exercise the gate

import database as db  # noqa: E402
import chess_logic  # noqa: E402
import glicko2  # noqa: E402
import ratings  # noqa: E402

_fail = []
def ck(name, cond, extra=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{(' — ' + str(extra)) if extra else ''}")
    if not cond: _fail.append(name)

def near(a, b, tol): return abs(a - b) <= tol

def acct(tag):
    return db.get_or_create_account(f"kaspa:q{tag}", "pk" + tag)

def played_match(a, b, *, winner, stake=1_000_000, result="checkmate"):
    m = db.create_match(challenge_id=None, tournament_id=None, round_no=None,
                        player_a_account_id=a["id"], player_b_account_id=b["id"],
                        stake_sompi=stake, mode="rapid", fen=chess_logic.STARTING_FEN,
                        escrow_a=None, escrow_b=None, reclaim_daa=None)
    db.mark_match_live(m["id"], initial_ms=600000, increment_ms=0, now_ms=0)
    wid = None if winner == "draw" else (a["id"] if winner == "a" else b["id"])
    res = "draw" if winner == "draw" else result
    assert db.settle_match_if_live(m["id"], result=res, winner_account_id=wid)
    return m["id"]

def main_():
    db.ensure_schema()

    print("Glicko-2 math == Glickman's published worked example (tau=0.5)")
    r, rd, vol = glicko2.rate(1500, 200, 0.06,
                              [(1400, 30, 1.0), (1550, 100, 0.0), (1700, 300, 0.0)], tau=0.5)
    ck("rating ~= 1464.05", near(r, 1464.05, 0.1), round(r, 2))
    ck("RD ~= 151.52", near(rd, 151.52, 0.1), round(rd, 2))
    ck("volatility ~= 0.05999", near(vol, 0.05999, 0.0001), round(vol, 6))

    print("idle inflation grows RD, capped at 350")
    ck("more idle periods -> more RD", glicko2.inflate(50, 0.06, 10) > glicko2.inflate(50, 0.06, 1))
    ck("inflation capped at 350", glicko2.inflate(300, 0.06, 500) == 350.0)
    ck("zero idle = unchanged", glicko2.inflate(120, 0.06, 0) == 120.0 or near(glicko2.inflate(120,0.06,0),120,1e-9))

    print("a staked, played game moves BOTH ratings (winner up, loser down)")
    a, b = acct("A"), acct("B")
    played_match(a, b, winner="a")
    n = ratings.flush_unrated()
    ck("one rateable game applied", n == 1, n)
    a2, b2 = db.get_account(a["id"]), db.get_account(b["id"])
    ck("winner rating rose above seed", a2["rating"] > 1500, round(a2["rating"], 1))
    ck("loser rating fell below seed", b2["rating"] < 1500, round(b2["rating"], 1))
    ck("both counted one rated game", (a2["rated_games"], b2["rated_games"]) == (1, 1))
    ck("RD shrank from 350 (a real game reduces uncertainty)", a2["rd"] < 350)

    print("flush is idempotent — a second flush changes nothing")
    before = (db.get_account(a["id"])["rating"], db.get_account(a["id"])["rated_games"])
    ck("second flush applies 0 games", ratings.flush_unrated() == 0)
    after = (db.get_account(a["id"])["rating"], db.get_account(a["id"])["rated_games"])
    ck("rating + game count unchanged", before == after, (before, after))

    print("non-rateable games never move a rating (but are stamped so they aren't rescanned)")
    c, d = acct("C"), acct("D")
    played_match(c, d, winner="a", stake=0)                       # free game
    played_match(c, d, winner="a", result="deposit_timeout")      # no real game
    ck("no rateable games applied", ratings.flush_unrated() == 0)
    ck("C stayed at seed", db.get_account(c["id"])["rating"] == 1500)
    ck("nothing left unrated (all stamped)", len(db.unrated_settled_matches()) == 0)

    print("a draw pulls the two ratings toward each other")
    e, f = acct("E"), acct("F")
    played_match(e, f, winner="a"); played_match(e, f, winner="a"); ratings.flush_unrated()  # E >> F
    e_hi = db.get_account(e["id"])["rating"]
    played_match(e, f, winner="draw"); ratings.flush_unrated()
    ck("higher-rated player loses points on a draw with a weaker one", db.get_account(e["id"])["rating"] < e_hi)

    print("leaderboard: min-games gate + ranked by rating - 2*RD")
    # a,b have 1 game each -> below the gate of 3; give a enough games to qualify.
    for _ in range(3):
        played_match(a, b, winner="a")
    board = ratings.leaderboard()
    ids = {row["address"] for row in board}
    ck("A (>=3 games) is on the board", a["address"] in ids)
    ck("D (0 rated games) is NOT on the board", d["address"] not in ids)
    if board:
        cons = [row["conservative"] for row in board]
        ck("board is sorted by conservative estimate desc", cons == sorted(cons, reverse=True), cons)
        ck("rank numbers assigned from 1", board[0]["rank"] == 1)
        ck("conservative == rating - 2*RD", board[0]["conservative"] == round(board[0]["rating"] - 2*board[0]["rd"]))

    print("demo wallets are excluded from ratings")
    dm = db.get_or_create_account("kaspa:qDEMO", "pkdemo", is_demo=True)
    real = acct("G")
    played_match(dm, real, winner="a")
    ratings.flush_unrated()
    ck("a demo player's game doesn't rate the real opponent", db.get_account(real["id"])["rated_games"] == 0)

    print()
    print(f"{len(_fail)} FAILED: {_fail}" if _fail else "all rating checks passed")
    return 1 if _fail else 0

if __name__ == "__main__":
    raise SystemExit(main_())
