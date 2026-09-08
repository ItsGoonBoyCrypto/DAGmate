"""Tests for covenant-escrow (v3) settlement — roadmap #3a.
Run: python tools/test_settlement_v3.py

Same discipline as test_settlement_v2.py: real schema, real accessors, throwaway DB, sidecar
stubbed. v3's SETTLE path is byte-for-byte v2's oracle settle (proven on mainnet by
service/spikes_forfeit.mjs S12), so these prove the ORCHESTRATION: the v3 sidecar routes are the
ones called, the payout math/tag are right, it's idempotent, AND — the v3-specific bit — a match
that ended by a trustless FORFEIT (clocks.py already claimed on-chain) reports the challenge-window
state WITHOUT re-signing an oracle verdict. The forfeit covenant itself is proven in S8–S12.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DAGMATE_SITE_DB"] = os.path.join(tempfile.mkdtemp(prefix="dagmate-v3-"), "t.db")

import config  # noqa: E402
# Isolation (feedback_test_db_isolation): config.DB_PATH is captured from the env at import, but set
# it explicitly too and ASSERT it — the env var alone has been silently ignored before, writing test
# rows into MAINNET. database.py reads config.DB_PATH, so pinning it here is what actually isolates.
config.DB_PATH = os.environ["DAGMATE_SITE_DB"]
assert config.DB_PATH.startswith(tempfile.gettempdir()), f"DB not isolated: {config.DB_PATH}"

import database as db  # noqa: E402
import settlement  # noqa: E402
from service_client import ServiceError  # noqa: E402

STAKE = 10 * config.SOMPI_PER_KAS
ADDR_A, ADDR_B = "kaspa:escrowV3A", "kaspa:escrowV3B"
FEE = config.SETTLE_V3_FEE_SOMPI_PER_INPUT

_failures: list[str] = []
_calls: dict = {}


def check(name, got, want):
    if got == want:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        _failures.append(name)


def stub_sidecar(*, settle_error=None, settle_txid="txid-v3"):
    _calls.clear()
    _calls.update({"sign": [], "settle": []})

    async def _sign(*, match_id, outcome):
        _calls["sign"].append({"match_id": match_id, "outcome": outcome})
        return {"outcome": outcome, "sigA": f"sigA-{outcome}", "sigB": f"sigB-{outcome}"}

    async def _settle(*, escrows, outcome, pk_a, pk_b, sig_a, sig_b):
        _calls["settle"].append({"escrows": escrows, "outcome": outcome,
                                 "pk_a": pk_a, "pk_b": pk_b, "sig_a": sig_a, "sig_b": sig_b})
        if settle_error:
            raise ServiceError(settle_error)
        return {"txid": settle_txid, "potSompi": str(STAKE * 2), "feeSompi": str(FEE * 2), "outcome": outcome}

    settlement.service_client.oracle_sign_result_v3 = _sign
    settlement.service_client.settle_v3 = _settle


def new_v3_match(*, winner="a", stake=STAKE, result="checkmate"):
    with db._lock, db._conn() as c:
        c.execute("DELETE FROM matches")
    a = db.get_or_create_account(f"kaspa:pA{time.time_ns()}", "pubA")
    b = db.get_or_create_account(f"kaspa:pB{time.time_ns()}", "pubB")
    m = db.create_match(challenge_id=None, tournament_id=None, round_no=None,
                        player_a_account_id=a["id"], player_b_account_id=b["id"],
                        stake_sompi=stake, mode="rapid", fen="startpos",
                        escrow_a={"address": ADDR_A, "redeemHex": "aa"},
                        escrow_b={"address": ADDR_B, "redeemHex": "bb"}, reclaim_daa=1)
    # pin it v3 with session keys + window, exactly as set_match_escrows(version="v3") would
    db.set_match_escrows(m["id"], {"address": ADDR_A, "redeemHex": "aa"},
                         {"address": ADDR_B, "redeemHex": "bb"}, version="v3",
                         sess_pk_a="a" * 64, sess_pk_b="b" * 64, w_daa=72000)
    winner_id = {"a": a["id"], "b": b["id"], None: None}[winner]
    with db._lock, db._conn() as c:
        c.execute("UPDATE matches SET status='settled', result=?, winner_account_id=?, "
                  "funded_a_sompi=?, funded_b_sompi=? WHERE id=?",
                  (result, winner_id, stake, stake, m["id"]))
    return m["id"], a, b


async def err(coro) -> str:
    try:
        await coro
        return "<no error>"
    except settlement.SettlementError as e:
        return str(e)


async def main() -> int:
    db.ensure_schema()

    print("a decisive v3 win self-settles via the v3 routes — no signature")
    stub_sidecar()
    mid, a, b = new_v3_match(winner="a")
    p = await settlement.prepare(mid, a["address"])
    check("stored as v3", db.get_match(mid)["escrow_version"], "v3")
    check("session keys pinned on the match", (db.get_match(mid)["sess_pk_a"], db.get_match(mid)["sess_pk_b"]), ("a" * 64, "b" * 64))
    check("oracle signed exactly once", len(_calls["sign"]), 1)
    check("signed outcome A", _calls["sign"][0]["outcome"], "A")
    check("v3 settle called once", len(_calls["settle"]), 1)
    check("settle got A/B escrows with sides",
          [(e["side"], e["redeemHex"]) for e in _calls["settle"][0]["escrows"]], [("A", "aa"), ("B", "bb")])
    check("state is broadcast", p["state"], "broadcast")
    check("txid returned", p["txid"], "txid-v3")
    check("tagged v3", p["escrowVersion"], "v3")
    check("nothing to sign", p["mySignatureInputs"], [])
    check("auto-settled flag", p["autoSettled"], True)
    check("winner sees they won", p["youWon"], True)
    check("payout is pot minus 2 input fees", p["payoutSompi"], str(STAKE * 2 - 2 * FEE))
    check("verdict stored+published", p["verdict"], {"outcome": "A", "sigA": "sigA-A", "sigB": "sigB-A"})

    print("a second poll does NOT settle again (idempotent)")
    before = len(_calls["settle"])
    q = await settlement.prepare(mid, b["address"])
    check("no second settle", len(_calls["settle"]), before)
    check("loser sees it paid", q["state"], "broadcast")
    check("loser's payout is zero", q["payoutSompi"], "0")

    print("a v3 DRAW pays each depositor their own stake back")
    stub_sidecar()
    mid, a, b = new_v3_match(winner=None)
    pa = await settlement.prepare(mid, a["address"])
    check("signed outcome draw", _calls["sign"][0]["outcome"], "draw")
    check("A gets stake back minus one fee", pa["payoutSompi"], str(STAKE - FEE))
    pb = await settlement.prepare(mid, b["address"])
    check("B gets stake back minus one fee", pb["payoutSompi"], str(STAKE - FEE))

    print("submit on a v3 match is a harmless self-settle")
    stub_sidecar()
    mid, a, b = new_v3_match(winner="b")
    r = await settlement.submit(mid, b["address"], "IGNORED-NO-SIG")
    check("submit settled it", r["state"], "broadcast")
    check("submit signed outcome B", _calls["sign"][0]["outcome"], "B")

    print("a gas-only v3 pot is refused before any signing")
    stub_sidecar()
    mid, a, b = new_v3_match(winner="a", stake=config.GAS_ONLY_STAKE_SOMPI)
    msg = await err(settlement.prepare(mid, a["address"]))
    check("explains nothing to claim", "smaller than the Kaspa network fee" in msg, True)
    check("oracle never signed", len(_calls["sign"]), 0)

    print("a stranger can't settle a v3 match")
    stub_sidecar()
    mid, a, b = new_v3_match(winner="a")
    outsider = db.get_or_create_account(f"kaspa:evil{time.time_ns()}", "pubX")
    check("prepare refused", await err(settlement.prepare(mid, outsider["address"])),
          "you're not a player in this match")

    print("a settle that races a landed txid is treated as success")
    stub_sidecar(settle_error="already spent")
    mid, a, b = new_v3_match(winner="a")
    db.mark_v2_settled(mid, "txid-other-tab", json.dumps({"outcome": "A"}))
    r = await settlement.prepare(mid, a["address"])
    check("reports the winning txid", r["txid"], "txid-other-tab")
    check("never tried to settle", len(_calls["settle"]), 0)

    print("a genuine v3 settle failure IS surfaced")
    stub_sidecar(settle_error="node unreachable")
    mid, a, b = new_v3_match(winner="a")
    check("error reaches the player", await err(settlement.prepare(mid, a["address"])), "node unreachable")
    check("no txid recorded", db.get_match(mid)["settle_txid"], None)

    print("a TIMEOUT forfeit reports the challenge window WITHOUT re-signing (clocks already claimed)")
    stub_sidecar()
    mid, a, b = new_v3_match(winner="a", result="timeout")
    # clocks.py has already spent the escrow into the pending covenant on-chain
    with db._lock, db._conn() as c:
        c.execute("UPDATE matches SET forfeit_claim_txid='claimtx', forfeit_claim_daa=530000000, "
                  "forfeit_pending_address='kaspa:pending' WHERE id=?", (mid,))
    p = await settlement.prepare(mid, a["address"])
    check("no oracle signature for a forfeit", len(_calls["sign"]), 0)
    check("no oracle settle for a forfeit", len(_calls["settle"]), 0)
    check("state is the forfeit window", p["state"], "forfeit_window")
    check("forfeit block surfaced", p["forfeit"]["claimTxid"], "claimtx")
    check("window not yet finalised", p["forfeit"]["finalised"], False)
    check("winner still sees youWon", p["youWon"], True)
    # ...and once finalise lands, it reads as paid
    db.mark_v2_settled(mid, "finaltx", json.dumps({"outcome": "A"}))
    p2 = await settlement.prepare(mid, a["address"])
    check("finalised forfeit reads broadcast", p2["state"], "broadcast")
    check("forfeit marked finalised", p2["forfeit"]["finalised"], True)
    check("still no oracle involvement", (len(_calls["sign"]), len(_calls["settle"])), (0, 0))

    # ── sweep: outcome × stake × viewpoint ──
    print("SWEEP: outcome × stake × viewpoint")
    for kas in [1, 5, 137, 1_000_000]:
        stake = kas * config.SOMPI_PER_KAS
        pot = 2 * stake
        for winner in ("a", "b", None):
            stub_sidecar(settle_txid=f"tx-{kas}-{winner}")
            mid, a, b = new_v3_match(winner=winner, stake=stake)
            outcome = {"a": "A", "b": "B", None: "draw"}[winner]
            pa = await settlement.prepare(mid, a["address"])
            pb = await settlement.prepare(mid, b["address"])
            tag = f"{kas}KAS/{outcome}"
            check(f"[{tag}] signed right outcome", _calls["sign"][0]["outcome"], outcome)
            check(f"[{tag}] settled once (idempotent both views)", len(_calls["settle"]), 1)
            check(f"[{tag}] tagged v3", pa["escrowVersion"], "v3")
            if outcome == "draw":
                check(f"[{tag}] A refunded stake-fee", pa["payoutSompi"], str(stake - FEE))
                check(f"[{tag}] B refunded stake-fee", pb["payoutSompi"], str(stake - FEE))
            else:
                pw, pl = (pa, pb) if outcome == "A" else (pb, pa)
                check(f"[{tag}] winner paid pot-2fee", pw["payoutSompi"], str(pot - 2 * FEE))
                check(f"[{tag}] loser paid nothing", pl["payoutSompi"], "0")

    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        return 1
    print("all v3 settlement checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
