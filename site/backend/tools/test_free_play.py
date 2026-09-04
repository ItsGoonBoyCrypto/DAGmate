"""Tests for FREE PLAY (no-wager PvP) — run: python tools/test_free_play.py

A 0-stake challenge is a free game: no escrow, no deposit, no settlement. It must go LIVE the
instant it's accepted (skipping the money path AND the Kaspa sidecar), play like any other game,
and end with nothing to settle. These tests drive the real HTTP handlers (main.new_challenge /
accept_challenge) with real accounts + DB; the sidecar is never needed for a free match, so it
isn't even stubbed — if a free game tried to touch it, this would blow up, which is the point.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DAGMATE_SITE_DB"] = os.path.join(tempfile.mkdtemp(prefix="dagmate-free-"), "t.db")

import bot_client  # noqa: E402
import chess_logic  # noqa: E402
import config  # noqa: E402
import database as db  # noqa: E402
import settlement  # noqa: E402

_failures: list[str] = []


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        _failures.append(name)


async def _noop(*a, **k):  # swallow every outbound DM
    return None


for _n in dir(bot_client):
    if _n.startswith("notify_"):
        setattr(bot_client, _n, _noop)

import main  # noqa: E402  (after DB env + DM stubs)
from main import NewChallengeBody  # noqa: E402
from fastapi import HTTPException  # noqa: E402


def acct(tag):
    return db.get_or_create_account(f"kaspa:{tag}{time.time_ns()}", f"pub{tag}")


async def status_of(coro):
    try:
        await coro
        return 200
    except HTTPException as e:
        return e.status_code


async def main_() -> int:
    db.ensure_schema()

    print("a 0-stake challenge creates a FREE, escrow-less match that is live immediately")
    a, b = acct("A"), acct("B")
    ch = main.new_challenge(NewChallengeBody(stakeKas=0, mode="rapid"), account=a)
    check("challenge marked free", ch["isFree"], True)
    check("challenge stake is 0", ch["stakeKas"], 0.0)
    m = await main.accept_challenge(ch["id"], accepter=b)
    mid = m["id"]
    row = db.get_match(mid)
    check("match is LIVE on accept (no deposit phase)", row["status"], "live")
    check("no escrow A", row["escrow_a_address"], None)
    check("no escrow B", row["escrow_b_address"], None)
    check("stake is 0", row["stake_sompi"], 0)
    check("public match flagged free", main._match_public(row)["isFree"], True)
    check("clock is running (white to move)", row["turn"], "white")

    print("a free game plays and ends with NOTHING to settle")
    # end it decisively (the game engine / clock path is exercised by the other suites; here we
    # only care that a settled FREE match has no money path). A wins.
    db.settle_match_if_live(mid, result="resign", winner_account_id=a["id"])
    check("settled", db.get_match(mid)["status"], "settled")
    check("A recorded as winner", db.get_match(mid)["winner_account_id"], a["id"])

    print("settlement of a free game returns a 'free' result, not a payout")
    pa = await settlement.prepare(mid, a["address"])
    check("state is free", pa["state"], "free")
    check("winner told they won", pa["youWon"], True)
    check("no pot", pa["payoutSompi"], "0")
    check("nothing to sign", pa["mySignatureInputs"], [])
    pb = await settlement.prepare(mid, b["address"])
    check("loser sees free too", pb["state"], "free")
    check("loser didn't win", pb["youWon"], False)
    # a stray submit is a harmless no-op that still reports the free result
    r = await settlement.submit(mid, a["address"], "IGNORED")
    check("submit on a free game is a no-op free result", r["state"], "free")

    print("a free DRAW settles to nothing for both, honours even")
    a2, b2 = acct("C"), acct("D")
    ch2 = main.new_challenge(NewChallengeBody(stakeKas=0, mode="rapid"), account=a2)
    m2 = await main.accept_challenge(ch2["id"], accepter=b2)
    mid2 = m2["id"]
    db.settle_match_if_live(mid2, result="draw_agreed", winner_account_id=None)
    pa2 = await settlement.prepare(mid2, a2["address"])
    check("free draw state", pa2["state"], "free")
    check("free draw flagged", pa2["isDraw"], True)
    check("free draw pays nothing", pa2["payoutSompi"], "0")

    print("a staked challenge still enforces the minimum (free is only 0)")
    a3 = acct("E")
    lo = await status_of(_coro(main.new_challenge, NewChallengeBody(stakeKas=0.5, mode="rapid"), account=a3))
    check("0.5 KAS rejected (below min, not free)", lo, 400)

    print("free play can be switched off")
    config.FREE_PLAY_ENABLED = False
    try:
        off = await status_of(_coro(main.new_challenge, NewChallengeBody(stakeKas=0, mode="rapid"), account=a3))
        check("0-stake rejected when free play is disabled", off, 400)
    finally:
        config.FREE_PLAY_ENABLED = True

    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        return 1
    print("all free-play checks passed")
    return 0


async def _coro(fn, *a, **k):
    # new_challenge is a plain def; wrap so status_of can await it uniformly.
    return fn(*a, **k)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_()))
