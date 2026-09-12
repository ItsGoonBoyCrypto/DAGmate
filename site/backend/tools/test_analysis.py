"""Tests for engine-cheat analysis (integrity Phase 2 Part B). Run: python tools/test_analysis.py

The scorer is engine-agnostic, so a MOCK engine (no Stockfish needed) drives it deterministically:
  - a player who always plays the engine's top move, accurately and consistently, scores ABOVE the hold
    threshold; a player who plays off-best with real, varied losses scores well BELOW it;
  - too few decision plies -> no score (a short game is not evidence);
  - run_and_record FAILS OPEN when Stockfish is missing (stamps the match analysed, does NOT hold), so an
    analysis outage can never brick a payout.
"""
from __future__ import annotations
import json, os, random, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DAGMATE_SITE_DB"] = os.path.join(tempfile.mkdtemp(prefix="dagmate-an-"), "t.db")

import config  # noqa: E402
config.DB_PATH = os.environ["DAGMATE_SITE_DB"]
assert config.DB_PATH.startswith(tempfile.gettempdir()), f"DB not isolated: {config.DB_PATH}"

import chess  # noqa: E402
import database as db  # noqa: E402
import analysis  # noqa: E402

_fail = []
def ck(name, got, want=True):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f" - got {got!r} want {want!r}"))
    if not ok: _fail.append(name)


def gen_game(min_plies):
    """A deterministic legal game of at least min_plies plies."""
    for seed in range(1000):
        b = chess.Board(); rng = random.Random(seed); moves = []
        for _ in range(min_plies + 12):
            if b.is_game_over():
                break
            mv = rng.choice(list(b.legal_moves)); moves.append(mv.uci()); b.push(mv)
        if len(moves) >= min_plies:
            return moves
    raise RuntimeError("could not generate a long enough game")


def mock_evaluate_factory(moves, *, cheat_color):
    """Build an evaluate(board, multipv) that, per position, makes the CHEAT colour's played move the clear
    top choice (near-zero loss, tight field) and the other colour's played move a sub-best one (real loss)."""
    lookup = {}
    b = chess.Board()
    for uci in moves:
        lookup[b.board_fen() + (" w" if b.turn else " b")] = (uci, b.turn)
        b.push(chess.Move.from_uci(uci))

    def evaluate(board, multipv):
        key = board.board_fen() + (" w" if board.turn else " b")
        played, mover = lookup[key]
        legal = [m.uci() for m in board.legal_moves]
        others = [u for u in legal if u != played]
        if mover == cheat_color:
            # played is best; a tight field just behind it -> top1 hit, ~0 loss, high weight
            return [(played, 30)] + [(u, 22) for u in others[:multipv - 1]]
        # human: some other move is best; played is clearly worse -> top1 miss, real loss
        if others:
            return [(others[0], 60), (played, 5)] + [(u, 0) for u in others[1:multipv - 1]]
        return [(played, 30)]
    return evaluate


def new_match(*, winner="a", stake=10 * config.SOMPI_PER_KAS, moves=None):
    a = db.get_or_create_account(f"kaspa:pA{time.time_ns()}", "pubA")
    b = db.get_or_create_account(f"kaspa:pB{time.time_ns()}", "pubB")
    m = db.create_match(challenge_id=None, tournament_id=None, round_no=None,
                        player_a_account_id=a["id"], player_b_account_id=b["id"],
                        stake_sompi=stake, mode="rapid", fen="startpos",
                        escrow_a={"address": "kaspa:eA", "redeemHex": "aa"},
                        escrow_b={"address": "kaspa:eB", "redeemHex": "bb"}, reclaim_daa=1)
    wid = {"a": a["id"], "b": b["id"], None: None}[winner]
    with db._lock, db._conn() as c:
        c.execute("UPDATE matches SET status='settled', result='checkmate', winner_account_id=?, "
                  "funded_a_sompi=?, funded_b_sompi=?, moves_json=? WHERE id=?",
                  (wid, stake, stake, json.dumps(moves or []), m["id"]))
    return m["id"], a, b


