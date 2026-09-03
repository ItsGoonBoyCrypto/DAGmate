"""Tests for MUTUAL settlement — roadmap #1 (run: python tools/test_mutual_settlement.py)

Same shape and honesty as test_settlement.py: this proves the ORCHESTRATION of
the arbiter-free path — who is asked to co-sign, that both players' signatures
are mapped onto the right (playerA, playerB) redeem slots, that DAGmate's
arbiter key is NOT used when the loser co-operates, and that an absent or
stalling loser can never hold the pot hostage (the arbiter breaks the tie after
the stall window). It does NOT prove a Kasware signature validates on chain —
that's spike S4 (service/spikes.mjs), the gate before DAGMATE_SETTLE_MUTUAL=1.

Every case here is a money case: a settle sends the whole pot somewhere, and the
whole point of #1 is to do it without trusting DAGmate for an honest game.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DAGMATE_SITE_DB"] = os.path.join(tempfile.mkdtemp(prefix="dagmate-mutual-"), "t.db")

import config  # noqa: E402
config.SETTLE_MUTUAL_ENABLED = True   # the whole suite runs with #1 switched on
config.SETTLE_STALL_SECS = 45

import database as db  # noqa: E402
import settlement  # noqa: E402
from service_client import ServiceError  # noqa: E402

STAKE = 10 * config.SOMPI_PER_KAS
ADDR_A, ADDR_B = "kaspatest:escrowA", "kaspatest:escrowB"

_failures: list[str] = []
_calls: dict = {}


def check(name: str, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        _failures.append(name)


def stub_sidecar(*, utxos_a=1, utxos_b=1, mutual_error=None, arb_error=None):
    _calls.clear()
    _calls.update({"build": [], "broadcast_mutual": [], "broadcast_arb": [], "extract": []})

    async def _extract(*, signed_tx_json, indexes):
        _calls["extract"].append({"signed_tx_json": signed_tx_json, "indexes": list(indexes)})
        return {"sigs": {str(i): f"{signed_tx_json}#{i}" for i in indexes}}

    async def _unsigned(*, match_id, escrows, winner_addr, split, rake_sompi):
        _calls["build"].append({"match_id": match_id, "escrows": escrows,
                                "winner_addr": winner_addr, "split": split, "rake": rake_sompi})
        inputs, i = [], 0
        for e, n in ((escrows[0], utxos_a), (escrows[1], utxos_b)):
            for _ in range(n):
                inputs.append({"index": i, "address": e["address"]})
                i += 1
        return {"txJson": "{tx}", "sigsArb": [f"arb{n}" for n in range(len(inputs))],
                "potSompi": str(STAKE * 2), "rakeSompi": str(rake_sompi), "inputs": inputs}

    async def _broadcast_mutual(*, tx_json, escrows, sigs_a, sigs_b):
        _calls["broadcast_mutual"].append({"tx_json": tx_json, "escrows": escrows,
                                           "sigs_a": sigs_a, "sigs_b": sigs_b})
        if mutual_error:
            raise ServiceError(mutual_error)
        return {"txid": "txid-mutual"}

    async def _broadcast_arb(*, tx_json, escrows, sigs_player, sigs_arb):
        _calls["broadcast_arb"].append({"tx_json": tx_json, "escrows": escrows,
                                        "sigs_player": sigs_player, "sigs_arb": sigs_arb})
        if arb_error:
            raise ServiceError(arb_error)
        return {"txid": "txid-arb"}

    settlement.service_client.settle_unsigned = _unsigned
    settlement.service_client.settle_broadcast_mutual = _broadcast_mutual
    settlement.service_client.settle_broadcast = _broadcast_arb
    settlement.service_client.extract_sigs = _extract


def new_match(*, winner="a", stake=STAKE):
    with db._lock, db._conn() as c:
        c.execute("DELETE FROM matches")
    a = db.get_or_create_account(f"kaspatest:pA{time.time_ns()}", "aa")
    b = db.get_or_create_account(f"kaspatest:pB{time.time_ns()}", "bb")
    m = db.create_match(challenge_id=None, tournament_id=None, round_no=None,
                        player_a_account_id=a["id"], player_b_account_id=b["id"],
                        stake_sompi=stake, mode="rapid", fen="startpos",
                        escrow_a={"address": ADDR_A, "redeemHex": "aaaa"},
                        escrow_b={"address": ADDR_B, "redeemHex": "bbbb"}, reclaim_daa=1)
    winner_id = {"a": a["id"], "b": b["id"], None: None}[winner]
    with db._lock, db._conn() as c:
        c.execute("UPDATE matches SET status='settled', result='checkmate', winner_account_id=?, "
                  "funded_a_sompi=?, funded_b_sompi=? WHERE id=?",
                  (winner_id, stake, stake, m["id"]))
    return m["id"], a, b


def _backdate_prepared(mid, secs):
    with db._lock, db._conn() as c:
        c.execute("UPDATE matches SET settle_prepared_ts=? WHERE id=?",
                  (int(time.time()) - secs, mid))


async def err(coro) -> str:
    try:
        await coro
        return "<no error>"
    except settlement.SettlementError as e:
        return str(e)


async def main() -> int:
    db.ensure_schema()

    print("the loser is asked to CO-SIGN the winner's payout, not to claim")
    stub_sidecar()
    mid, a, b = new_match(winner="a")
    pw = await settlement.prepare(mid, a["address"])   # winner
    pl = await settlement.prepare(mid, b["address"])   # loser
    check("winner signs every input", pw["mySignatureInputs"], [0, 1])
    check("winner claims their own pot (not a co-sign)", pw["cosignAsk"], False)
    check("loser is asked to co-sign every input", pl["mySignatureInputs"], [0, 1])
    check("loser's panel is a release, not a claim", pl["cosignAsk"], True)
    check("loser is handed the tx to sign", pl["txJson"], "{tx}")
    check("loser is told they didn't win", pl["youWon"], False)
    check("loser's own payout is zero", pl["payoutSompi"], "0")

    print("loser co-signs then winner claims -> released with NO arbiter")
    await settlement.submit(mid, b["address"], "LOSE")   # loser co-signs first
    check("no broadcast on the loser's co-sign alone", len(_calls["broadcast_mutual"]), 0)
    r = await settlement.submit(mid, a["address"], "WIN")  # winner completes it
    check("released via the mutual (arbiter-free) path", len(_calls["broadcast_mutual"]), 1)
    check("the arbiter path was never called", len(_calls["broadcast_arb"]), 0)
    check("txid recorded from the mutual broadcast", r["txid"], "txid-mutual")
    check("state is broadcast", r["state"], "broadcast")

    print("winner=A: sigsA are the winner's, sigsB are the loser's (pubkey order)")
    bc = _calls["broadcast_mutual"][0]
    check("sigsA = winner (A) per input", bc["sigs_a"], ["WIN#0", "WIN#1"])
    check("sigsB = loser (B) per input", bc["sigs_b"], ["LOSE#0", "LOSE#1"])
    check("escrows are per input", [e["redeemHex"] for e in bc["escrows"]], ["aaaa", "bbbb"])

    print("winner=B flips the roles: the winner's sigs go in the B slot")
    stub_sidecar()
    mid, a, b = new_match(winner="b")
    await settlement.prepare(mid, b["address"])          # build the tx
    await settlement.submit(mid, a["address"], "LOSE")   # A is the loser here
    await settlement.submit(mid, b["address"], "WIN")    # B is the winner
    bc = _calls["broadcast_mutual"][0]
    check("sigsA = loser (A)", bc["sigs_a"], ["LOSE#0", "LOSE#1"])
    check("sigsB = winner (B)", bc["sigs_b"], ["WIN#0", "WIN#1"])

    print("winner signs but the loser hasn't yet: held for the stall window")
    stub_sidecar()
    mid, a, b = new_match(winner="a")
    await settlement.prepare(mid, a["address"])          # build the tx
    r = await settlement.submit(mid, a["address"], "WIN")
    check("nothing broadcast yet", len(_calls["broadcast_mutual"]) + len(_calls["broadcast_arb"]), 0)
    check("winner's panel says it's auto-releasing", r["awaitingCosign"], True)
    check("a countdown is shown", isinstance(r["autoReleaseInSecs"], int), True)
    check("winner has nothing left to sign", r["mySignatureInputs"], [])

    print("the loser co-signing inside the window still takes the arbiter-free path")
    r = await settlement.submit(mid, b["address"], "LOSE")
    check("released mutually", len(_calls["broadcast_mutual"]), 1)
    check("arbiter still untouched", len(_calls["broadcast_arb"]), 0)
    check("txid is the mutual one", r["txid"], "txid-mutual")

    print("!! a stalling / absent loser can't hold the pot: arbiter breaks the tie")
    stub_sidecar()
    mid, a, b = new_match(winner="a")
    await settlement.prepare(mid, a["address"])          # build the tx
    await settlement.submit(mid, a["address"], "WIN")     # winner signs, loser never shows
    check("held during the window", len(_calls["broadcast_arb"]), 0)
    _backdate_prepared(mid, config.SETTLE_STALL_SECS + 5)  # window lapses
    # The winner's panel poll (prepare) now flips to the arbiter stall-breaker.
    p = await settlement.prepare(mid, a["address"])
    check("released via winner+arbiter after the stall", len(_calls["broadcast_arb"]), 1)
    check("never used the mutual path", len(_calls["broadcast_mutual"]), 0)
    check("winner's own signature carried the arbiter path", _calls["broadcast_arb"][0]["sigs_player"],
          ["WIN#0", "WIN#1"])
    check("arbiter sigs came from the build", _calls["broadcast_arb"][0]["sigs_arb"], ["arb0", "arb1"])
    check("txid is the arbiter one", p["txid"], "txid-arb")

    print("the loser cannot write into the winner's primary signature slots")
    stub_sidecar()
    mid, a, b = new_match(winner="a")
    await settlement.prepare(mid, b["address"])          # build the tx
    await settlement.submit(mid, b["address"], "LOSE")
    check("loser's sig lands in the cosign array, not the player array",
          json.loads(db.get_match(mid)["settle_sigs_player_json"]), [None, None])
    check("cosign array holds the loser's sigs",
          json.loads(db.get_match(mid)["settle_sigs_cosign_json"]), ["LOSE#0", "LOSE#1"])

    print("two-UTXO escrow: every input gets a winner AND a loser signature")
    stub_sidecar(utxos_a=2, utxos_b=1)
    mid, a, b = new_match(winner="b")
    pl = await settlement.prepare(mid, a["address"])   # loser
    check("loser co-signs all three inputs", pl["mySignatureInputs"], [0, 1, 2])
    await settlement.submit(mid, a["address"], "LOSE")
    await settlement.submit(mid, b["address"], "WIN")
    bc = _calls["broadcast_mutual"][0]
    check("sigsA = loser across A's two inputs, winner not in A", bc["sigs_a"],
          ["LOSE#0", "LOSE#1", "LOSE#2"])
    check("sigsB = winner across all three", bc["sigs_b"], ["WIN#0", "WIN#1", "WIN#2"])
    check("redeem per input follows the escrow layout",
          [e["redeemHex"] for e in bc["escrows"]], ["aaaa", "aaaa", "bbbb"])

    print("a DRAW ignores the mutual path entirely (still winner-less arbiter split)")
    stub_sidecar()
    mid, a, b = new_match(winner=None)
    pa = await settlement.prepare(mid, a["address"])
    pb = await settlement.prepare(mid, b["address"])
    check("A signs only their own escrow", pa["mySignatureInputs"], [0])
    check("B signs only their own escrow", pb["mySignatureInputs"], [1])
    check("neither is a co-sign ask", (pa["cosignAsk"], pb["cosignAsk"]), (False, False))
    await settlement.submit(mid, a["address"], "SIGNED-A")
    r = await settlement.submit(mid, b["address"], "SIGNED-B")
    check("draw released via the arbiter path", len(_calls["broadcast_arb"]), 1)
    check("draw never touched the mutual path", len(_calls["broadcast_mutual"]), 0)
    check("draw txid", r["txid"], "txid-arb")

    print("a stranger still can't touch a mutual settlement")
    stub_sidecar()
    mid, a, b = new_match(winner="a")
    outsider = db.get_or_create_account(f"kaspatest:evil{time.time_ns()}", "cc")
    check("prepare refused", await err(settlement.prepare(mid, outsider["address"])),
          "you're not a player in this match")
    check("submit refused", await err(settlement.submit(mid, outsider["address"], "X")),
          "you're not a player in this match")

    print("a mutual broadcast that races a landed txid is treated as success")
    stub_sidecar(mutual_error="already spent")
    mid, a, b = new_match(winner="a")
    await settlement.prepare(mid, a["address"])          # build the tx
    await settlement.submit(mid, b["address"], "LOSE")
    db.mark_settlement_broadcast(mid, "txid-from-other-tab")
    r = await settlement.submit(mid, a["address"], "WIN")  # short-circuits: txid already set
    check("reports the winning txid", r["txid"], "txid-from-other-tab")

    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        return 1
    print("all mutual-settlement checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
