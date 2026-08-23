"""Tests for challenge creation + the accept/decline/tournament-start guards —
run: python tools/test_challenges.py

Same shape as the other suites: dependency-free, real schema, real accessors,
throwaway DB. Nothing chain-facing is touched here — these are the guards that
sit BEFORE an escrow is ever built, so they can be proven with the DB alone.

Why these earn tests: every property below, if it regressed, would either mint
money out of thin air or spend one stake twice.

  1. A named challenge to an address that has never played must NOT silently
     become an open challenge to the whole board (M5). The creator picked an
     opponent; going public is a surprise the money can't take back.
  2. Stake bounds + a finite-number check (H3/M6). NaN survives JSON and beats a
     `<= 0` test, so a garbage stake could otherwise mint an escrow.
  3. Accepting is a single atomic open->accepting transition (M1). A
     double-submit or two racing acceptors must yield exactly one match, never
     two escrows for one stake. A failed build hands the challenge back to open.
  4. Declining is guarded the same way (won't fire once acceptance has claimed
     the challenge), and starting a tournament is a one-winner open->running
     transition (M2).
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DAGMATE_SITE_DB"] = os.path.join(tempfile.mkdtemp(prefix="dagmate-chal-"), "t.db")

import config  # noqa: E402
import database as db  # noqa: E402

_failures: list[str] = []


def check(name: str, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        _failures.append(name)


import main  # noqa: E402  (after DB env is in place)
from fastapi import HTTPException  # noqa: E402


def acct(tag: str) -> dict:
    a = db.get_or_create_account(f"kaspatest:{tag}{time.time_ns()}", tag)
    db.set_pubkey(a["id"], "02" + "ab" * 32) if hasattr(db, "set_pubkey") else None
    return db.get_account(a["id"])


def status_of(fn, *args) -> int:
    try:
        fn(*args)
        return 200
    except HTTPException as e:
        return e.status_code


def main_() -> int:
    db.ensure_schema()

    print("a named challenge to an unknown address is refused, not made public")
    creator = acct("cr")
    Body = main.NewChallengeBody
    code = status_of(main.new_challenge,
                     Body(toAddress="kaspatest:nobodyhere", stakeKas=10, mode="rapid"), creator)
    check("unknown opponent bounces", code, 400)

    print("stake bounds + finite check")
    check("below minimum refused",
          status_of(main.new_challenge, Body(stakeKas=0.0001, mode="rapid"), creator), 400)
    check("above maximum refused",
          status_of(main.new_challenge,
                    Body(stakeKas=config.MAX_STAKE_SOMPI / config.SOMPI_PER_KAS + 1, mode="rapid"), creator), 400)
    check("NaN stake refused",
          status_of(main.new_challenge, Body(stakeKas=float("nan"), mode="rapid"), creator), 400)
    check("inf stake refused",
          status_of(main.new_challenge, Body(stakeKas=float("inf"), mode="rapid"), creator), 400)
    check("a sane stake is accepted",
          status_of(main.new_challenge, Body(stakeKas=10, mode="rapid"), creator), 200)
    check("gas-only ignores the bounds",
          status_of(main.new_challenge, Body(gasOnly=True, mode="rapid"), creator), 200)

    print("accept is a one-winner open->accepting transition")
    ch = db.create_challenge(creator["id"], None, 10 * config.SOMPI_PER_KAS, "rapid", False)
    check("first claim wins", db.claim_challenge_for_accept(ch["id"]), True)
    check("second claim loses", db.claim_challenge_for_accept(ch["id"]), False)
    check("status is the transient accepting", db.get_challenge(ch["id"])["status"], "accepting")

    print("a failed build releases the challenge back to open")
    check("released", db.release_challenge_to_open(ch["id"]) or db.get_challenge(ch["id"])["status"], "open")
    check("and can be claimed again", db.claim_challenge_for_accept(ch["id"]), True)

    print("decline is guarded against racing an acceptance")
    ch2 = db.create_challenge(creator["id"], None, 10 * config.SOMPI_PER_KAS, "rapid", False)
    db.claim_challenge_for_accept(ch2["id"])  # someone is mid-accept
    check("decline can't fire on an accepting challenge", db.decline_challenge_if_open(ch2["id"]), False)
    ch3 = db.create_challenge(creator["id"], None, 10 * config.SOMPI_PER_KAS, "rapid", False)
    check("decline fires on an open one", db.decline_challenge_if_open(ch3["id"]), True)
    check("and only once", db.decline_challenge_if_open(ch3["id"]), False)

    print("tournament start is a one-winner open->running transition")
    t = db.get_or_create_open_tournament(config.TOURNAMENT_TIERS_KAS[0])
    check("first start wins", db.claim_tournament_start(t["id"]), True)
    check("second start loses", db.claim_tournament_start(t["id"]), False)
    check("status is running", db.get_tournament(t["id"])["status"], "running")

    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        return 1
    print("all challenge-guard checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_())
