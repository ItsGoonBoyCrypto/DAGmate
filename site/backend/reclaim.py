"""DAGmate site backend — timelock reclaim (docs/DAGMATE_SPEC.md §2.4).

The escape hatch. Every escrow has two branches: a 2-of-3 that settles the
match, and `<reclaimDaa> CHECKLOCKTIMEVERIFY <pkDepositor> CHECKSIG` that hands
a depositor their own stake back after 14 days. Settlement is the happy path;
this module is what happens when there is no happy path — one player funds and
the other never does, or a match dies before it can be released.

Without it the non-custodial promise is only half true. The reclaim branch
existed in the script from day one, but nothing could build a transaction that
spent it, so "your stake is recoverable" meant "recoverable by someone who can
write Kaspa covenant code". Now it means "click the button".

Four properties this file exists to hold:

1. **DAGmate cannot perform a reclaim.** The branch is single-sig on the
   DEPOSITOR's key, which we do not hold. We build an unsigned tx, their
   wallet signs it, we relay it. If this service disappears the player still
   has `redeemHex` and can do the same thing without us — which is why the
   redeem script is published in the match view rather than kept here.

2. **Reclaim is never offered while the pot can still be won.** After the
   timelock the SCRIPT permits a loser to walk their own stake back, and no
   amount of backend policy changes that. What the backend controls is whether
   DAGmate helps: it does not build that transaction for a match a winner can
   still claim. `_never_settles` is the whole of that rule.

3. **Which escrow you may drain comes from your session, not the request.**
   Player A reclaims escrow A. There is no parameter for it. The two escrows
   have different redeem scripts bound to different pubkeys, so naming the
   wrong one produces an unspendable tx rather than a theft — but the point is
   that it should not be nameable at all.

4. **No build-once dance.** Settlement stores its tx because two people sign
   it days apart and a rebuild would orphan the first signature. A reclaim has
   exactly one signer in one sitting, so a rebuild costs nothing and stale
   state is the bigger risk. Only the resulting txid is kept, as a receipt.
"""
from __future__ import annotations

import logging

import config
import database as db
import service_client

log = logging.getLogger("dagmate.reclaim")


class ReclaimError(RuntimeError):
    """This stake isn't reclaimable (yet, or by you). Shown to the player, so
    the message says which of those it is."""


def _never_settles(m: dict) -> bool:
    """Can this match still pay someone out through the 2-of-3 branch?

    `expired` — the funding window closed with only one side paid, so there is
    no game and no winner; whatever landed goes back.

    `awaiting_deposit` — reachable only if the deposit watcher has been down
    since before the timelock opened, since the funding deadline (an hour) is
    two weeks short of it. Still definitionally never went live.

    `settled` with no txid and a pot under the settle fee — the gas-only case.
    The 2-of-3 branch is open but useless: releasing the pot costs more than
    the pot. Reclaim is the only way those coins ever move, so it's offered
    even though a winner nominally exists.

    Everything else — a live game, or a settled match whose pot is still
    claimable — is a no. A slow winner has not forfeited anything.
    """
    if m["status"] in ("expired", "awaiting_deposit"):
        return True
    if m["status"] == "settled" and not m["settle_txid"]:
        pot = (m["funded_a_sompi"] or 0) + (m["funded_b_sompi"] or 0)
        return pot < config.SETTLE_MIN_POT_SOMPI
    return False


def summary(m: dict) -> dict:
    """Caller-independent reclaim facts for the match view. Deliberately says
    nothing about who is asking — this rides along on a public endpoint."""
    return {
        "eligible": _never_settles(m) and bool(m["escrow_a_address"]),
        "reclaimDaa": m["reclaim_daa"],
        "aTxid": m["reclaim_a_txid"],
        "bTxid": m["reclaim_b_txid"],
    }


def _side_for(m: dict, address: str) -> str:
    a = db.get_account(m["player_a_account_id"])
    b = db.get_account(m["player_b_account_id"])
    if a and a["address"] == address:
        return "a"
    if b and b["address"] == address:
        return "b"
    raise ReclaimError("you're not a player in this match")


