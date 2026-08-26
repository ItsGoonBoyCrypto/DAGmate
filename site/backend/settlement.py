"""DAGmate site backend — settlement (docs/DAGMATE_SPEC.md §2.3).

Turning a finished game into money in the winner's wallet. The chain work all
lives in service/escrow.js; this module is the orchestration around it, and
the orchestration is where the money bugs would be, so the reasoning is
written down rather than implied.

Four things drive the whole design:

1. **The tx is built ONCE and then reused.** A signature is only valid for the
   exact transaction it signed. Rebuilding picks different UTXOs and a
   different fee, so a rebuild silently invalidates every signature already
   collected. In a draw the two players sign at different times — possibly
   days apart — so a rebuild between them would mean the first player's
   approval quietly stopped counting. `db.save_settlement_build` is guarded on
   `settle_tx_json IS NULL` so the first build wins and everyone else reads it
   back.

2. **A decisive result needs one signer; a draw needs two.** The escrow is
   2-of-3 (playerA, playerB, arbiter) and we hold the arbiter key, so one
   player signature per input is enough. For a win, the winner signs every
   input. For a draw, each escrow releases to its own depositor, so each
   escrow's inputs are signed by THAT depositor — which is why settlement is a
   multi-visit process and not a single request.

3. **The payout address comes from the database, never the request.** The
   arbiter co-signs whatever we hand it. If the caller could name the payout
   address, "settle my match" would be "send the pot anywhere I like".

4. **`escrows` means two different shapes on the two sidecar calls.**
   `buildSettleUnsigned` takes the deduped 1–2 escrows; `broadcastSettle`
   indexes `escrows[i]` BY INPUT INDEX. An escrow holding two UTXOs is two
   inputs, so the second list is longer. Expanding one into the other is
   `_escrows_per_input` below and is the single easiest thing here to get
   wrong.
"""
from __future__ import annotations

import json
import logging

import config
import database as db
import service_client

log = logging.getLogger("dagmate.settlement")


class SettlementError(RuntimeError):
    """Something about this match makes settling impossible or unsafe. The
    message is shown to the player, so it says what they can do about it."""


def _players(m: dict) -> tuple[dict, dict]:
    a = db.get_account(m["player_a_account_id"])
    b = db.get_account(m["player_b_account_id"])
    if not a or not b:
        raise SettlementError("this match is missing a player account")
    return a, b


def _escrow_list(m: dict, a: dict, b: dict) -> list[dict]:
    """The 1–2 escrows for `buildSettleUnsigned`. Order matters: on a draw the
    sidecar pays `escrows[0].depositorAddr` and `escrows[1].depositorAddr`, so
    A must be first and B second or the two players get each other's refund."""
    if not m["escrow_a_address"] or not m["escrow_b_address"]:
        raise SettlementError("this match has no escrow addresses — it was created "
                              "while the Kaspa service was unreachable")
    return [
        {"address": m["escrow_a_address"], "redeemHex": m["escrow_a_redeem_hex"], "depositorAddr": a["address"]},
        {"address": m["escrow_b_address"], "redeemHex": m["escrow_b_redeem_hex"], "depositorAddr": b["address"]},
    ]


def _escrows_per_input(m: dict, inputs: list[dict]) -> list[dict]:
    """Expand the escrow list to one entry PER INPUT, which is the shape
    `broadcastSettle` wants (see module docstring, point 4). Each input carries
    the address of the escrow it spends, so this is a lookup, not an
    assumption — two inputs from the same escrow both get that escrow's redeem
    script, which is exactly right."""
    redeem_by_addr = {
        m["escrow_a_address"]: m["escrow_a_redeem_hex"],
        m["escrow_b_address"]: m["escrow_b_redeem_hex"],
    }
    out = []
    for inp in inputs:
        redeem = redeem_by_addr.get(inp["address"])
        if not redeem:
            # The sidecar returned an input spending an address we don't know.
            # Never guess a redeem script — a wrong one produces an unspendable
            # sigScript at best.
            raise SettlementError(f"input {inp['index']} spends an unknown escrow")
        out.append({"address": inp["address"], "redeemHex": redeem})
    return out


def _signer_for(m: dict, escrow_address: str, *, a: dict, b: dict, winner_id: str | None) -> str:
    """Whose wallet must sign an input spending this escrow.

    Decisive: the winner signs everything — their signature plus ours meets the
    2-of-3 and nobody has to wait on the player who just lost, who has no
    incentive to help. Draw: each escrow is released by its own depositor,
    which is what makes a draw need both players."""
    if winner_id:
        return (a if winner_id == a["id"] else b)["address"]
    return (a if escrow_address == m["escrow_a_address"] else b)["address"]


