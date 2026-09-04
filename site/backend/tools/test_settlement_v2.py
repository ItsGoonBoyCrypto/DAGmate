"""Tests for covenant-escrow (v2) settlement — roadmap #2.
Run: python tools/test_settlement_v2.py

Same discipline as test_settlement.py: real schema, real accessors, throwaway DB, the sidecar
stubbed. Proves the ORCHESTRATION of the self-settling v2 path — outcome mapping, that DAGmate
signs and relays exactly once, idempotency under concurrent polls, the verdict is stored/
published, a draw pays each depositor back, and gas-only/stranger guards. It does NOT prove the
covenant itself — that's service/spikes_covenant.mjs (S5/S6/S6adv/S7) + test_escrow_v2.mjs, all
green on mainnet dust.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DAGMATE_SITE_DB"] = os.path.join(tempfile.mkdtemp(prefix="dagmate-v2-"), "t.db")

import config  # noqa: E402
import database as db  # noqa: E402
import settlement  # noqa: E402
from service_client import ServiceError  # noqa: E402

STAKE = 10 * config.SOMPI_PER_KAS
ADDR_A, ADDR_B = "kaspa:escrowVA", "kaspa:escrowVB"
FEE = config.SETTLE_V2_FEE_SOMPI_PER_INPUT

_failures: list[str] = []
_calls: dict = {}


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        _failures.append(name)


def stub_sidecar(*, settle_error=None, settle_txid="txid-v2"):
    _calls.clear()
    _calls.update({"sign": [], "settle": []})

    async def _sign(*, match_id, outcome):
        _calls["sign"].append({"match_id": match_id, "outcome": outcome})
        return {"outcome": outcome, "sigA": f"sigA-{outcome}", "sigB": f"sigB-{outcome}"}

    async def _settle(*, match_id, escrows, outcome, pk_a, pk_b, sig_a, sig_b):
        _calls["settle"].append({"match_id": match_id, "escrows": escrows, "outcome": outcome,
                                 "pk_a": pk_a, "pk_b": pk_b, "sig_a": sig_a, "sig_b": sig_b})
        if settle_error:
            raise ServiceError(settle_error)
        return {"txid": settle_txid, "potSompi": str(STAKE * 2), "feeSompi": str(FEE * 2), "outcome": outcome}

    settlement.service_client.oracle_sign_result = _sign
    settlement.service_client.settle_v2 = _settle


def new_v2_match(*, winner="a", stake=STAKE):
    with db._lock, db._conn() as c:
        c.execute("DELETE FROM matches")
    a = db.get_or_create_account(f"kaspa:pA{time.time_ns()}", "pubA")
    b = db.get_or_create_account(f"kaspa:pB{time.time_ns()}", "pubB")
    m = db.create_match(challenge_id=None, tournament_id=None, round_no=None,
                        player_a_account_id=a["id"], player_b_account_id=b["id"],
                        stake_sompi=stake, mode="rapid", fen="startpos",
                        escrow_a={"address": ADDR_A, "redeemHex": "aa"},
                        escrow_b={"address": ADDR_B, "redeemHex": "bb"}, reclaim_daa=1)
    winner_id = {"a": a["id"], "b": b["id"], None: None}[winner]
    with db._lock, db._conn() as c:
        c.execute("UPDATE matches SET status='settled', result='checkmate', winner_account_id=?, "
                  "escrow_version='v2', funded_a_sompi=?, funded_b_sompi=? WHERE id=?",
                  (winner_id, stake, stake, m["id"]))
    return m["id"], a, b


async def err(coro) -> str:
    try:
        await coro
        return "<no error>"
    except settlement.SettlementError as e:
        return str(e)


async def main() -> int:
    db.ensure_schema()

    print("a decisive v2 win self-settles on the first poll — no signature")
    stub_sidecar()
    mid, a, b = new_v2_match(winner="a")
    p = await settlement.prepare(mid, a["address"])
    check("oracle signed exactly once", len(_calls["sign"]), 1)
    check("signed outcome A", _calls["sign"][0]["outcome"], "A")
    check("settle called once", len(_calls["settle"]), 1)
    check("settle addressed by hd_index", _calls["settle"][0]["match_id"], db.get_match(mid)["hd_index"])
    check("settle got both players' pubkeys", (_calls["settle"][0]["pk_a"], _calls["settle"][0]["pk_b"]), ("pubA", "pubB"))
    check("settle got A/B escrows with sides",
          [(e["side"], e["redeemHex"]) for e in _calls["settle"][0]["escrows"]], [("A", "aa"), ("B", "bb")])
    check("state is broadcast", p["state"], "broadcast")
    check("txid returned", p["txid"], "txid-v2")
    check("nothing to sign", p["mySignatureInputs"], [])
    check("auto-settled flag", p["autoSettled"], True)
    check("winner sees they won", p["youWon"], True)
    check("payout is pot minus 2 input fees", p["payoutSompi"], str(STAKE * 2 - 2 * FEE))
    check("verdict stored+published", p["verdict"], {"outcome": "A", "sigA": "sigA-A", "sigB": "sigB-A"})
    check("txid persisted", db.get_match(mid)["settle_txid"], "txid-v2")

    print("a second poll does NOT settle again (idempotent)")
    before = len(_calls["settle"])
    q = await settlement.prepare(mid, b["address"])
    check("no second settle", len(_calls["settle"]), before)
    check("loser also sees it paid", q["state"], "broadcast")
    check("loser's payout is zero", q["payoutSompi"], "0")
    check("loser knows they lost", q["youWon"], False)

    print("a v2 DRAW pays each depositor their own stake back")
    stub_sidecar()
    mid, a, b = new_v2_match(winner=None)
    pa = await settlement.prepare(mid, a["address"])
    check("signed outcome draw", _calls["sign"][0]["outcome"], "draw")
    check("settle outcome draw", _calls["settle"][0]["outcome"], "draw")
    check("marked a draw", pa["isDraw"], True)
    check("A gets their stake back minus one fee", pa["payoutSompi"], str(STAKE - FEE))
    pb = await settlement.prepare(mid, b["address"])
    check("B gets their stake back minus one fee", pb["payoutSompi"], str(STAKE - FEE))

    print("submit on a v2 match is a harmless self-settle (no signature needed)")
    stub_sidecar()
    mid, a, b = new_v2_match(winner="b")
    r = await settlement.submit(mid, b["address"], "IGNORED-NO-SIG")
    check("submit settled it", r["state"], "broadcast")
    check("submit signed outcome B", _calls["sign"][0]["outcome"], "B")

    print("a gas-only v2 pot is refused before any signing")
    stub_sidecar()
    mid, a, b = new_v2_match(winner="a", stake=config.GAS_ONLY_STAKE_SOMPI)
    msg = await err(settlement.prepare(mid, a["address"]))
    check("explains nothing to claim", "smaller than the Kaspa network fee" in msg, True)
    check("oracle never signed", len(_calls["sign"]), 0)

    print("a stranger can't settle a v2 match")
    stub_sidecar()
    mid, a, b = new_v2_match(winner="a")
    outsider = db.get_or_create_account(f"kaspa:evil{time.time_ns()}", "pubX")
    check("prepare refused", await err(settlement.prepare(mid, outsider["address"])),
          "you're not a player in this match")

    print("a settle that races a landed txid is treated as success")
    stub_sidecar(settle_error="already spent")
    mid, a, b = new_v2_match(winner="a")
    db.mark_v2_settled(mid, "txid-from-other-tab", json.dumps({"outcome": "A"}))
    r = await settlement.prepare(mid, a["address"])
    check("reports the winning txid", r["txid"], "txid-from-other-tab")
    check("never tried to settle", len(_calls["settle"]), 0)

    print("a genuine settle failure IS surfaced")
    stub_sidecar(settle_error="node unreachable")
    mid, a, b = new_v2_match(winner="a")
    check("error reaches the player", await err(settlement.prepare(mid, a["address"])), "node unreachable")
    check("no txid recorded", db.get_match(mid)["settle_txid"], None)

    # ── parametrised sweep: every outcome × a range of stakes, from both players' viewpoints ──
    print("SWEEP: outcome × stake × viewpoint — payout math and settle args on every combination")
    stakes = [1, 5, 10, 137, 1000, 1_000_000]  # KAS, incl. an odd value and the max
    for kas in stakes:
        stake = kas * config.SOMPI_PER_KAS
        pot = 2 * stake
        for winner in ("a", "b", None):
            stub_sidecar(settle_txid=f"tx-{kas}-{winner}")
            mid, a, b = new_v2_match(winner=winner, stake=stake)
            outcome = {"a": "A", "b": "B", None: "draw"}[winner]
            # settle from A's viewpoint (any player triggers the same server-side settle)
            pa = await settlement.prepare(mid, a["address"])
            pb = await settlement.prepare(mid, b["address"])  # idempotent second view
            tag = f"{kas}KAS/{outcome}"
            check(f"[{tag}] oracle signed the right outcome", _calls["sign"][0]["outcome"], outcome)
            check(f"[{tag}] settle used the right outcome", _calls["settle"][0]["outcome"], outcome)
            check(f"[{tag}] settled exactly once (idempotent across both views)", len(_calls["settle"]), 1)
            check(f"[{tag}] txid persisted", pa["txid"], f"tx-{kas}-{winner}")
            if outcome == "draw":
                check(f"[{tag}] A refunded stake-fee", pa["payoutSompi"], str(stake - FEE))
                check(f"[{tag}] B refunded stake-fee", pb["payoutSompi"], str(stake - FEE))
                check(f"[{tag}] both flagged draw", (pa["isDraw"], pb["isDraw"]), (True, True))
            else:
                winner_addr, loser_addr = (a, b) if outcome == "A" else (b, a)
                pw = pa if winner_addr is a else pb
                pl = pb if winner_addr is a else pa
                check(f"[{tag}] winner paid pot-2fee", pw["payoutSompi"], str(pot - 2 * FEE))
                check(f"[{tag}] winner youWon", pw["youWon"], True)
                check(f"[{tag}] loser paid nothing", pl["payoutSompi"], "0")
                check(f"[{tag}] loser youWon False", pl["youWon"], False)

    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        return 1
    print("all v2 settlement checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