def _check(match_id: str, address: str) -> tuple[dict, str]:
    m = db.get_match(match_id)
    if not m:
        raise ReclaimError("match not found")
    side = _side_for(m, address)
    if not _never_settles(m):
        if m["status"] == "live":
            raise ReclaimError("this match is still being played")
        raise ReclaimError("this pot can still be released to its winner, so it isn't "
                           "reclaimable — open the payout panel instead")
    if not m["escrow_a_address"] or m["reclaim_daa"] is None:
        raise ReclaimError("this match has no escrow on chain — nothing was ever deposited")
    if m[f"reclaim_{side}_txid"]:
        raise ReclaimError("you've already reclaimed this stake")
    # Fast path on what the deposit watcher recorded, so a player who never
    # funded doesn't sit through an RPC round-trip to be told nothing is there.
    # It is NOT the authority — the sidecar reads the real UTXO set — but a
    # side that was never seen funded has nothing to build against.
    if not (m[f"funded_{side}_sompi"] or 0):
        raise ReclaimError("no deposit from you was ever seen at this escrow, so there's "
                           "nothing here to reclaim")
    return m, side


async def prepare(match_id: str, address: str) -> dict:
    """Build the unsigned reclaim tx for this player's own escrow.

    The sidecar refuses if the timelock hasn't opened or the escrow is empty,
    which is a live read of the chain rather than a read of our own bookkeeping
    — the two can disagree (a manual spend, a watcher outage) and the chain
    wins."""
    m, side = _check(match_id, address)
    try:
        built = await service_client.reclaim_unsigned(
            address=m[f"escrow_{side}_address"],
            # From the account, never the body: this is the address the stake
            # lands at, and taking it from the request would make "reclaim my
            # stake" mean "send this escrow anywhere I like" for anyone who
            # could reach it.
            depositor_addr=address,
            reclaim_daa=m["reclaim_daa"])
    except service_client.ServiceError as e:
        # Surfaced as a player-facing refusal rather than a service outage.
        # Every way this build fails — timelock not open yet, escrow already
        # empty, dust below the fee — is a fact about their stake that they
        # should read plainly, not a 503.
        raise ReclaimError(str(e))
    return {
        "state": "needs_signature",
        "side": side,
        "txJson": built["txJson"],
        "mySignatureInputs": [i["index"] for i in built["inputs"]],
        "escrowAddress": m[f"escrow_{side}_address"],
        "redeemHex": m[f"escrow_{side}_redeem_hex"],
        "reclaimDaa": int(built["reclaimDaa"]),
        "totalKas": int(built["totalSompi"]) / config.SOMPI_PER_KAS,
        "networkFeeKas": int(built["feeSompi"]) / config.SOMPI_PER_KAS,
        "payoutKas": int(built["payoutSompi"]) / config.SOMPI_PER_KAS,
        "txid": None,
    }


async def submit(match_id: str, address: str, tx_json: str, sigs: list[str]) -> dict:
    """Relay the player's signed reclaim to the network.

    `tx_json` comes back from the browser because nothing was stored — and it
    cannot be a lever: it is the wallet's own signed tx, the signatures only
    validate against the exact tx they signed, and the redeem script is ours
    (from the DB), bound to that escrow's script hash. A substituted tx spends
    UTXOs the signature doesn't cover and dies at the node."""
    m, side = _check(match_id, address)
    if not tx_json:
        raise ReclaimError("nothing to broadcast — reload and try again")
    if not sigs or any(not s for s in sigs):
        raise ReclaimError("your wallet didn't return a signature for every input")

    try:
        r = await service_client.reclaim_broadcast(
            tx_json=tx_json,
            # The redeem script is read from OUR record of the escrow, not from
            # the request. It is the only thing here that decides which script
            # is being satisfied.
            redeem_hex=m[f"escrow_{side}_redeem_hex"],
            sigs=sigs)
    except service_client.ServiceError as e:
        raise ReclaimError(str(e))

    if not db.mark_reclaim_broadcast(match_id, side, r["txid"]):
        log.warning(f"reclaim for match {match_id} side {side} broadcast twice — "
                    f"keeping the first txid")
    m = db.get_match(match_id)
    return {"state": "broadcast", "side": side, "txid": m[f"reclaim_{side}_txid"],
            "mySignatureInputs": [], "txJson": None}
