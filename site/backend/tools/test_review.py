"""Tests for the fair-play review workflow (integrity Phase 2, Part A). Run: python tools/test_review.py

Real schema + accessors + the real settlement.admin_resolve; sidecar stubbed (same discipline as
test_settlement_v3). Proves the DORMANT-by-default hold gate, and the owner decisions:
  - a HELD match won't settle through prepare() — the pot stays escrowed, nothing to sign;
  - CLEAR releases the pot to the real winner and stamps 'cleared';
  - CONFIRM releases the pot to the VICTIM (oracle outcome overridden), bans the cheat, stamps 'confirmed';
  - a held DRAW can't be confirmed (no victim);
  - a hold is never placed once the pot is already paid.
"""
from __future__ import annotations
import asyncio, json, os, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DAGMATE_SITE_DB"] = os.path.join(tempfile.mkdtemp(prefix="dagmate-rev-"), "t.db")

import config  # noqa: E402
config.DB_PATH = os.environ["DAGMATE_SITE_DB"]
assert config.DB_PATH.startswith(tempfile.gettempdir()), f"DB not isolated: {config.DB_PATH}"

import database as db  # noqa: E402
import settlement  # noqa: E402

STAKE = 10 * config.SOMPI_PER_KAS
FEE = config.SETTLE_V3_FEE_SOMPI_PER_INPUT
_fail = []
_calls = {}
_SENTINEL = object()

def ck(name, got, want=_SENTINEL):
    # want given -> equality (so [], None, False compare correctly); want omitted -> truthiness.
    ok = bool(got) if want is _SENTINEL else got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f" - got {got!r} want {want!r}"))
    if not ok: _fail.append(name)

def stub_sidecar():
    _calls.clear(); _calls["sign"] = []; _calls["settle"] = []
    async def _sign(*, match_id, outcome):
        _calls["sign"].append(outcome)
        return {"outcome": outcome, "sigA": "sa", "sigB": "sb"}
    async def _settle(*, escrows, outcome, pk_a, pk_b, sig_a, sig_b):
        _calls["settle"].append(outcome)
        return {"txid": f"tx-{outcome}", "potSompi": str(STAKE * 2), "feeSompi": str(FEE * 2), "outcome": outcome}
    settlement.service_client.oracle_sign_result_v3 = _sign
    settlement.service_client.settle_v3 = _settle

def new_held_match(*, winner="a", score=0.92):
    a = db.get_or_create_account(f"kaspa:pA{time.time_ns()}", "pubA")
    b = db.get_or_create_account(f"kaspa:pB{time.time_ns()}", "pubB")
    m = db.create_match(challenge_id=None, tournament_id=None, round_no=None,
                        player_a_account_id=a["id"], player_b_account_id=b["id"],
                        stake_sompi=STAKE, mode="rapid", fen="startpos",
                        escrow_a={"address": "kaspa:eA", "redeemHex": "aa"},
                        escrow_b={"address": "kaspa:eB", "redeemHex": "bb"}, reclaim_daa=1)
    db.set_match_escrows(m["id"], {"address": "kaspa:eA", "redeemHex": "aa"},
                         {"address": "kaspa:eB", "redeemHex": "bb"}, version="v3",
                         sess_pk_a="a" * 64, sess_pk_b="b" * 64, w_daa=72000)
    wid = {"a": a["id"], "b": b["id"], None: None}[winner]
    res = "draw" if winner is None else "checkmate"
    with db._lock, db._conn() as c:
        c.execute("UPDATE matches SET status='settled', result=?, winner_account_id=?, "
                  "funded_a_sompi=?, funded_b_sompi=? WHERE id=?", (res, wid, STAKE, STAKE, m["id"]))
    held = db.record_analysis(m["id"], score, json.dumps({"engineMatch": 0.97}), hold=True)
    return m["id"], a, b, held

async def err_of(coro):
    try: await coro; return "<no error>"
    except settlement.SettlementError as e: return str(e)

async def main():
    db.ensure_schema()

    print("recording a HIGH-score analysis holds the match; a held match won't settle through prepare()")
    stub_sidecar()
    mid, a, b, held = new_held_match(winner="a")
    ck("hold placed", held, held)
    ck("review_status is held", db.get_match(mid)["review_status"], "held")
    p = await settlement.prepare(mid, a["address"])
    ck("prepare returns held_review", p["state"], "held_review")
    ck("no oracle sign while held", _calls["sign"], [])
    ck("pot untouched (no settle_txid)", db.get_match(mid)["settle_txid"], None)

    print("CLEAR releases the pot to the real winner and stamps cleared")
    stub_sidecar()
    r = await settlement.admin_resolve(mid, "clear", "reviewed, clean game")
    ck("decision cleared", r["decision"], "cleared")
    ck("settled to the real winner A", _calls["settle"], ["A"])
    ck("review_status cleared", db.get_match(mid)["review_status"], "cleared")
    ck("winner not banned", db.is_banned(a["id"]), False)

    print("CONFIRM forfeits the pot to the VICTIM and bans the cheat")
    stub_sidecar()
    mid2, a2, b2, _ = new_held_match(winner="a")   # A is the flagged 'winner' => B is the victim
    r2 = await settlement.admin_resolve(mid2, "confirm", "engine match 97%, only-move finds")
    ck("decision confirmed", r2["decision"], "confirmed")
    ck("oracle outcome overridden to the victim B", _calls["settle"], ["B"])
    ck("cheat A is banned", db.is_banned(a2["id"]), True)
    ck("victim B is NOT banned", db.is_banned(b2["id"]), False)
    ck("review_status confirmed", db.get_match(mid2)["review_status"], "confirmed")
    ck("cheat/victim ids reported", (r2["cheatAccountId"], r2["victimAccountId"]), (a2["id"], b2["id"]))
    ck("cheat's rating was penalised (below the 1500 seed)", db.get_account(a2["id"])["rating"] < 1500)
    ck("victim's rating was credited (above the seed)", db.get_account(b2["id"])["rating"] > 1500)

    print("a held DRAW can't be confirmed (no victim to award)")
    stub_sidecar()
    mid3, a3, b3, _ = new_held_match(winner=None)
    ck("confirm on a draw errors", (await err_of(settlement.admin_resolve(mid3, "confirm", "x"))).startswith("a drawn game"))
    ck("but a draw can be cleared", (await settlement.admin_resolve(mid3, "clear", "ok"))["decision"], "cleared")

    print("guards")
    ck("resolving a non-held match errors",
       (await err_of(settlement.admin_resolve(mid, "clear", "again"))).startswith("this match isn't awaiting"))
    # a hold is never placed once the pot is already paid
    mid4, a4, b4, _ = new_held_match(winner="a")
    with db._lock, db._conn() as c:
        c.execute("UPDATE matches SET settle_txid='already-paid', review_status=NULL WHERE id=?", (mid4,))
    ck("no hold on an already-paid pot", db.record_analysis(mid4, 0.99, "{}", hold=True), False)
    ck("a clean account isn't banned", db.is_banned(b["id"]), False)

    print()
    print(f"{len(_fail)} FAILED: {_fail}" if _fail else "all review checks passed")
    return 1 if _fail else 0

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
