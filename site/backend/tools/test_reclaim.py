"""Tests for reclaim (reclaim.py) — run: python tools/test_reclaim.py

Same shape as test_settlement.py: dependency-free, real schema, real
database.py accessors, throwaway DB, sidecar stubbed. It proves the POLICY —
who may reclaim which escrow and when — not the Kaspa script. The script side
(CLTV pops its own locktime, sequence must differ from MAX, the low-level
builder has no change output) was proven on mainnet dust in service/spikes.mjs
and is enforced in service/escrow.js.

The case this file exists for is the third one down: a settled match whose pot
a winner can still claim must NOT be reclaimable, or "wait two weeks and take
your stake back" becomes a way to undo a loss.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DAGMATE_SITE_DB"] = os.path.join(tempfile.mkdtemp(prefix="dagmate-reclaim-"), "t.db")

import config  # noqa: E402
import database as db  # noqa: E402
import reclaim  # noqa: E402
from service_client import ServiceError  # noqa: E402

STAKE = 10 * config.SOMPI_PER_KAS
ADDR_A, ADDR_B = "kaspatest:escrowA", "kaspatest:escrowB"
RECLAIM_DAA = 500_000

_failures: list[str] = []
_calls: dict = {}


def check(name: str, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        _failures.append(name)


def stub_sidecar(*, utxos=1, held=STAKE, build_error=None, broadcast_error=None):
    _calls.clear()
    _calls["build"] = []
    _calls["broadcast"] = []

    async def _unsigned(*, address, depositor_addr, reclaim_daa):
        _calls["build"].append({"address": address, "depositor_addr": depositor_addr,
                                "reclaim_daa": reclaim_daa})
        if build_error:
            raise ServiceError(build_error)
        fee = config.RECLAIM_FEE_SOMPI_PER_INPUT * utxos
        return {"txJson": "{reclaimtx}",
                "inputs": [{"index": i, "address": address} for i in range(utxos)],
                "totalSompi": str(held), "feeSompi": str(fee),
                "payoutSompi": str(held - fee),
                "tipDaa": str(reclaim_daa + 1), "reclaimDaa": str(reclaim_daa)}

    async def _broadcast(*, tx_json, redeem_hex, sigs):
        _calls["broadcast"].append({"tx_json": tx_json, "redeem_hex": redeem_hex, "sigs": sigs})
        if broadcast_error:
            raise ServiceError(broadcast_error)
        return {"txid": "reclaim-txid"}

    reclaim.service_client.reclaim_unsigned = _unsigned
    reclaim.service_client.reclaim_broadcast = _broadcast


def new_match(*, status="expired", funded_a=STAKE, funded_b=0, winner=None,
              settle_txid=None, stake=STAKE, escrows=True):
    with db._lock, db._conn() as c:
        c.execute("DELETE FROM matches")
    a = db.get_or_create_account(f"kaspatest:pA{time.time_ns()}", "aa")
    b = db.get_or_create_account(f"kaspatest:pB{time.time_ns()}", "bb")
    m = db.create_match(challenge_id=None, tournament_id=None, round_no=None,
                        player_a_account_id=a["id"], player_b_account_id=b["id"],
                        stake_sompi=stake, mode="rapid", fen="startpos",
                        escrow_a={"address": ADDR_A, "redeemHex": "aaaa"} if escrows else None,
                        escrow_b={"address": ADDR_B, "redeemHex": "bbbb"} if escrows else None,
                        reclaim_daa=RECLAIM_DAA if escrows else None)
    winner_id = {"a": a["id"], "b": b["id"], None: None}[winner]
    with db._lock, db._conn() as c:
        c.execute("UPDATE matches SET status=?, winner_account_id=?, settle_txid=?, "
                  "funded_a_sompi=?, funded_b_sompi=? WHERE id=?",
                  (status, winner_id, settle_txid, funded_a, funded_b, m["id"]))
    return m["id"], a, b


async def err(coro) -> str:
    try:
        await coro
        return "<no error>"
    except reclaim.ReclaimError as e:
        return str(e)


async def main() -> int:
    db.ensure_schema()

    print("the ordinary case: one player funded, the other never did")
    stub_sidecar()
    mid, a, b = new_match(status="expired", funded_a=STAKE, funded_b=0)
    p = await reclaim.prepare(mid, a["address"])
    check("offered", p["state"], "needs_signature")
    check("A drains escrow A", _calls["build"][0]["address"], ADDR_A)
    check("payout goes to A's own address", _calls["build"][0]["depositor_addr"], a["address"])
    check("timelock passed through from the DB", _calls["build"][0]["reclaim_daa"], RECLAIM_DAA)
    check("A is asked for one signature", p["mySignatureInputs"], [0])
    check("stake back, minus the fee",
          p["payoutKas"], (STAKE - config.RECLAIM_FEE_SOMPI_PER_INPUT) / config.SOMPI_PER_KAS)
    check("platform takes nothing (fee is the network's)",
          p["networkFeeKas"], config.RECLAIM_FEE_SOMPI_PER_INPUT / config.SOMPI_PER_KAS)
    check("redeem script published for the escape hatch", p["redeemHex"], "aaaa")

    print("the player who never paid has nothing to reclaim, and no RPC is spent saying so")
    before = len(_calls["build"])
    msg = await err(reclaim.prepare(mid, b["address"]))
    check("refused", "nothing here to reclaim" in msg, True)
    check("sidecar never called", len(_calls["build"]), before)

    print("!! a settled match a winner can still claim is NOT reclaimable")
    # Otherwise the timelock is a free undo: lose, wait, take your stake back
    # before the winner gets round to claiming.
    stub_sidecar()
    mid, a, b = new_match(status="settled", winner="a", funded_a=STAKE, funded_b=STAKE)
    check("loser refused",
          "can still be released to its winner" in await err(reclaim.prepare(mid, b["address"])), True)
    check("winner refused too — they should settle, not reclaim",
          "can still be released to its winner" in await err(reclaim.prepare(mid, a["address"])), True)
    check("not advertised in the match view", reclaim.summary(db.get_match(mid))["eligible"], False)
    check("sidecar never called", len(_calls["build"]), 0)

    print("a live game is never reclaimable")
    stub_sidecar()
    mid, a, b = new_match(status="live", funded_a=STAKE, funded_b=STAKE)
    check("refused", await err(reclaim.prepare(mid, a["address"])), "this match is still being played")
    check("not advertised", reclaim.summary(db.get_match(mid))["eligible"], False)

    print("a gas-only pot IS reclaimable — the 2-of-3 branch can't release it")
    stub_sidecar(held=config.GAS_ONLY_STAKE_SOMPI)
    mid, a, b = new_match(status="settled", winner="a", stake=config.GAS_ONLY_STAKE_SOMPI,
                          funded_a=config.GAS_ONLY_STAKE_SOMPI, funded_b=config.GAS_ONLY_STAKE_SOMPI)
    check("advertised", reclaim.summary(db.get_match(mid))["eligible"], True)
    check("offered to the depositor", (await reclaim.prepare(mid, a["address"]))["state"],
          "needs_signature")

    print("an already-paid-out match is not reclaimable")
    stub_sidecar()
    mid, a, b = new_match(status="settled", winner="a", funded_a=STAKE, funded_b=STAKE,
                          settle_txid="paid")
    check("not advertised", reclaim.summary(db.get_match(mid))["eligible"], False)
    check("refused", "can still be released" in await err(reclaim.prepare(mid, a["address"])), True)

    print("a stranger can't reclaim anyone's escrow")
    stub_sidecar()
    mid, a, b = new_match(status="expired", funded_a=STAKE)
    outsider = db.get_or_create_account(f"kaspatest:evil{time.time_ns()}", "cc")
    check("prepare refused", await err(reclaim.prepare(mid, outsider["address"])),
          "you're not a player in this match")
    check("submit refused", await err(reclaim.submit(mid, outsider["address"], "{tx}", ["sig"])),
          "you're not a player in this match")
    check("sidecar never called", len(_calls["build"]) + len(_calls["broadcast"]), 0)

    print("signing and broadcasting")
    stub_sidecar()
    mid, a, b = new_match(status="expired", funded_a=STAKE)
    p = await reclaim.prepare(mid, a["address"])
    r = await reclaim.submit(mid, a["address"], p["txJson"], ["sigA0"])
    check("broadcast happened", len(_calls["broadcast"]), 1)
    check("signature passed through", _calls["broadcast"][0]["sigs"], ["sigA0"])
    check("redeem script came from the DB, not the caller",
          _calls["broadcast"][0]["redeem_hex"], "aaaa")
    check("txid returned", r["txid"], "reclaim-txid")
    check("txid persisted on A's side", db.get_match(mid)["reclaim_a_txid"], "reclaim-txid")
    check("B's side untouched", db.get_match(mid)["reclaim_b_txid"], None)
    check("surfaced in the match view", reclaim.summary(db.get_match(mid))["aTxid"], "reclaim-txid")

    print("a second click can't spend it again or overwrite the receipt")
    check("refused", await err(reclaim.prepare(mid, a["address"])), "you've already reclaimed this stake")
    check("submit refused too",
          await err(reclaim.submit(mid, a["address"], "{reclaimtx}", ["sigA0"])),
          "you've already reclaimed this stake")
    check("still one broadcast", len(_calls["broadcast"]), 1)
    check("receipt guard holds", db.mark_reclaim_broadcast(mid, "a", "second-txid"), False)
    check("original txid kept", db.get_match(mid)["reclaim_a_txid"], "reclaim-txid")

    print("both players can reclaim their own escrow independently")
    stub_sidecar()
    mid, a, b = new_match(status="expired", funded_a=STAKE, funded_b=STAKE)
    pa = await reclaim.prepare(mid, a["address"])
    pb = await reclaim.prepare(mid, b["address"])
    check("A gets escrow A", _calls["build"][0]["address"], ADDR_A)
    check("B gets escrow B", _calls["build"][1]["address"], ADDR_B)
    check("A's redeem script", pa["redeemHex"], "aaaa")
    check("B's redeem script", pb["redeemHex"], "bbbb")
    await reclaim.submit(mid, a["address"], pa["txJson"], ["sA"])
    check("A's reclaim doesn't block B's", (await reclaim.prepare(mid, b["address"]))["state"],
          "needs_signature")
    await reclaim.submit(mid, b["address"], pb["txJson"], ["sB"])
    m = db.get_match(mid)
    check("two separate receipts", (m["reclaim_a_txid"], m["reclaim_b_txid"]),
          ("reclaim-txid", "reclaim-txid"))

    print("an escrow holding several UTXOs needs a signature for each")
    stub_sidecar(utxos=3)
    mid, a, b = new_match(status="expired", funded_a=STAKE)
    p = await reclaim.prepare(mid, a["address"])
    check("three inputs to sign", p["mySignatureInputs"], [0, 1, 2])
    check("fee counts every input",
          p["networkFeeKas"], 3 * config.RECLAIM_FEE_SOMPI_PER_INPUT / config.SOMPI_PER_KAS)
    check("a short signature list is refused",
          await err(reclaim.submit(mid, a["address"], p["txJson"], ["s0", None, "s2"])),
          "your wallet didn't return a signature for every input")
    check("nothing broadcast", len(_calls["broadcast"]), 0)

    print("the sidecar's own refusals reach the player")
    stub_sidecar(build_error="timelock hasn't opened yet - reclaimable at DAA 500000, chain is at 4")
    mid, a, b = new_match(status="expired", funded_a=STAKE)
    check("early reclaim explained",
          "timelock hasn't opened yet" in await err(reclaim.prepare(mid, a["address"])), True)
    stub_sidecar(broadcast_error="nothing left in this escrow to reclaim")
    p_mid, p_a, _ = new_match(status="expired", funded_a=STAKE)
    p = await reclaim.prepare(p_mid, p_a["address"])
    check("broadcast failure surfaced",
          await err(reclaim.submit(p_mid, p_a["address"], p["txJson"], ["s"])),
          "nothing left in this escrow to reclaim")
    check("no receipt written on failure", db.get_match(p_mid)["reclaim_a_txid"], None)

    print("a match that never got an escrow says so instead of crashing")
    stub_sidecar()
    mid, a, b = new_match(status="expired", funded_a=STAKE, escrows=False)
    check("not advertised", reclaim.summary(db.get_match(mid))["eligible"], False)
    check("refused with a reason",
          "no escrow on chain" in await err(reclaim.prepare(mid, a["address"])), True)

    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        return 1
    print("all reclaim checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
