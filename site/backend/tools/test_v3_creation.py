"""Tests for v3 escrow CREATION dispatch (roadmap #3a) — run: python tools/test_v3_creation.py

Drives the real HTTP handlers (main.new_challenge / accept_challenge) with real accounts + DB; the
sidecar builders are stubbed to record what they were called with. Proves the branch logic in
_create_match_from_pair: with ESCROW_V3 on AND both players' session keys present -> build_escrow_v3
(side A + B) with the right sess pks + window, stored version 'v3' with the keys pinned; a missing
session key or the flag off falls through to v2/v1; a malformed sessPk is refused; free games never
touch any of it. The covenant + settle path are proven elsewhere (S8-S12, test_settlement_v3).
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DAGMATE_SITE_DB"] = os.path.join(tempfile.mkdtemp(prefix="dagmate-v3c-"), "t.db")

import config  # noqa: E402
config.DB_PATH = os.environ["DAGMATE_SITE_DB"]
assert config.DB_PATH.startswith(tempfile.gettempdir()), f"DB not isolated: {config.DB_PATH}"

import bot_client  # noqa: E402
import database as db  # noqa: E402
import daa_clock  # noqa: E402


async def _noop(*a, **k):
    return None
for _n in dir(bot_client):
    if _n.startswith("notify_"):
        setattr(bot_client, _n, _noop)

import service_client  # noqa: E402
import main  # noqa: E402
from main import NewChallengeBody, AcceptChallengeBody  # noqa: E402
from fastapi import HTTPException  # noqa: E402

_failures: list[str] = []
_calls: dict = {}


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        _failures.append(name)


def stub_builders():
    _calls.clear()
    _calls.update({"v3": [], "v2": [], "v1": [], "daa": 0})

    async def _daa():
        _calls["daa"] += 1
        return 500_000_000

    async def _v3(*, match_id, pk_a, pk_b, side, reclaim_daa, sess_pk_a, sess_pk_b, w_daa):
        _calls["v3"].append(dict(match_id=match_id, side=side, sess_pk_a=sess_pk_a, sess_pk_b=sess_pk_b,
                                 w_daa=w_daa, reclaim_daa=reclaim_daa))
        return {"address": f"kaspa:v3{side}", "redeemHex": f"redeem{side}", "checkpointTag": "cc" * 32}

    async def _v2(*, match_id, pk_a, pk_b, side, reclaim_daa):
        _calls["v2"].append(dict(side=side))
        return {"address": f"kaspa:v2{side}", "redeemHex": f"r{side}"}

    async def _v1(*, match_id, pk_a, pk_b, depositor_is_a, reclaim_daa):
        _calls["v1"].append(dict(depositor_is_a=depositor_is_a))
        return {"address": f"kaspa:v1{depositor_is_a}", "redeemHex": "r"}

    service_client.daa_score = _daa
    service_client.build_escrow_v3 = _v3
    service_client.build_escrow_v2 = _v2
    service_client.build_escrow = _v1


A_SESS = "a1" * 32   # 64 hex
B_SESS = "b2" * 32


def acct(tag):
    return db.get_or_create_account(f"kaspa:{tag}{time.time_ns()}", f"pub{tag}")


async def status_of(coro):
    try:
        await coro
        return 200
    except HTTPException as e:
        return e.status_code


async def _mk(fn, *a, **k):
    return fn(*a, **k)


async def main_() -> int:
    db.ensure_schema()
    STAKE_KAS = 10

    print("ESCROW_V3 on + both session keys -> v3 escrows built with the right args")
    config.ESCROW_V3_ENABLED = True
    config.ESCROW_V2_ENABLED = True
    stub_builders()
    a, b = acct("A"), acct("B")
    ch = main.new_challenge(NewChallengeBody(stakeKas=STAKE_KAS, mode="rapid", sessPk=A_SESS), account=a)
    check("creator session key stored on challenge", db.get_challenge(ch["id"])["sess_pk"], A_SESS)
    m = await main.accept_challenge(ch["id"], AcceptChallengeBody(sessPk=B_SESS), accepter=b)
    row = db.get_match(m["id"])
    check("built two v3 escrows", len(_calls["v3"]), 2)
    check("no v2 build", len(_calls["v2"]), 0)
    check("sides A and B", sorted(c["side"] for c in _calls["v3"]), ["A", "B"])
    check("both session keys passed through", (_calls["v3"][0]["sess_pk_a"], _calls["v3"][0]["sess_pk_b"]), (A_SESS, B_SESS))
    check("window is challenge_window_daa()", _calls["v3"][0]["w_daa"], daa_clock.challenge_window_daa())
    check("stored version v3", row["escrow_version"], "v3")
    check("session keys pinned on match", (row["sess_pk_a"], row["sess_pk_b"]), (A_SESS, B_SESS))
    check("w_daa pinned on match", row["w_daa"], daa_clock.challenge_window_daa())
    check("escrow addresses stored", (row["escrow_a_address"], row["escrow_b_address"]), ("kaspa:v3A", "kaspa:v3B"))

    print("ESCROW_V3 on but accepter sends NO session key -> falls through to v2")
    stub_builders()
    a, b = acct("C"), acct("D")
    ch = main.new_challenge(NewChallengeBody(stakeKas=STAKE_KAS, mode="rapid", sessPk=A_SESS), account=a)
    m = await main.accept_challenge(ch["id"], AcceptChallengeBody(sessPk=None), accepter=b)
    check("no v3 build (a key was missing)", len(_calls["v3"]), 0)
    check("fell through to v2", len(_calls["v2"]), 2)
    check("stored version v2", db.get_match(m["id"])["escrow_version"], "v2")

    print("ESCROW_V3 on, creator sends no session key -> v2")
    stub_builders()
    a, b = acct("E"), acct("F")
    ch = main.new_challenge(NewChallengeBody(stakeKas=STAKE_KAS, mode="rapid"), account=a)  # no sessPk
    m = await main.accept_challenge(ch["id"], AcceptChallengeBody(sessPk=B_SESS), accepter=b)
    check("no v3 without the creator's key", len(_calls["v3"]), 0)
    check("v2 instead", db.get_match(m["id"])["escrow_version"], "v2")

    print("ESCROW_V3 OFF, both keys present -> v2 (flag gates it)")
    config.ESCROW_V3_ENABLED = False
    stub_builders()
    a, b = acct("G"), acct("H")
    ch = main.new_challenge(NewChallengeBody(stakeKas=STAKE_KAS, mode="rapid", sessPk=A_SESS), account=a)
    m = await main.accept_challenge(ch["id"], AcceptChallengeBody(sessPk=B_SESS), accepter=b)
    check("flag off -> no v3", len(_calls["v3"]), 0)
    check("v2 built", db.get_match(m["id"])["escrow_version"], "v2")
    config.ESCROW_V3_ENABLED = True

    print("both covenant flags off -> v1, still ignores session keys")
    config.ESCROW_V3_ENABLED = False
    config.ESCROW_V2_ENABLED = False
    stub_builders()
    a, b = acct("I"), acct("J")
    ch = main.new_challenge(NewChallengeBody(stakeKas=STAKE_KAS, mode="rapid", sessPk=A_SESS), account=a)
    m = await main.accept_challenge(ch["id"], AcceptChallengeBody(sessPk=B_SESS), accepter=b)
    check("v1 built", db.get_match(m["id"])["escrow_version"], "v1")
    check("2 v1 escrows (depositor A + B)", len(_calls["v1"]), 2)
    config.ESCROW_V3_ENABLED = True
    config.ESCROW_V2_ENABLED = True

    print("a malformed sessPk is refused at the door (not stored as junk)")
    a = acct("K")
    bad = await status_of(_mk(main.new_challenge, NewChallengeBody(stakeKas=STAKE_KAS, mode="rapid", sessPk="xyz"), account=a))
    check("bad sessPk -> 400", bad, 400)
    short = await status_of(_mk(main.new_challenge, NewChallengeBody(stakeKas=STAKE_KAS, mode="rapid", sessPk="ab"), account=a))
    check("short sessPk -> 400", short, 400)

    print("a 0x-prefixed / uppercase sessPk is normalised, not rejected")
    stub_builders()
    a, b = acct("L"), acct("M")
    ch = main.new_challenge(NewChallengeBody(stakeKas=STAKE_KAS, mode="rapid", sessPk="0x" + ("AB" * 32)), account=a)
    check("normalised to bare lowercase hex", db.get_challenge(ch["id"])["sess_pk"], "ab" * 32)

    print("a FREE game ignores session keys entirely (no escrow, no sidecar)")
    stub_builders()
    a, b = acct("N"), acct("O")
    ch = main.new_challenge(NewChallengeBody(stakeKas=0, mode="rapid", sessPk=A_SESS), account=a)
    m = await main.accept_challenge(ch["id"], AcceptChallengeBody(sessPk=B_SESS), accepter=b)
    check("free match is live", db.get_match(m["id"])["status"], "live")
    check("free match built no escrow of any version", (len(_calls["v3"]), len(_calls["v2"]), len(_calls["v1"])), (0, 0, 0))
    check("free match has no escrow_version", db.get_match(m["id"])["escrow_version"], None)

    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        return 1
    print("all v3 creation checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_()))
