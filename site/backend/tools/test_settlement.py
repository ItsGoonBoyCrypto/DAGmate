"""Tests for settlement (settlement.py) — run: python tools/test_settlement.py

Same shape as test_deposits.py: dependency-free, real schema, real
database.py accessors, throwaway DB. The stub is the sidecar, because a real
settle needs a funded escrow on a live testnet node and an arbiter key — none
of which exist on a laptop.

What that means is worth being honest about: this proves the ORCHESTRATION —
who is asked to sign what, that the tx is built once, that the payout address
can't be chosen by the caller, that nobody can broadcast twice. It does NOT
prove that a Kasware signature over this tx validates. That's blocker #5 and
needs a real extension.

Every case here is a money case. A settle sends the whole pot somewhere.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DAGMATE_SITE_DB"] = os.path.join(tempfile.mkdtemp(prefix="dagmate-settle-"), "t.db")

import config  # noqa: E402
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


def stub_sidecar(*, utxos_a=1, utxos_b=1, build_error=None, broadcast_error=None):
    """Stand in for service/escrow.js. `utxos_*` matters more than it looks:
    an escrow holding two UTXOs becomes two inputs, and the per-escrow vs
    per-input `escrows` shape mismatch between build and broadcast is the
    easiest thing in this module to get wrong."""
    _calls.clear()
    _calls["build"] = []
    _calls["broadcast"] = []

    async def _unsigned(*, match_id, escrows, winner_addr, split, rake_sompi):
        _calls["build"].append({"match_id": match_id, "escrows": escrows,
                                "winner_addr": winner_addr, "split": split, "rake": rake_sompi})
        if build_error:
            raise ServiceError(build_error)
        inputs, i = [], 0
        for e, n in ((escrows[0], utxos_a), (escrows[1], utxos_b)):
            for _ in range(n):
                inputs.append({"index": i, "address": e["address"]})
                i += 1
        return {"txJson": "{tx}", "sigsArb": [f"arb{n}" for n in range(len(inputs))],
                "potSompi": str(STAKE * 2), "rakeSompi": str(rake_sompi), "inputs": inputs}

    async def _broadcast(*, tx_json, escrows, sigs_player, sigs_arb):
        _calls["broadcast"].append({"tx_json": tx_json, "escrows": escrows,
                                    "sigs_player": sigs_player, "sigs_arb": sigs_arb})
        if broadcast_error:
            raise ServiceError(broadcast_error)
        return {"txid": "txid-abc"}

    settlement.service_client.settle_unsigned = _unsigned
    settlement.service_client.settle_broadcast = _broadcast


def new_match(*, winner: str | None = "a", status="settled", stake=STAKE, escrows=True):
    """A finished match ready to claim. `winner=None` is a draw."""
    with db._lock, db._conn() as c:
        c.execute("DELETE FROM matches")
    a = db.get_or_create_account(f"kaspatest:pA{time.time_ns()}", "aa")
    b = db.get_or_create_account(f"kaspatest:pB{time.time_ns()}", "bb")
    m = db.create_match(challenge_id=None, tournament_id=None, round_no=None,
                        player_a_account_id=a["id"], player_b_account_id=b["id"],
                        stake_sompi=stake, mode="rapid", fen="startpos",
                        escrow_a={"address": ADDR_A, "redeemHex": "aaaa"} if escrows else None,
                        escrow_b={"address": ADDR_B, "redeemHex": "bbbb"} if escrows else None,
                        reclaim_daa=1)
    winner_id = {"a": a["id"], "b": b["id"], None: None}[winner]
    with db._lock, db._conn() as c:
        c.execute("UPDATE matches SET status=?, result='checkmate', winner_account_id=?, "
                  "funded_a_sompi=?, funded_b_sompi=? WHERE id=?",
                  (status, winner_id, stake, stake, m["id"]))
    return m["id"], a, b


async def err(coro) -> str:
    try:
        await coro
        return "<no error>"
    except settlement.SettlementError as e:
        return str(e)


async def main() -> int:
    db.ensure_schema()

    print("a decisive win: the winner signs every input")
    stub_sidecar(utxos_a=1, utxos_b=1)
    mid, a, b = new_match(winner="a")
    p = await settlement.prepare(mid, a["address"])
    check("winner must sign both inputs", p["mySignatureInputs"], [0, 1])
    check("state says so", p["state"], "needs_signature")
    check("winner gets the tx to sign", p["txJson"], "{tx}")
    check("not a draw", p["isDraw"], False)
    check("winner is told they won", p["youWon"], True)
    check("payout is pot minus network fee",
          p["payoutKas"], (STAKE * 2 - 2 * config.SETTLE_FEE_SOMPI_PER_INPUT) / config.SOMPI_PER_KAS)
    check("platform takes nothing", p["platformFeeKas"], 0.0)
    check("sidecar asked to pay the winner", _calls["build"][0]["winner_addr"], a["address"])
    check("not a split", _calls["build"][0]["split"], False)
    check("no rake requested", _calls["build"][0]["rake"], 0)

    print("the loser has nothing to sign and isn't handed the tx")
    q = await settlement.prepare(mid, b["address"])
    check("loser signs nothing", q["mySignatureInputs"], [])
    check("loser gets no txJson", q["txJson"], None)
    check("loser told they didn't win", q["youWon"], False)
    check("loser waits on nobody's signature but the winner's", q["waitingOnOpponent"], True)

    print("the sidecar is addressed by HD index, not the public UUID")
    # The arbiter key is derived from this number. Send the UUID and the
    # sidecar signs with a different key, producing a tx that can never spend
    # the escrow it was built for.
    check("build used hd_index", _calls["build"][0]["match_id"], db.get_match(mid)["hd_index"])

    print("the tx is built once and reused")
    before = len(_calls["build"])
    await settlement.prepare(mid, a["address"])
    await settlement.prepare(mid, b["address"])
    check("no rebuild on repeat prepare", len(_calls["build"]), before)

    print("the winner signs and it broadcasts")
    r = await settlement.submit(mid, a["address"], {0: "sigA0", 1: "sigA1"})
    check("broadcast happened", len(_calls["broadcast"]), 1)
    check("player sigs passed through", _calls["broadcast"][0]["sigs_player"], ["sigA0", "sigA1"])
    check("arbiter sigs passed through", _calls["broadcast"][0]["sigs_arb"], ["arb0", "arb1"])
    check("txid recorded", r["txid"], "txid-abc")
    check("state is broadcast", r["state"], "broadcast")
    check("txid persisted", db.get_match(mid)["settle_txid"], "txid-abc")

    print("a second claim doesn't broadcast again")
    r2 = await settlement.submit(mid, a["address"], {0: "sigA0", 1: "sigA1"})
    check("still one broadcast", len(_calls["broadcast"]), 1)
    check("returns the same txid", r2["txid"], "txid-abc")
    check("prepare after broadcast doesn't rebuild", len(_calls["build"]), before)

    print("a player can't sign an input that isn't theirs")
    stub_sidecar()
    mid, a, b = new_match(winner="a")
    await settlement.prepare(mid, a["address"])
    check("loser signing the winner's input is refused",
          await err(settlement.submit(mid, b["address"], {0: "forged"})),
          "input 0 isn't yours to sign")
    check("nothing stored", json.loads(db.get_match(mid)["settle_sigs_player_json"]), [None, None])
    check("out-of-range index refused",
          await err(settlement.submit(mid, a["address"], {7: "sig"})),
          "input 7 isn't part of this settlement")

    print("a stranger can't touch the settlement at all")
    outsider = db.get_or_create_account(f"kaspatest:evil{time.time_ns()}", "cc")
    check("prepare refused", await err(settlement.prepare(mid, outsider["address"])),
          "you're not a player in this match")
    check("submit refused", await err(settlement.submit(mid, outsider["address"], {0: "sig"})),
          "you're not a player in this match")

    print("a draw: each player signs their OWN escrow, and only when both have")
    stub_sidecar(utxos_a=1, utxos_b=1)
    mid, a, b = new_match(winner=None)
    pa = await settlement.prepare(mid, a["address"])
    pb = await settlement.prepare(mid, b["address"])
    check("A signs input 0 only", pa["mySignatureInputs"], [0])
    check("B signs input 1 only", pb["mySignatureInputs"], [1])
    check("marked as a draw", pa["isDraw"], True)
    check("nobody 'won'", pa["youWon"], False)
    check("split requested of the sidecar", _calls["build"][0]["split"], True)
    check("no winner address on a draw", _calls["build"][0]["winner_addr"], None)
    check("escrow order is A then B",
          [e["depositorAddr"] for e in _calls["build"][0]["escrows"]], [a["address"], b["address"]])
    check("each player is refunded half",
          pa["payoutKas"],
          (STAKE * 2 - 2 * config.SETTLE_FEE_SOMPI_PER_INPUT) / config.SOMPI_PER_KAS / 2)

    print("half a draw doesn't move any money")
    r = await settlement.submit(mid, a["address"], {0: "sigA"})
    check("no broadcast yet", len(_calls["broadcast"]), 0)
    check("A now waits on B", r["waitingOnOpponent"], True)
    check("A has nothing left to sign", r["mySignatureInputs"], [])
    check("A's signature survives", json.loads(db.get_match(mid)["settle_sigs_player_json"]),
          ["sigA", None])

    print("B completes it days later, against the SAME tx")
    r = await settlement.submit(mid, b["address"], {1: "sigB"})
    check("broadcast now", len(_calls["broadcast"]), 1)
    check("both signatures used", _calls["broadcast"][0]["sigs_player"], ["sigA", "sigB"])
    check("the tx B signed is the one A signed", _calls["broadcast"][0]["tx_json"], "{tx}")
    check("txid returned", r["txid"], "txid-abc")

    print("an escrow with two UTXOs is two inputs, and both get its redeem script")
    stub_sidecar(utxos_a=2, utxos_b=1)
    mid, a, b = new_match(winner="b")
    p = await settlement.prepare(mid, b["address"])
    check("winner signs all three inputs", p["mySignatureInputs"], [0, 1, 2])
    await settlement.submit(mid, b["address"], {0: "s0", 1: "s1", 2: "s2"})
    check("broadcast escrows are per INPUT, not per escrow",
          [e["redeemHex"] for e in _calls["broadcast"][0]["escrows"]], ["aaaa", "aaaa", "bbbb"])
    check("network fee counts every input",
          (await settlement.prepare(mid, b["address"]))["networkFeeKas"],
          3 * config.SETTLE_FEE_SOMPI_PER_INPUT / config.SOMPI_PER_KAS)

    print("a gas-only pot is refused before anyone opens a wallet")
    stub_sidecar()
    mid, a, b = new_match(winner="a", stake=config.GAS_ONLY_STAKE_SOMPI)
    msg = await err(settlement.prepare(mid, a["address"]))
    check("explains why there's nothing to claim", "smaller than the Kaspa network fee" in msg, True)
    check("sidecar never called", len(_calls["build"]), 0)

    print("an unfinished match can't be settled")
    stub_sidecar()
    mid, a, b = new_match(winner="a", status="live")
    check("live match refused", await err(settlement.prepare(mid, a["address"])),
          "this match hasn't finished yet")
    check("sidecar never called", len(_calls["build"]), 0)

    print("a match with no escrows says so instead of crashing")
    stub_sidecar()
    mid, a, b = new_match(winner="a", escrows=False)
    check("refused with a reason",
          "no escrow addresses" in await err(settlement.prepare(mid, a["address"])), True)

    print("a broadcast failure after a real txid landed is treated as success")
    # Both players completing the set in the same instant: the second submit is
    # a double-spend the node rejects. That's the system working, not an error
    # to show the player.
    stub_sidecar(broadcast_error="already spent")
    mid, a, b = new_match(winner="a")
    await settlement.prepare(mid, a["address"])
    db.mark_settlement_broadcast(mid, "txid-from-the-other-tab")
    # settle_txid is set, so submit short-circuits before it ever broadcasts.
    r = await settlement.submit(mid, a["address"], {0: "s0", 1: "s1"})
    check("reports the winning txid", r["txid"], "txid-from-the-other-tab")
    check("never tried to broadcast", len(_calls["broadcast"]), 0)

    print("a genuine broadcast failure IS surfaced")
    stub_sidecar(broadcast_error="node unreachable")
    mid, a, b = new_match(winner="a")
    await settlement.prepare(mid, a["address"])
    check("error reaches the player",
          await err(settlement.submit(mid, a["address"], {0: "s0", 1: "s1"})), "node unreachable")
    check("no txid recorded", db.get_match(mid)["settle_txid"], None)
    check("signatures kept for the retry",
          json.loads(db.get_match(mid)["settle_sigs_player_json"]), ["s0", "s1"])

    print("the build guard: only the first concurrent claim writes")
    stub_sidecar()
    mid, a, b = new_match(winner="a")
    first = db.save_settlement_build(mid, tx_json="{first}", inputs=[{"index": 0, "address": ADDR_A, "signer": a["address"]}],
                                     sigs_arb=["x"], pot_sompi=1, rake_sompi=0)
    second = db.save_settlement_build(mid, tx_json="{second}", inputs=[{"index": 0, "address": ADDR_A, "signer": a["address"]}],
                                      sigs_arb=["y"], pot_sompi=1, rake_sompi=0)
    check("first build wins", first, True)
    check("second build refused", second, False)
    check("stored tx is the first one", db.get_match(mid)["settle_tx_json"], "{first}")

    print("the broadcast guard: only the first txid is recorded")
    check("first mark wins", db.mark_settlement_broadcast(mid, "tx1"), True)
    check("second mark refused", db.mark_settlement_broadcast(mid, "tx2"), False)
    check("stored txid is the first", db.get_match(mid)["settle_txid"], "tx1")

    print("signatures can't be written against an already-broadcast settle")
    check("refused", db.save_settlement_sigs(mid, ["late"]), False)

    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        return 1
    print("all settlement checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