async def prepare(match_id: str, address: str) -> dict:
    """Build (or read back) the settle tx and report what still needs signing.

    Safe to call repeatedly and from both players at once — the build is
    guarded, and a caller who loses that race simply reads the winning build.
    """
    m = db.get_match(match_id)
    if not m:
        raise SettlementError("match not found")
    if m["status"] != "settled":
        raise SettlementError("this match hasn't finished yet")
    a, b = _players(m)
    if address not in (a["address"], b["address"]):
        raise SettlementError("you're not a player in this match")
    if m["settle_txid"]:
        return _public(m, address, a, b)

    if not m["settle_tx_json"]:
        pot_estimate = (m["funded_a_sompi"] or 0) + (m["funded_b_sompi"] or 0)
        if pot_estimate and pot_estimate < config.SETTLE_MIN_POT_SOMPI:
            # Gas-only matches live here by design: a 1000-sompi stake against a
            # 60,000,000-sompi-per-input fee can never be released profitably.
            # Say so plainly instead of letting the sidecar fail with a raw
            # "pot too small to cover rake + fee" after a wallet popup.
            raise SettlementError(
                "this pot is smaller than the Kaspa network fee needed to release it, "
                "so there is nothing to claim — the match still stands as an on-chain record")
        winner_id = m["winner_account_id"]
        winner_addr = None
        if winner_id:
            # From the DB, never the request body (module docstring, point 3).
            winner_addr = (a if winner_id == a["id"] else b)["address"]
        built = await service_client.settle_unsigned(
            match_id=m["hd_index"],  # the sidecar derives the arbiter key from this,
                                     # so it must be the HD index, not the UUID
            escrows=_escrow_list(m, a, b),
            winner_addr=winner_addr, split=winner_id is None,
            rake_sompi=_rake_sompi(m))
        inputs = [{"index": i["index"], "address": i["address"],
                   "signer": _signer_for(m, i["address"], a=a, b=b, winner_id=winner_id)}
                  for i in built["inputs"]]
        if not db.save_settlement_build(
                match_id, tx_json=built["txJson"], inputs=inputs, sigs_arb=built["sigsArb"],
                pot_sompi=int(built["potSompi"]), rake_sompi=int(built["rakeSompi"])):
            log.info(f"settle build for {match_id} lost the race — using the stored one")
        m = db.get_match(match_id)

    # A fully-signed tx that never went out strands the pot: once the last
    # signature is stored, every player sees "nothing to sign" and no click
    # path reaches the broadcast. Whoever opens the panel next finishes the
    # release — it needs no signature from them, only the stored set — so a
    # failed broadcast self-heals on the next poll instead of deadlocking.
    if _fully_signed(m):
        return await _broadcast(match_id, m, address, a, b)
    return _public(m, address, a, b)


def _rake_sompi(m: dict) -> int:
    """Zero, unless someone deliberately turns the rake on. See config.RAKE_BPS:
    the platform takes no cut of a pot. Derived from the same constant the UI
    quotes so the promise and the arithmetic can't drift apart."""
    if config.RAKE_BPS <= 0:
        return 0
    return (m["stake_sompi"] * 2 * config.RAKE_BPS) // 10_000


def _fully_signed(m: dict) -> bool:
    """A settle tx that is built, has every player signature, and has not yet
    been broadcast — i.e. ready to release with no further wallet interaction."""
    if not m["settle_tx_json"] or m["settle_txid"]:
        return False
    stored = json.loads(m["settle_sigs_player_json"])
    return bool(stored) and all(s is not None for s in stored)


async def _broadcast(match_id: str, m: dict, address: str, a: dict, b: dict) -> dict:
    """Push a fully-signed settle tx and record its txid.

    Called from two places — submit (the player who just added the last
    signature) and prepare (a player opening a match whose set is already
    complete but never went out). Both are safe: the broadcast guard keeps the
    first txid, and a double-spend rejection means the other side released
    first, which is a success, not an error — but only if a txid really landed,
    so it checks rather than assumes."""
    inputs = json.loads(m["settle_inputs_json"])
    stored = json.loads(m["settle_sigs_player_json"])
    try:
        r = await service_client.settle_broadcast(
            tx_json=m["settle_tx_json"],
            escrows=_escrows_per_input(m, inputs),
            sigs_player=stored, sigs_arb=json.loads(m["settle_sigs_arb_json"]))
    except service_client.ServiceError as e:
        current = db.get_match(match_id)
        if current["settle_txid"]:
            return _public(current, address, a, b)
        raise SettlementError(str(e))
    if not db.mark_settlement_broadcast(match_id, r["txid"]):
        log.warning(f"settle for {match_id} broadcast twice — keeping the first txid")
    return _public(db.get_match(match_id), address, a, b)


