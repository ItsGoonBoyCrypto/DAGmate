"""Tests for the v3 move channel RELAY (roadmap #3a, Phase 2a) — run: python tools/test_move_channel.py

Drives real handlers (make_move / sign_checkpoint) on a real live v3 match; the sidecar's escrow
build + DAA read are stubbed. Proves the relay ORCHESTRATION: each move pins a server-computed
checkpoint for the new position (claimant = the mover, deadline from the authoritative clock + DAA),
both players' signatures promote it to the latest co-signed checkpoint, and the last co-signed state
survives a fresh (unsigned) pending. The signatures themselves are validated on-chain by the covenant
(S8-S12); the trustless forfeit DRIVER that consumes these is Phase 2b.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DAGMATE_SITE_DB"] = os.path.join(tempfile.mkdtemp(prefix="dagmate-mc-"), "t.db")

import config  # noqa: E402
config.DB_PATH = os.environ["DAGMATE_SITE_DB"]
assert config.DB_PATH.startswith(tempfile.gettempdir()), f"DB not isolated: {config.DB_PATH}"
config.ESCROW_V3_ENABLED = True
config.ANCHOR_MOVES = False

import bot_client  # noqa: E402
import database as db  # noqa: E402
import clocks  # noqa: E402


async def _noop(*a, **k):
    return None
for _n in dir(bot_client):
    if _n.startswith("notify_"):
        setattr(bot_client, _n, _noop)

import service_client  # noqa: E402
import main  # noqa: E402
from main import NewChallengeBody, AcceptChallengeBody, MoveBody, CheckpointSigBody  # noqa: E402
from fastapi import HTTPException  # noqa: E402

_failures: list[str] = []
FAKE_DAA = 500_000_000


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        _failures.append(name)


def stub_sidecar():
    async def _daa():
        return FAKE_DAA

    async def _v3(*, match_id, pk_a, pk_b, side, reclaim_daa, sess_pk_a, sess_pk_b, w_daa):
        return {"address": f"kaspa:v3{side}{match_id}", "redeemHex": f"r{side}", "checkpointTag": "cc" * 32}
    service_client.daa_score = _daa
    service_client.build_escrow_v3 = _v3


def acct(tag):
    return db.get_or_create_account(f"kaspa:{tag}{time.time_ns()}", f"pub{tag}")


async def status_of(coro):
    try:
        await coro
        return 200
    except HTTPException as e:
        return e.status_code


async def make_live_v3():
    """Create+accept a staked v3 match with session keys, then force it live with clocks running."""
    a, b = acct("A"), acct("B")
    ch = main.new_challenge(NewChallengeBody(stakeKas=10, mode="rapid", sessPk="a1" * 32), account=a)
    m = await main.accept_challenge(ch["id"], AcceptChallengeBody(sessPk="b2" * 32), accepter=b)
    initial, inc = clocks.settings_for("rapid")
    db.mark_match_live(m["id"], initial_ms=initial, increment_ms=inc, now_ms=clocks.now_ms())
    return m["id"], a, b


async def main_() -> int:
    db.ensure_schema()
    stub_sidecar()

    print("a v3 match is created with a checkpoint tag and no checkpoint until the first move")
    mid, a, b = await make_live_v3()
    row = db.get_match(mid)
    check("checkpoint tag stored", row["checkpoint_tag"], "cc" * 32)
    check("no pending checkpoint before any move", row["pending_cp_json"], None)
    check("public checkpoint is None pre-move", main._checkpoint_public(row), None)

    print("white's move pins a checkpoint: claimant A, ply 1, a real deadline")
    out = await main.make_move(mid, MoveBody(uci="e2e4"), a=a)
    cp = out["checkpoint"]
    check("checkpoint surfaced after the move", bool(cp), True)
    check("claimant is the mover (A)", cp["claimant"], "A")
    check("ply is 1", cp["ply"], 1)
    check("tag surfaced", cp["tag"], "cc" * 32)
    check("deadline is now_daa + clock(daa) + margin (> now)", cp["deadlineDaa"] > FAKE_DAA, True)
    check("not co-signed yet", cp["cosigned"], False)

    print("both players co-sign -> promoted to the latest co-signed checkpoint")
    r1 = await main.sign_checkpoint(mid, CheckpointSigBody(sig="ab" * 64), a=a)
    check("A signed, not yet cosigned", (r1["haveA"], r1["cosigned"]), (True, False))
    r2 = await main.sign_checkpoint(mid, CheckpointSigBody(sig="cd" * 64), a=b)
    check("B signed -> cosigned", r2["cosigned"], True)
    row = db.get_match(mid)
    cos = json.loads(row["cosigned_cp_json"])
    check("cosigned stored with both sigs", (cos["sigA"], cos["sigB"]), ("ab" * 64, "cd" * 64))
    check("cosigned claimant A", cos["claimant"], "A")

    print("black's reply pins a NEW checkpoint (claimant B) but the last co-signed one survives")
    out = await main.make_move(mid, MoveBody(uci="e7e5"), a=b)
    cp2 = out["checkpoint"]
    check("new checkpoint claimant B", cp2["claimant"], "B")
    check("new checkpoint ply 2", cp2["ply"], 2)
    check("new checkpoint not cosigned", cp2["cosigned"], False)
    row = db.get_match(mid)
    check("last co-signed checkpoint (A, ply1) still intact", json.loads(row["cosigned_cp_json"])["ply"], 1)

    print("a malformed signature is refused")
    bad = await status_of(main.sign_checkpoint(mid, CheckpointSigBody(sig="nothex"), a=a))
    check("bad sig -> 400", bad, 400)
    short = await status_of(main.sign_checkpoint(mid, CheckpointSigBody(sig="ab" * 10), a=a))
    check("short sig -> 400", short, 400)

    print("a non-player can't sign the channel")
    outsider = acct("X")
    check("stranger -> 403", await status_of(main.sign_checkpoint(mid, CheckpointSigBody(sig="ab" * 64), a=outsider)), 403)

    print("a v2 match has no move channel")
    config.ESCROW_V3_ENABLED = False
    a2, b2 = acct("C"), acct("D")
    ch2 = main.new_challenge(NewChallengeBody(stakeKas=10, mode="rapid", sessPk="a1" * 32), account=a2)
    # stub v2 build so accept works
    async def _v2(*, match_id, pk_a, pk_b, side, reclaim_daa):
        return {"address": f"kaspa:v2{side}", "redeemHex": "r"}
    service_client.build_escrow_v2 = _v2
    config.ESCROW_V2_ENABLED = True
    m2 = await main.accept_challenge(ch2["id"], AcceptChallengeBody(sessPk="b2" * 32), accepter=b2)
    db.mark_match_live(m2["id"], initial_ms=600000, increment_ms=5000, now_ms=clocks.now_ms())
    out2 = await main.make_move(m2["id"], MoveBody(uci="e2e4"), a=a2)
    check("v2 move has no checkpoint", out2["checkpoint"], None)
    check("v2 sign refused", await status_of(main.sign_checkpoint(m2["id"], CheckpointSigBody(sig="ab" * 64), a=a2)), 400)
    config.ESCROW_V3_ENABLED = True

    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        return 1
    print("all move-channel relay checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_()))