def main_():
    db.ensure_schema()
    moves = gen_game(48)   # long enough for 12+ decision plies each colour

    print("a top-move, accurate, consistent player scores ABOVE the hold threshold")
    ev = mock_evaluate_factory(moves, cheat_color=chess.WHITE)
    res = analysis.analyze_game(ev, moves, None)
    white = res["white"]["score"]; black = res["black"]["score"]
    print(f"    white(cheat)={white}  black(human)={black}  threshold={config.CHEAT_HOLD_THRESHOLD}")
    ck("cheat (white) has enough plies to score", res["white"]["score"] is not None, True)
    ck("cheat score is at/above the hold threshold", white >= config.CHEAT_HOLD_THRESHOLD, True)
    ck("cheat top-1 rate is ~1.0", res["white"]["top1Rate"] >= 0.95, True)

    print("a normal, off-best, varied player scores WELL BELOW the threshold")
    ck("human (black) scored", black is not None, True)
    ck("human score below threshold", black < config.CHEAT_HOLD_THRESHOLD, True)
    ck("cheat scores clearly higher than human", white - black > 0.3, True)

    print("too few decision plies -> no score (a short game is not evidence)")
    short = gen_game(20)[:22]
    r2 = analysis.analyze_game(mock_evaluate_factory(short, cheat_color=chess.WHITE), short, None)
    ck("short game white score is None", r2["white"]["score"], None)

    print("run_and_record (injected engine): a blatant winner is HELD and both wallets accrue their score")
    ev_full = mock_evaluate_factory(moves, cheat_color=chess.WHITE)
    mid_h, ah, bh = new_match(winner="a", moves=moves)   # white wins and 'cheats'
    analysis.run_and_record(mid_h, evaluate=ev_full)
    mh = db.get_match(mid_h)
    ck("blatant winning game is held", mh["review_status"], "held")
    ck("winner's high score stored", mh["cheat_score"] >= config.CHEAT_HOLD_THRESHOLD)
    ck("white wallet accrued one game", db.cheat_aggregate(ah["id"])["games"], 1)
    ck("black wallet accrued one game", db.cheat_aggregate(bh["id"])["games"], 1)

    print("aggregate trigger: a sustained per-wallet pattern holds even when no single game trips the bar")
    config.CHEAT_HOLD_THRESHOLD = 0.999      # single-game trigger effectively off
    config.CHEAT_AGG_MIN_GAMES = 5
    config.CHEAT_AGG_THRESHOLD = 0.80
    mid_a, aa, ba = new_match(winner="a", moves=moves)
    for _ in range(4):
        db.accumulate_cheat_score(aa["id"], 0.85)   # a prior pattern, none individually damning
    analysis.run_and_record(mid_a, evaluate=mock_evaluate_factory(moves, cheat_color=chess.WHITE))
    ma = db.get_match(mid_a)
    ck("held on the per-wallet pattern", ma["review_status"], "held")
    ck("hold reason is aggregate, not single", json.loads(ma["analysis_json"]).get("_holdReason"), "aggregate")
    config.CHEAT_HOLD_THRESHOLD = 0.90       # restore

    print("run_and_record FAILS OPEN when Stockfish is missing")
    config.ANALYSIS_ENABLED = True
    config.STOCKFISH_PATH = os.path.join(tempfile.gettempdir(), "no_such_stockfish_binary_xyz")
    mid, a, b = new_match(winner="a", moves=moves)
    analysis.run_and_record(mid)
    m = db.get_match(mid)
    ck("match stamped analysed (gate releases)", m["analyzed_ts"] is not None, True)
    ck("failed analysis did NOT hold the pot", m["review_status"], None)

    print("is_awaiting_analysis: on within window, off once analysed / when disabled")
    mid2, _, _ = new_match(winner="a", moves=moves)
    with db._lock, db._conn() as c:
        c.execute("UPDATE matches SET settled_ts=? WHERE id=?", (int(time.time()), mid2))
    ck("awaiting while fresh + unanalysed", analysis.is_awaiting_analysis(db.get_match(mid2)), True)
    db.record_analysis(mid2, 0.1, "{}", hold=False)
    ck("not awaiting once analysed", analysis.is_awaiting_analysis(db.get_match(mid2)), False)
    config.ANALYSIS_ENABLED = False
    ck("not awaiting when disabled", analysis.is_awaiting_analysis(db.get_match(mid)), False)

    print()
    print(f"{len(_fail)} FAILED: {_fail}" if _fail else "all analysis checks passed")
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main_())
