"""Tests for draw offers — run: python tools/test_draw.py

Same shape as the other suites: dependency-free, real schema, real accessors,
throwaway DB. Only the outbound DMs are stubbed.

Why draw offers earn tests despite looking like a UI nicety: agreeing a draw
is the one ending that MOVES MONEY on the say-so of two separate clicks, and
it splits a pot the winning side would otherwise take whole. That makes it the
most attractive thing on the board to forge. The four properties below are the
ones an attacker would go after, so each gets a test that would fail loudly if
the guard were ever relaxed into a read-then-write:

  1. You cannot accept your own offer — otherwise a losing player offers a
     draw and immediately takes half the pot without the opponent agreeing.
  2. Accepting is one statement — a check-then-settle would let a move or a
     clock flag land in between and record an agreement nobody currently held.
  3. Playing on withdraws the offer — an offer that outlives its position
     could be banked and cashed twenty moves later by whoever ends up losing.
  4. One offer per position — otherwise "decline" is just a button that makes
     the nag reappear.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DAGMATE_SITE_DB"] = os.path.join(tempfile.mkdtemp(prefix="dagmate-draw-"), "t.db")

import bot_client  # noqa: E402
import chess_logic  # noqa: E402
import clocks  # noqa: E402
import config  # noqa: E402
import database as db  # noqa: E402

STAKE = 10 * config.SOMPI_PER_KAS

_failures: list[str] = []
_dms: list[tuple[str, str]] = []


def check(name: str, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        _failures.append(name)


async def _dm_settled(account_id, match_id, summary):
    _dms.append(("settled", account_id))


async def _dm_draw_offer(account_id, match_id, url):
    _dms.append(("offer", account_id))


bot_client.notify_settled = _dm_settled
bot_client.notify_draw_offer = _dm_draw_offer

import main  # noqa: E402  (imported after the DB env + DM stubs are in place)
from fastapi import HTTPException  # noqa: E402


def new_live_match() -> tuple[dict, dict, dict]:
    """(match, accountA, accountB) with the clock already running."""
    a = db.get_or_create_account(f"kaspatest:pA{time.time_ns()}", "aa")
    b = db.get_or_create_account(f"kaspatest:pB{time.time_ns()}", "bb")
    m = db.create_match(challenge_id=None, tournament_id=None, round_no=None,
                        player_a_account_id=a["id"], player_b_account_id=b["id"],
                        stake_sompi=STAKE, mode="rapid", fen=chess_logic.STARTING_FEN,
                        escrow_a={"address": "kaspatest:a", "redeemHex": "00"},
                        escrow_b={"address": "kaspatest:b", "redeemHex": "00"}, reclaim_daa=1)
    initial, inc = clocks.settings_for("rapid")
    db.mark_match_live(m["id"], initial_ms=initial, increment_ms=inc, now_ms=clocks.now_ms())
    return db.get_match(m["id"]), a, b


def play(mid: str, uci: str):
    m = db.get_match(mid)
    st = chess_logic.apply_uci(m["fen"], uci)
    moves = json.loads(m["moves_json"]) + [uci]
    return db.apply_move_with_clock(mid, st["fen"], moves, st["turn"],
                                    mover_color=m["turn"],
                                    mover_remaining_ms=m[f"clock_{m['turn']}_ms"],
                                    now_ms=clocks.now_ms())


async def status_of(fn, *args) -> int:
    """The HTTP status a route rejects with, or 200 if it doesn't. Takes the
    callable rather than a coroutine because draw_decline is a plain `def`
    (it does no I/O) while the other two are async."""
    try:
        r = fn(*args)
        if asyncio.iscoroutine(r):
            await r
        return 200
    except HTTPException as e:
        return e.status_code


async def main_() -> int:
    db.ensure_schema()

    print("an offer stands, and says who made it")
    m, a, b = new_live_match()
    check("offer accepted", db.offer_draw(m["id"], a["id"], 0), True)
    m = db.get_match(m["id"])
    check("stored", m["draw_offer_by"], a["id"])
    check("published as a colour, not an id", main._match_public(m)["drawOffer"], {"byColor": "white"})
    check("no offer reads as None", main._match_public(new_live_match()[0])["drawOffer"], None)

    print("!! you cannot accept your own offer")
    # The whole point of "both must agree". If this ever passes, a losing
    # player offers a draw and takes half the pot on their own.
    check("self-accept refused", db.accept_draw_if_offered(m["id"], a["id"]), False)
    check("match still live", db.get_match(m["id"])["status"], "live")
    check("offer still standing", db.get_match(m["id"])["draw_offer_by"], a["id"])

    print("the opponent accepting ends it as a draw")
    check("accepted", db.accept_draw_if_offered(m["id"], b["id"]), True)
    m = db.get_match(m["id"])
    check("settled", m["status"], "settled")
    check("result", m["result"], "draw_agreed")
    check("nobody collects the pot", m["winner_account_id"], None)
    check("offer consumed", m["draw_offer_by"], None)

    print("a second accept can't settle it twice")
    check("guarded write rejects", db.accept_draw_if_offered(m["id"], a["id"]), False)

    print("!! playing on withdraws your offer")
    m, a, b = new_live_match()
    db.offer_draw(m["id"], a["id"], 0)
    check("move committed", play(m["id"], "e2e4"), True)
    check("offer gone", db.get_match(m["id"])["draw_offer_by"], None)
    check("stale offer can't be accepted", db.accept_draw_if_offered(m["id"], b["id"]), False)
    check("match still live", db.get_match(m["id"])["status"], "live")

    print("!! one offer per position - declining isn't a reset button")
    m, a, b = new_live_match()
    check("offered at ply 0", db.offer_draw(m["id"], a["id"], 0), True)
    check("declined", db.clear_draw_offer(m["id"]), True)
    check("no offer on the board", db.get_match(m["id"])["draw_offer_by"], None)
    check("can't re-offer in the same position", db.offer_draw(m["id"], a["id"], 0), False)
    check("nor can the opponent", db.offer_draw(m["id"], b["id"], 0), False)
    play(m["id"], "e2e4")
    check("a move opens it up again", db.offer_draw(m["id"], b["id"], 1), True)

    print("only one offer at a time")
    check("opponent can't counter-offer over it", db.offer_draw(m["id"], a["id"], 1), False)
    check("nor can the offerer repeat it", db.offer_draw(m["id"], b["id"], 1), False)

    print("nothing to decline when nothing was offered")
    m, a, b = new_live_match()
    check("clear is a no-op", db.clear_draw_offer(m["id"]), False)
    check("accept finds nothing", db.accept_draw_if_offered(m["id"], b["id"]), False)

    print("a match that isn't live can't be drawn")
    m, a, b = new_live_match()
    db.offer_draw(m["id"], a["id"], 0)
    db.settle_match_if_live(m["id"], result="resign", winner_account_id=a["id"])
    check("accept refused after settlement", db.accept_draw_if_offered(m["id"], b["id"]), False)
    check("winner untouched", db.get_match(m["id"])["winner_account_id"], a["id"])
    check("offering on a dead match refused", db.offer_draw(m["id"], a["id"], 1), False)

    # ── the HTTP surface ────────────────────────────────────────────────
    print("the endpoints take their player from the session")
    m, a, b = new_live_match()
    stranger = db.get_or_create_account(f"kaspatest:pX{time.time_ns()}", "xx")
    check("a stranger can't offer", await status_of(main.draw_offer, m["id"], stranger), 403)
    check("a stranger can't accept", await status_of(main.draw_accept, m["id"], stranger), 403)
    check("a stranger can't decline", await status_of(main.draw_decline, m["id"], stranger), 403)

    print("offer -> accept over the routes, with the DMs that go with it")
    _dms.clear()
    check("offer ok", await status_of(main.draw_offer, m["id"], a), 200)
    check("only the opponent is pinged", _dms, [("offer", b["id"])])
    check("offerer can't accept it", await status_of(main.draw_accept, m["id"], a), 400)
    _dms.clear()
    check("opponent accepts", await status_of(main.draw_accept, m["id"], b), 200)
    check("both players told", sorted(d[1] for d in _dms), sorted([a["id"], b["id"]]))
    check("settled as a draw", db.get_match(m["id"])["result"], "draw_agreed")

    print("a second accept over the route doesn't DM again")
    _dms.clear()
    check("match no longer live", await status_of(main.draw_accept, m["id"], b), 400)
    check("silent", _dms, [])

    print("declining over the route leaves the game running")
    m, a, b = new_live_match()
    await main.draw_offer(m["id"], a)
    out = main.draw_decline(m["id"], b)
    check("no offer left", out["drawOffer"], None)
    check("still live", out["status"], "live")

    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        return 1
    print("all draw-offer checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_()))
