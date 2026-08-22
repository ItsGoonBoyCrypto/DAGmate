"""Tests for the game clocks (clocks.py) — run: python tools/test_clocks.py

Same shape as test_deposits.py: dependency-free, real schema, real accessors,
throwaway DB. Only the outbound DMs are stubbed.

Why the clock earns tests: it is the second money-critical path in this backend.
A flag decides who collects the pot, and the timing arithmetic runs against a
wall clock that no test can slow down, so the only way to exercise a two-hour
think is to inject the timestamp. Everything here takes `at_ms` explicitly for
exactly that reason.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DAGMATE_SITE_DB"] = os.path.join(tempfile.mkdtemp(prefix="dagmate-clk-"), "t.db")

import bot_client  # noqa: E402
import chess_logic  # noqa: E402
import clocks  # noqa: E402
import config  # noqa: E402
import database as db  # noqa: E402

STAKE = 10 * config.SOMPI_PER_KAS
MIN = 60_000

_failures: list[str] = []
_dms: list[tuple[str, str]] = []


def check(name: str, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        _failures.append(name)


async def _dm_settled(account_id, match_id, summary):
    _dms.append(("settled", summary))


async def _dm_warning(account_id, match_id, remaining, url):
    _dms.append(("warning", remaining))


bot_client.notify_settled = _dm_settled
bot_client.notify_clock_warning = _dm_warning


def new_match(mode="rapid", fen=chess_logic.STARTING_FEN) -> str:
    acct_a = db.get_or_create_account(f"kaspatest:pA{time.time_ns()}", "aa")
    acct_b = db.get_or_create_account(f"kaspatest:pB{time.time_ns()}", "bb")
    m = db.create_match(challenge_id=None, tournament_id=None, round_no=None,
                        player_a_account_id=acct_a["id"], player_b_account_id=acct_b["id"],
                        stake_sompi=STAKE, mode=mode, fen=fen,
                        escrow_a={"address": "kaspatest:a", "redeemHex": "00"},
                        escrow_b={"address": "kaspatest:b", "redeemHex": "00"}, reclaim_daa=1)
    return m["id"]


def go_live(mid: str, now_ms: int, mode="rapid") -> dict:
    initial, inc = clocks.settings_for(mode)
    db.mark_match_live(mid, initial_ms=initial, increment_ms=inc, now_ms=now_ms)
    return db.get_match(mid)


async def main() -> int:
    db.ensure_schema()
    t0 = clocks.now_ms()

    print("an un-started clock reads full, never flagged")
    m = db.get_match(new_match())
    check("white full", clocks.remaining_ms(m, t0)[0], 10 * MIN)
    check("not flagged while awaiting deposit", clocks.flagged_color(m, t0), None)

    print("going live starts the clock in the same write")
    mid = new_match()
    m = go_live(mid, t0)
    check("status live", m["status"], "live")
    check("white banked", m["clock_white_ms"], 10 * MIN)
    check("increment stored", m["clock_increment_ms"], 5_000)
    check("turn started stamped", m["clock_turn_started_ms"], t0)

    print("only the side to move burns time")
    after = t0 + 90_000
    white, black = clocks.remaining_ms(m, after)
    check("white charged 90s", white, 10 * MIN - 90_000)
    check("black untouched", black, 10 * MIN)
    check("black can't flag on white's turn", clocks.flagged_color(m, t0 + 60 * MIN), "white")

    print("a move banks the remainder plus the increment")
    charged = clocks.charge_move(m, "white", after)
    check("banked = left + 5s", charged, 10 * MIN - 90_000 + 5_000)
    st = chess_logic.apply_uci(m["fen"], "e2e4")
    moves = json.loads(m["moves_json"]) + ["e2e4"]
    check("move committed", db.apply_move_with_clock(
        mid, st["fen"], moves, st["turn"], mover_color="white",
        mover_remaining_ms=charged, now_ms=after), True)
    m = db.get_match(mid)
    check("turn is black", m["turn"], "black")
    check("white's bank updated", m["clock_white_ms"], charged)
    check("clock restarted for black", m["clock_turn_started_ms"], after)
    check("black now burns time", clocks.remaining_ms(m, after + 30_000)[1], 10 * MIN - 30_000)
    check("white now frozen", clocks.remaining_ms(m, after + 30_000)[0], charged)

    print("a duplicate move racing the same position is rejected")
    check("second write does not match", db.apply_move_with_clock(
        mid, st["fen"], moves, "black", mover_color="white",
        mover_remaining_ms=999, now_ms=after + 1), False)
    check("board unchanged", db.get_match(mid)["clock_white_ms"], charged)

    print("flagging loses the match")
    _dms.clear()
    mid = new_match()
    m = go_live(mid, t0)
    flag_at = t0 + 10 * MIN + 1
    check("white has flagged", clocks.flagged_color(m, flag_at), "white")
    check("forfeit fires", await clocks.forfeit_if_flagged(m, flag_at), True)
    m = db.get_match(mid)
    check("settled", m["status"], "settled")
    check("result", m["result"], "timeout")
    check("black collects", m["winner_account_id"], m["player_b_account_id"])
    check("both players told once", len(_dms), 2)

    print("a second forfeit call can't settle it twice")
    _dms.clear()
    check("guarded write rejects", await clocks.forfeit_if_flagged(m, flag_at + 1000), False)
    check("no second DM", _dms, [])

    print("flagging against a lone king is a draw (FIDE 6.9)")
    # Black has only a king, so it cannot mate — white running out is a draw,
    # not a loss. In a wagered game this is split-the-pot vs lose-the-pot.
    mid = new_match(fen="4k3/8/8/8/8/8/4P3/4K3 w - - 0 1")
    m = go_live(mid, t0)
    check("forfeit fires", await clocks.forfeit_if_flagged(m, flag_at), True)
    m = db.get_match(mid)
    check("draw, not a loss", m["result"], "draw_timeout")
    check("nobody collects", m["winner_account_id"], None)

    print("a settled match never flags again")
    check("no clock on a settled match", clocks.flagged_color(m, flag_at + 10 * MIN), None)
    check("clock reads frozen", clocks.public(m, flag_at)["running"], False)

    print("the low-time warning DMs once, to the side to move only")
    _dms.clear()
    mid = new_match()
    m = go_live(mid, t0)
    low_at = t0 + 9 * MIN + 30_000  # 30s left of 10min = under the 10% warn line
    await clocks._warn_if_low(m, low_at)
    check("warned", [d[0] for d in _dms], ["warning"])
    check("remaining rendered for a human", _dms[0][1], "30s")
    await clocks._warn_if_low(db.get_match(mid), low_at + 1000)
    check("does not warn twice", len(_dms), 1)
    check("black not warned", db.get_match(mid)["clock_warned_black"], 0)

    print("no warning while there's still plenty of time")
    _dms.clear()
    mid = new_match()
    m = go_live(mid, t0)
    await clocks._warn_if_low(m, t0 + MIN)
    check("silent", _dms, [])

    print("daily mode is the same mechanism, just slower")
    mid = new_match(mode="daily")
    m = go_live(mid, t0, mode="daily")
    check("3 day bank", m["clock_white_ms"], 3 * 24 * 3600 * 1000)
    check("12h increment", m["clock_increment_ms"], 12 * 3600 * 1000)
    check("a 2h think is nothing here", clocks.flagged_color(m, t0 + 2 * 3600 * 1000), None)
    check("but 3 days is not", clocks.flagged_color(m, t0 + 3 * 24 * 3600 * 1000 + 1), "white")

    print("an unknown mode still gets a clock")
    check("falls back to rapid", clocks.settings_for("blitz-9000"), (10 * MIN, 5_000))

    print("the client is given the server's own clock to count from")
    mid = new_match()
    m = go_live(mid, t0)
    pub = clocks.public(m, t0 + 5_000)
    check("server timestamp sent", pub["serverNowMs"], t0 + 5_000)
    check("running", pub["running"], True)
    check("white ticking", pub["whiteMs"], 10 * MIN - 5_000)
    check("label", pub["label"], "Rapid 10+5")

    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        return 1
    print("all clock checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
