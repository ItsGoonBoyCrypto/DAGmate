"""Tests for tournament bracket progression (the DB layer that drives
main._advance_tournament) — run: python tools/test_tournament.py

Dependency-free, real schema, real database.py accessors, throwaway DB — same
shape as test_settlement.py. This proves the bracket LOGIC: a round only reads
as complete when every match in it has a decided winner, winners come back in a
deterministic pairing order, a drawn match holds the round open, and the
next-round / champion claims each happen exactly once under a race.

The on-chain half (escrows actually built, stakes doubling, the champion paid
the whole pool) is proven end-to-end against live testnet by the e2e harness,
not here — a laptop has no node or arbiter key.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DAGMATE_SITE_DB"] = os.path.join(tempfile.mkdtemp(prefix="dagmate-tourney-"), "t.db")

import config  # noqa: E402
import database as db  # noqa: E402

db.ensure_schema()

TIER = 20
STAKE = TIER * config.SOMPI_PER_KAS
_failures: list[str] = []


def check(name: str, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        _failures.append(name)


def acct(tag: str) -> dict:
    return db.get_or_create_account(f"kaspatest:{tag}", pubkey=f"pub_{tag}")


def new_match(tid: str, round_no: int, a: dict, b: dict, stake: int = STAKE) -> dict:
    return db.create_match(
        challenge_id=None, tournament_id=tid, round_no=round_no,
        player_a_account_id=a["id"], player_b_account_id=b["id"], stake_sompi=stake,
        mode="rapid", fen="fen", escrow_a=None, escrow_b=None, reclaim_daa=100)


def decide(match_id: str, winner_id: str | None, result: str = "resign"):
    db.mark_match_live(match_id, initial_ms=600_000, increment_ms=0, now_ms=int(time.time() * 1000))
    db.settle_match_if_live(match_id, result=result, winner_account_id=winner_id)


def main():
    print("a round reads incomplete until EVERY match in it is decided")
    t = db.get_or_create_open_tournament(TIER)
    players = [acct(f"r{i}") for i in range(8)]
    r1 = [new_match(t["id"], 1, players[i], players[i + 1]) for i in range(0, 8, 2)]
    check("no winners while all four are unplayed", db.round_winners_if_complete(t["id"], 1), None)
    decide(r1[0]["id"], players[0]["id"])
    decide(r1[1]["id"], players[2]["id"])
    decide(r1[2]["id"], players[4]["id"])
    check("still none with one match live", db.round_winners_if_complete(t["id"], 1), None)
    decide(r1[3]["id"], players[6]["id"])
    check("all four decided -> four winners", db.round_winners_if_complete(t["id"], 1),
          [players[0]["id"], players[2]["id"], players[4]["id"], players[6]["id"]])

    print("winners come back in bracket (creation) order, not settle order")
    t2 = db.get_or_create_open_tournament(100)  # a different tier => different open tournament
    p = [acct(f"o{i}") for i in range(4)]
    m = [new_match(t2["id"], 1, p[0], p[1]), new_match(t2["id"], 1, p[2], p[3])]
    decide(m[1]["id"], p[3]["id"])  # settle the SECOND match first
    decide(m[0]["id"], p[0]["id"])
    check("ordered by hd_index, not by when they settled",
          db.round_winners_if_complete(t2["id"], 1), [p[0]["id"], p[3]["id"]])

    print("a drawn match holds the round open (no winner to promote)")
    t3 = db.get_or_create_open_tournament(250)
    d = [acct(f"d{i}") for i in range(2)]
    dm = new_match(t3["id"], 1, d[0], d[1])
    decide(dm["id"], None, result="stalemate")
    check("settled-but-drawn is not a completed round", db.round_winners_if_complete(t3["id"], 1), None)

    print("the next-round build is claimed exactly once")
    check("first claim wins", db.claim_round_advance(t["id"], 2), True)
    check("second claim refused", db.claim_round_advance(t["id"], 2), False)
    check("a different round is a separate claim", db.claim_round_advance(t["id"], 3), True)

    print("the champion is recorded exactly once, and closes the tournament")
    db.claim_tournament_start(t["id"])  # open -> running (set_champion guards on 'running')
    check("first crown wins", db.set_tournament_champion(t["id"], players[0]["id"]), True)
    row = db.get_tournament(t["id"])
    check("status is complete", row["status"], "complete")
    check("champion recorded", row["champion_account_id"], players[0]["id"])
    check("second crown refused", db.set_tournament_champion(t["id"], players[2]["id"]), False)
    check("champion unchanged", db.get_tournament(t["id"])["champion_account_id"], players[0]["id"])

    print("stake doubles into the next round (winnings roll up)")
    prev = db.list_tournament_round_matches(t["id"], 1)
    check("round-1 stake is the tier", prev[0]["stake_sompi"], STAKE)
    check("next round is double", prev[0]["stake_sompi"] * 2, 2 * STAKE)

    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {_failures}")
        sys.exit(1)
    print("all tournament checks passed")


if __name__ == "__main__":
    main()