async def submit(match_id: str, address: str, signed_tx_json: str) -> dict:
    """Take the wallet-signed tx, keep this player's signatures, and — once the
    set is complete — broadcast.

    The player's wallet (Kasware signPskt) returns the WHOLE tx with their
    signature embedded in each of their inputs. We only ever pull sigs from the
    inputs the stored signer map says are THIS player's — so a draw's
    loser-of-the-coin-flip can't slip a signature into the other player's escrow
    slot. The extracted raw sigs then go through the same broadcastSettle
    assembly the arbiter sigs already do."""
    m = db.get_match(match_id)
    if not m:
        raise SettlementError("match not found")
    a, b = _players(m)
    if address not in (a["address"], b["address"]):
        raise SettlementError("you're not a player in this match")
    if not m["settle_tx_json"]:
        raise SettlementError("nothing prepared to sign — reload and try again")
    if m["settle_txid"]:
        return _public(m, address, a, b)  # already broadcast; a duplicate click

    inputs = json.loads(m["settle_inputs_json"])
    stored = json.loads(m["settle_sigs_player_json"])
    # Only the inputs that are this player's to sign AND not yet filled.
    my_indexes = [i["index"] for i in inputs
                  if i["signer"] == address and stored[i["index"]] is None]
    if my_indexes:
        got = await service_client.extract_sigs(signed_tx_json=signed_tx_json, indexes=my_indexes)
        extracted = got.get("sigs", {})
        for i in my_indexes:
            sig = extracted.get(str(i))
            if not sig:
                raise SettlementError(f"your wallet didn't sign input {i} — try again")
            stored[i] = sig
        if not db.save_settlement_sigs(match_id, stored):
            # The guard only fails if a txid landed while we were signing, i.e.
            # the other player completed the set first. Their broadcast covers us.
            return _public(db.get_match(match_id), address, a, b)

    # Re-read so the broadcast works off the just-saved signature set (and picks
    # up a txid if the other player's submit landed in between).
    m = db.get_match(match_id)
    # Whether or not this call added a signature, if the set is now complete but
    # a previous broadcast attempt failed (e.g. a transient sidecar error), a
    # re-submit gets us here and retries the broadcast rather than sitting on a
    # fully-signed tx that never went out.
    if not _fully_signed(m):
        return _public(m, address, a, b)
    return await _broadcast(match_id, m, address, a, b)


def _public(m: dict, address: str, a: dict, b: dict) -> dict:
    """What the browser needs to drive the claim panel, and nothing more.

    `mine` is the point of this payload: the wallet has to be asked for a
    signature per input, and a player should never be asked for one that isn't
    theirs."""
    if not m["settle_tx_json"]:
        return {"state": "unprepared"}
    inputs = json.loads(m["settle_inputs_json"])
    stored = json.loads(m["settle_sigs_player_json"])
    mine = [i["index"] for i in inputs
            if i["signer"] == address and stored[i["index"]] is None]
    pot = m["settle_pot_sompi"] or 0
    rake = m["settle_rake_sompi"] or 0
    fee = config.SETTLE_FEE_SOMPI_PER_INPUT * len(inputs)
    distributable = max(0, pot - rake - fee)
    is_draw = m["winner_account_id"] is None
    # On a draw the sidecar splits the odd sompi to A (halfA = distributable −
    # distributable//2, halfB = distributable//2), so the exact amount this
    # caller receives depends on which side they are. Show THAT number, not a
    # rounded half, so the display matches the on-chain output to the sompi.
    if is_draw:
        half_b = distributable // 2
        half_a = distributable - half_b
        payout = half_a if a["address"] == address else half_b
    else:
        payout = distributable
    return {
        "state": "broadcast" if m["settle_txid"] else ("ready" if not mine else "needs_signature"),
        "txid": m["settle_txid"],
        "txJson": m["settle_tx_json"] if mine else None,
        "mySignatureInputs": mine,
        "waitingOnOpponent": bool(not mine and any(s is None for s in stored)),
        "potKas": pot / config.SOMPI_PER_KAS,
        "networkFeeKas": fee / config.SOMPI_PER_KAS,
        "platformFeeKas": rake / config.SOMPI_PER_KAS,
        # What actually lands in a wallet, so the claim button can state the
        # number rather than the player discovering it after signing.
        "payoutKas": payout / config.SOMPI_PER_KAS,
        # Exact integer sompi, as decimal strings (never JS floats — a KAS float
        # divided by 2 on a draw prints 4.999999999999999). The client formats
        # from these, and verifies the tx it's about to sign against them: the
        # sum of every output in txJson MUST equal expectedOutputSompi (= pot −
        # miner fee), so a substituted tx that pays a different total or routes
        # money elsewhere can't be signed with a correct-looking amount on screen.
        "potSompi": str(pot),
        "networkFeeSompi": str(fee),
        "platformFeeSompi": str(rake),
        "payoutSompi": str(payout),
        "expectedOutputSompi": str(max(0, pot - fee)),
        "isDraw": is_draw,
        "youWon": m["winner_account_id"] is not None
                  and (a if m["winner_account_id"] == a["id"] else b)["address"] == address,
    }
