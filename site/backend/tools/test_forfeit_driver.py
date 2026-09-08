"""Tests for the v3 trustless-forfeit DRIVER in clocks.py (roadmap #3a, Phase 2b).
Run: python tools/test_forfeit_driver.py

Real schema + accessors + a real flagged clock; the sidecar forfeit routes are stubbed. Proves the
DRIVER logic: when the latest co-signed checkpoint names the winner, a flag claims the pot trustlessly
into the pending covenant (no oracle) and marks the match forfeit-claimed (not yet paid); when it
doesn't (draw, no checkpoint, or the last co-signed state authorises the loser), it falls back to the
oracle settle. And finalise_due_forfeits pays out once the window has passed. The covenant + on-chain
claim/finalise are proven separately (S8-S12, e2e_v3.mjs); these prove the orchestration.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DAGMATE_SITE_DB"] = os.path.join(tempfile.mkdtemp(prefix="dagmate-ff-"), "t.db")

import config  # noqa: E402
config.DB_PATH = os.environ["DAGMATE_SITE_DB"]
assert config.DB_PATH.startswith(tempfile.gettempdir()), f"DB not isolated: {config.DB_PATH}"
config.ESCROW_V3_ENABLED = True

import bot_client  # noqa: E402
import chess_logic  # noqa: E402
import database as db  # noqa: E402
import clocks  # noqa: E402


async def _noop(*a, **k):
    return None
for _n in dir(bot_client):
    if _n.startswith("notify_"):
        setattr(bot_client, _n, _noop)

import service_client  # noqa: E402

_failures: list[str] = []
_calls: dict = {}


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        _failures.append(name)


NOW_DAA = 530_000_000  # the stubbed current chain DAA the driver gates the claim on


def stub_sidecar(*, claim_error=None, finalise_error=None, now_daa=NOW_DAA):
    _calls.clear()
    _calls.update({"claim": [], "finalise": [], "daa": 0})

    async def _daa():
        _calls["daa"] += 1
        return now_daa

    async def _claim(*, escrows, match_id, pk_a, pk_b, sess_pk_a, sess_pk_b, w_daa, deadline_daa, ply, claimant, sig_a, sig_b):
        _calls["claim"].append({"escrows": escrows, "claimant": claimant, "ply": ply, "deadline_daa": deadline_daa})
        if claim_error:
            raise service_client.ServiceError(claim_error)
        return {"txid": "claimtx", "pendingAddress": "kaspa:pending", "pendingRedeem": "deadbeef", "inputs": len(escrows)}

    async def _finalise(*, pending_redeem, pending_address, claimant, pk_a, pk_b, w_daa):
        _calls["finalise"].append({"claimant": claimant, "pending_address": pending_address})
        if finalise_error:
            raise service_client.ServiceError(finalise_error)
        return {"txid": "finaltx", "paid": "kaspa:winner", "inputs": 2}

    service_client.forfeit_claim_v3 = _claim
    service_client.forfeit_finalise_v3 = _finalise
    service_client.daa_score = _daa


def flagged_v3_match(*, cosigned_claimant, stake=10 * 10**8, deadline_daa=NOW_DAA - 100):
    """A live v3 match whose white clock has run out, with a co-signed checkpoint naming
    `cosigned_claimant` ('A'/'B'/None-for-no-checkpoint) and DAA deadline `deadline_daa`
    (default in the PAST so the trustless claim is eligible; pass a future value to test 'wait')."""
    with db._lock, db._conn() as c:
        c.execute("DELETE FROM matches")
    a = db.get_or_create_account(f"kaspa:pA{time.time_ns()}", "pubA")
    b = db.get_or_create_account(f"kaspa:pB{time.time_ns()}", "pubB")
    m = db.create_match(challenge_id=None, tournament_id=None, round_no=None,
                        player_a_account_id=a["id"], player_b_account_id=b["id"],
                        stake_sompi=stake, mode="rapid", fen=chess_logic.STARTING_FEN,
                        escrow_a={"address": "kaspa:eA", "redeemHex": "aa"},
                        escrow_b={"address": "kaspa:eB", "redeemHex": "bb"}, reclaim_daa=1)
    db.set_match_escrows(m["id"], {"address": "kaspa:eA", "redeemHex": "aa", "checkpointTag": "cc" * 32},
                         {"address": "kaspa:eB", "redeemHex": "bb"}, version="v3",
                         sess_pk_a="a" * 64, sess_pk_b="b" * 64, w_daa=72000)
    started = clocks.now_ms() - 60_000  # 60s ago
    cp_json = None
    if cosigned_claimant is not None:
        cp_json = json.dumps({"deadlineDaa": deadline_daa, "ply": 12, "claimant": cosigned_claimant,
                              "sigA": "aa" * 64, "sigB": "bb" * 64})
    with db._lock, db._conn() as c:
        # white to move, white clock exhausted (100ms bank, started 60s ago) -> flagged white
        c.execute("UPDATE matches SET status='live', turn='white', clock_white_ms=100, clock_black_ms=600000, "
                  "clock_turn_started_ms=?, funded_a_sompi=?, funded_b_sompi=?, cosigned_cp_json=? WHERE id=?",
                  (started, stake, stake, cp_json, m["id"]))
    return db.get_match(m["id"]), a, b


async def main() -> int:
    db.ensure_schema()
    # who wins when white flags from the start position (real chess_logic)?
    _res, winner_color = chess_logic.timeout_result(chess_logic.STARTING_FEN, "white")
    winner_side = "A" if winner_color == "white" else "B"  # expect B (black wins when white flags)
    print(f"(white flags -> winner_color={winner_color}, winner_side={winner_side})")

    print("co-signed checkpoint names the winner -> TRUSTLESS forfeit, no oracle")
    stub_sidecar()
    m, a, b = flagged_v3_match(cosigned_claimant=winner_side)
    did = await clocks.forfeit_if_flagged(m, clocks.now_ms())
    check("forfeit_if_flagged handled it", did, True)
    check("forfeit_claim_v3 called once", len(_calls["claim"]), 1)
    check("claimed both escrows", len(_calls["claim"][0]["escrows"]), 2)
    check("claimant is the winner", _calls["claim"][0]["claimant"], winner_side)
    row = db.get_match(m["id"])
    check("match settled", row["status"], "settled")
    check("winner recorded", row["winner_account_id"], (a if winner_side == "A" else b)["id"])
    check("forfeit claim txid stored", row["forfeit_claim_txid"], "claimtx")
    check("NOT yet paid (settle_txid null until finalise)", row["settle_txid"], None)
    check("pending covenant stored", row["forfeit_pending_address"], "kaspa:pending")

    print("finalise_due_forfeits pays out once the window has passed")
    # backdate the claim so the wall-time gate opens
    with db._lock, db._conn() as c:
        c.execute("UPDATE matches SET settled_ts=? WHERE id=?", (int(time.time()) - config.CHALLENGE_WINDOW_SECS - 1, m["id"]))
    n = await clocks.finalise_due_forfeits()
    check("one forfeit finalised", n, 1)
    check("finalise called", len(_calls["finalise"]), 1)
    check("settle_txid now set (paid)", db.get_match(m["id"])["settle_txid"], "finaltx")

    print("before the window, finalise does nothing")
    stub_sidecar()
    m2, a2, b2 = flagged_v3_match(cosigned_claimant=winner_side)
    await clocks.forfeit_if_flagged(m2, clocks.now_ms())  # settled_ts = now
    n2 = await clocks.finalise_due_forfeits()
    check("not finalised early", n2, 0)
    check("no finalise call", len(_calls["finalise"]), 0)

    print("checkpoint names the winner but the DAA deadline HASN'T passed -> WAIT (no claim, no oracle)")
    stub_sidecar()
    m, a, b = flagged_v3_match(cosigned_claimant=winner_side, deadline_daa=NOW_DAA + 100_000)
    did = await clocks.forfeit_if_flagged(m, clocks.now_ms())
    check("not resolved yet (waiting for the DAA deadline)", did, False)
    check("no claim attempted", len(_calls["claim"]), 0)
    row = db.get_match(m["id"])
    check("match left LIVE for a later poll", row["status"], "live")
    check("not oracle-settled early", row["winner_account_id"], None)
    # ...and once the chain reaches the deadline, the next flag claims it
    stub_sidecar(now_daa=NOW_DAA + 200_000)
    did2 = await clocks.forfeit_if_flagged(db.get_match(m["id"]), clocks.now_ms())
    check("claimed once the deadline passes", did2, True)
    check("claim now attempted", len(_calls["claim"]), 1)
    check("settled via forfeit", db.get_match(m["id"])["forfeit_claim_txid"], "claimtx")

    print("co-signed checkpoint names the LOSER -> oracle fallback (no trustless claim)")
    stub_sidecar()
    loser_side = "A" if winner_side == "B" else "B"
    m, a, b = flagged_v3_match(cosigned_claimant=loser_side)
    did = await clocks.forfeit_if_flagged(m, clocks.now_ms())
    check("still resolved (oracle fallback)", did, True)
    check("no trustless claim", len(_calls["claim"]), 0)
    row = db.get_match(m["id"])
    check("settled by oracle path", row["status"], "settled")
    check("no forfeit claim txid", row["forfeit_claim_txid"], None)

    print("no co-signed checkpoint -> oracle fallback")
    stub_sidecar()
    m, a, b = flagged_v3_match(cosigned_claimant=None)
    did = await clocks.forfeit_if_flagged(m, clocks.now_ms())
    check("resolved", did, True)
    check("no trustless claim", len(_calls["claim"]), 0)
    check("settled", db.get_match(m["id"])["status"], "settled")

    print("a failing on-chain claim falls back to oracle (match still resolves, nothing stranded)")
    stub_sidecar(claim_error="node unreachable")
    m, a, b = flagged_v3_match(cosigned_claimant=winner_side)
    did = await clocks.forfeit_if_flagged(m, clocks.now_ms())
    check("resolved despite claim failure", did, True)
    check("tried the claim", len(_calls["claim"]), 1)
    row = db.get_match(m["id"])
    check("fell back: settled", row["status"], "settled")
    check("fell back: no forfeit txid", row["forfeit_claim_txid"], None)

    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        return 1
    print("all forfeit-driver checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
