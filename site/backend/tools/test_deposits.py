"""Tests for the deposit watcher (deposits.py) — run: python tools/test_deposits.py

Deliberately dependency-free (no pytest in this project yet) and deliberately
NOT mock-heavy: it drives the real SQLite schema, the real database.py
accessors and the real deposits.poll_once() against a throwaway DB. The only
thing stubbed is the chain itself, because that's the one part we can't
conjure locally.

Worth testing above everything else in this backend: `awaiting_deposit -> live`
is what makes a stake settleable to a winner, so every bug here is a money bug.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DAGMATE_SITE_DB"] = os.path.join(tempfile.mkdtemp(prefix="dagmate-test-"), "t.db")

import config  # noqa: E402
import database as db  # noqa: E402
import deposits  # noqa: E402

STAKE = 10 * config.SOMPI_PER_KAS
ADDR_A, ADDR_B = "kaspatest:escrowA", "kaspatest:escrowB"

_failures: list[str] = []


def check(name: str, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        _failures.append(name)


def chain(a_confirmed=0, b_confirmed=0, a_pending=0, b_pending=0, fail=False):
    """Stub the sidecar. `pending` is value that exists on chain but isn't deep
    enough yet — it must never count towards starting a match."""
    async def _stub(addresses, confirm_daa):
        if fail:
            from service_client import ServiceError
            raise ServiceError("node unreachable")
        return {
            ADDR_A: {"sompi": a_confirmed + a_pending, "confirmedSompi": a_confirmed, "utxos": 1},
            ADDR_B: {"sompi": b_confirmed + b_pending, "confirmedSompi": b_confirmed, "utxos": 1},
        }
    deposits.escrow_balances = _stub


def new_match(age_secs=0) -> str:
    # poll_once() sweeps EVERY awaiting match, so leftovers from an earlier
    # case would also go live and make the "how many started" counts
    # meaningless. Each case gets a clean matches table.
    with db._lock, db._conn() as c:
        c.execute("DELETE FROM matches")
    acct_a = db.get_or_create_account(f"kaspatest:pA{time.time_ns()}", "aa")
    acct_b = db.get_or_create_account(f"kaspatest:pB{time.time_ns()}", "bb")
    m = db.create_match(challenge_id=None, tournament_id=None, round_no=None,
                        player_a_account_id=acct_a["id"], player_b_account_id=acct_b["id"],
                        stake_sompi=STAKE, mode="rapid", fen="startpos",
                        escrow_a={"address": ADDR_A, "redeemHex": "00"},
                        escrow_b={"address": ADDR_B, "redeemHex": "00"}, reclaim_daa=1)
    if age_secs:
        with db._lock, db._conn() as c:
            c.execute("UPDATE matches SET created_ts=? WHERE id=?",
                      (int(time.time()) - age_secs, m["id"]))
    return m["id"]


def status(mid: str) -> str:
    return db.get_match(mid)["status"]


async def main() -> int:
    db.ensure_schema()

    print("neither side funded")
    mid = new_match()
    chain(0, 0)
    check("no match starts", await deposits.poll_once(), 0)
    check("stays awaiting", status(mid), "awaiting_deposit")

    print("only one side funded")
    mid = new_match()
    chain(STAKE, 0)
    check("no match starts", await deposits.poll_once(), 0)
    check("stays awaiting", status(mid), "awaiting_deposit")
    check("A recorded as funded", db.get_match(mid)["funded_a_ts"] is not None, True)
    check("B not funded", db.get_match(mid)["funded_b_ts"], None)

    print("underfunded by one sompi")
    mid = new_match()
    chain(STAKE, STAKE - 1)
    check("no match starts", await deposits.poll_once(), 0)
    check("B not funded", db.get_match(mid)["funded_b_ts"], None)
    check("partial amount is recorded", db.get_match(mid)["funded_b_sompi"], STAKE - 1)

    print("value on chain but not yet confirmed")
    mid = new_match()
    chain(a_confirmed=STAKE, b_confirmed=0, b_pending=STAKE * 5)
    check("unconfirmed value can't start a match", await deposits.poll_once(), 0)
    check("stays awaiting", status(mid), "awaiting_deposit")

    print("both sides funded")
    mid = new_match()
    chain(STAKE, STAKE)
    check("match starts", await deposits.poll_once(), 1)
    check("now live", status(mid), "live")

    print("re-poll after going live")
    check("not started twice", await deposits.poll_once(), 0)
    check("still live", status(mid), "live")

    print("overfunding")
    mid = new_match()
    chain(STAKE * 3, STAKE)
    check("match starts", await deposits.poll_once(), 1)
    check("now live", status(mid), "live")

    print("a funded side that momentarily reads low stays funded")
    mid = new_match()
    chain(STAKE, 0)
    await deposits.poll_once()
    chain(0, STAKE)  # node under-reports A on the next poll
    check("match still starts", await deposits.poll_once(), 1)
    check("now live", status(mid), "live")

    print("sidecar unreachable")
    mid = new_match(age_secs=config.DEPOSIT_DEADLINE_SECS + 60)
    chain(fail=True)
    check("no match starts", await deposits.poll_once(), 0)
    check("an old match is NOT expired while we're blind", status(mid), "awaiting_deposit")

    print("funding deadline passes")
    chain(STAKE, 0)  # A paid, B never did
    check("no match starts", await deposits.poll_once(), 0)
    check("expired", status(mid), "expired")
    check("reason recorded", db.get_match(mid)["result"], "deposit_timeout")
    check("A's deposit still on record", db.get_match(mid)["funded_a_ts"] is not None, True)

    print("an expired match is never revisited")
    chain(STAKE, STAKE)  # B pays late — too late
    check("does not start", await deposits.poll_once(), 0)
    check("still expired", status(mid), "expired")

    print("a match with no escrow addresses is skipped")
    acct = db.get_or_create_account("kaspatest:lonely", "cc")
    m = db.create_match(challenge_id=None, tournament_id=None, round_no=None,
                        player_a_account_id=acct["id"], player_b_account_id=acct["id"],
                        stake_sompi=STAKE, mode="rapid", fen="startpos",
                        escrow_a=None, escrow_b=None, reclaim_daa=1)
    chain(STAKE, STAKE)
    await deposits.poll_once()
    check("stays awaiting", status(m["id"]), "awaiting_deposit")

    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        return 1
    print("all deposit-watcher checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
