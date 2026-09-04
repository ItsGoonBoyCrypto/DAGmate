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

   ROADMAP #1 (config.SETTLE_MUTUAL_ENABLED) adds a second, preferred way to
   settle a WIN: the loser ALSO co-signs, and the pot releases on the
   {playerA, playerB} 2-subset with NO arbiter — so an honestly-completed game
   never trusts DAGmate's key. The winner's signature is required either way
   (it is one of the two sigs in both the mutual and the arbiter assembly). The
   arbiter is only used as a stall-breaker: if the loser has not co-signed
   within config.SETTLE_STALL_SECS of the build, the winner's claim falls back
   to winner+arbiter. Two signature arrays back this: settle_sigs_player_json
   (the "primary" signer — winner for a win, each depositor for a draw) and
   settle_sigs_cosign_json (the loser's mutual co-signatures). Draws are
   untouched by #1.

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
import time

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
    # Free game (0 stake): no escrow, no pot, nothing to settle — just report the result.
    if m["stake_sompi"] == 0:
        return _public_free(m, address, a, b)
    # Roadmap #2: a v2 (covenant) match settles itself — DAGmate signs the result and the
    # escrow pays the winner (or, on a draw, each depositor). No player signature, no co-sign
    # round-trip, so the whole v1 build/sign machinery below is skipped.
    if (m["escrow_version"] or "v1") == "v2":
        return await _settle_v2(match_id, m, address, a, b)
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

    # A settle that is ready to release but never went out strands the pot: once
    # the last needed signature is stored (or the stall window lapses), a player
    # may see "nothing to sign" and no click path reaches the broadcast. Whoever
    # opens the panel next finishes the release — it needs no signature from
    # them, only the stored set — so a failed or deferred broadcast self-heals
    # on the next poll instead of deadlocking. The stall fallback also lands
    # here: once the window passes, the next prepare() flips to 'arb'.
    mode = _broadcast_mode(m, a, b)
    if mode:
        return await _broadcast(match_id, m, address, a, b, mode)
    return _public(m, address, a, b)


def _rake_sompi(m: dict) -> int:
    """Zero, unless someone deliberately turns the rake on. See config.RAKE_BPS:
    the platform takes no cut of a pot. Derived from the same constant the UI
    quotes so the promise and the arithmetic can't drift apart."""
    if config.RAKE_BPS <= 0:
        return 0
    return (m["stake_sompi"] * 2 * config.RAKE_BPS) // 10_000


def _cosign_list(m: dict, n: int) -> list:
    """The loser's mutual co-signature array, defaulting to all-unsigned. NULL
    on every pre-#1 row and on any settle that hasn't used the mutual path, and
    a length mismatch (e.g. a rebuild) is treated the same as absent — never
    trusted into a broadcast."""
    raw = m["settle_sigs_cosign_json"]
    if not raw:
        return [None] * n
    try:
        lst = json.loads(raw)
    except (TypeError, ValueError):
        return [None] * n
    return lst if isinstance(lst, list) and len(lst) == n else [None] * n


def _my_signing(m: dict, address: str, a: dict, b: dict) -> tuple[str, list[int]]:
    """(which, indexes) — which signature array THIS caller fills and the input
    indexes still needing their signature.

    'primary' is the existing 2-of-3 co-signer set: the winner (who signs every
    input) for a win, each depositor (who signs their own escrow) for a draw —
    the stored per-input `signer` map already encodes it. 'cosign' is the
    loser's optional agreement in a mutual win, which drops the arbiter from an
    honestly-completed game (roadmap #1); the loser signs every input.

    With mutual disabled this returns exactly the pre-#1 behaviour: the loser
    has nothing to sign."""
    inputs = json.loads(m["settle_inputs_json"])
    primary = json.loads(m["settle_sigs_player_json"])
    prim = [i["index"] for i in inputs if i["signer"] == address and primary[i["index"]] is None]
    if prim:
        return ("primary", prim)
    # Nothing left on the primary set for this caller. On a decisive win, the
    # loser can still co-sign to release without the arbiter.
    if m["winner_account_id"] is not None and config.SETTLE_MUTUAL_ENABLED:
        winner_addr = (a if m["winner_account_id"] == a["id"] else b)["address"]
        if address != winner_addr:
            cosign = _cosign_list(m, len(inputs))
            co = [i["index"] for i in inputs if cosign[i["index"]] is None]
            return ("cosign", co)
    return ("primary", [])


def _broadcast_mode(m: dict, a: dict, b: dict) -> str | None:
    """Whether a built settle is ready to go out, and how: 'mutual' (both
    players, no arbiter), 'arb' (one player + arbiter), or None (still waiting).

    Generalises the old `_fully_signed`: with mutual disabled it returns 'arb'
    exactly when every primary signature is in, which is precisely when the
    pre-#1 code broadcast. With mutual enabled and a win, it prefers the
    arbiter-free release once the loser has co-signed, and otherwise holds for
    the stall window before letting the arbiter break the tie."""
    if not m["settle_tx_json"] or m["settle_txid"]:
        return None
    primary = json.loads(m["settle_sigs_player_json"])
    primary_done = bool(primary) and all(s is not None for s in primary)
    if m["winner_account_id"] is None:
        return "arb" if primary_done else None  # draw: unchanged path
    if not primary_done:
        return None  # the winner's signature is required in either assembly
    if config.SETTLE_MUTUAL_ENABLED:
        cosign = _cosign_list(m, len(primary))
        if all(s is not None for s in cosign):
            return "mutual"  # both players agreed — DAGmate's key is never touched
        prepared = m["settle_prepared_ts"] or 0
        if time.time() < prepared + config.SETTLE_STALL_SECS:
            return None  # give the loser the stall window to co-sign first
    return "arb"  # stall-breaker (or mutual disabled): winner + arbiter


def _mutual_role_sigs(m: dict, a: dict, b: dict, inputs: list[dict],
                      primary: list) -> tuple[list, list]:
    """Map the stored (winner=primary, loser=cosign) signatures onto the fixed
    (playerA, playerB) roles the escrow's redeem expects, so the sidecar can
    push them in pubkey order. This side is the only one that knows which wallet
    is A and which is B, which is exactly why the mapping lives here and the
    assembler on the sidecar stays a dumb order-preserver."""
    n = len(inputs)
    cosign = _cosign_list(m, n)
    winner_is_a = m["winner_account_id"] == a["id"]
    sigs_a: list = [None] * n
    sigs_b: list = [None] * n
    for inp in inputs:
        i = inp["index"]
        win_sig, lose_sig = primary[i], cosign[i]
        if win_sig is None or lose_sig is None:
            # _broadcast_mode only returns 'mutual' with both sets complete;
            # a gap here would mean a concurrent rebuild, so refuse rather than
            # ship a half-signed input the node will reject anyway.
            raise SettlementError("mutual settle is missing a signature — reload and try again")
        if winner_is_a:
            sigs_a[i], sigs_b[i] = win_sig, lose_sig
        else:
            sigs_a[i], sigs_b[i] = lose_sig, win_sig
    return sigs_a, sigs_b


async def _broadcast(match_id: str, m: dict, address: str, a: dict, b: dict, mode: str) -> dict:
    """Push a ready settle tx and record its txid.

    Called from two places — submit (the player who just added the last
    signature) and prepare (a player opening a match whose set is already
    complete but never went out). Both are safe: the broadcast guard keeps the
    first txid, and a double-spend rejection means the other side released
    first, which is a success, not an error — but only if a txid really landed,
    so it checks rather than assumes.

    `mode` decides which 2-subset of the 2-of-3 signs: 'mutual' = both players
    (no arbiter, roadmap #1); 'arb' = one player + arbiter (the pre-#1 path,
    used for draws and as the stall-breaker). The 'arb' branch is byte-for-byte
    what it always was."""
    inputs = json.loads(m["settle_inputs_json"])
    stored = json.loads(m["settle_sigs_player_json"])
    try:
        if mode == "mutual":
            sigs_a, sigs_b = _mutual_role_sigs(m, a, b, inputs, stored)
            r = await service_client.settle_broadcast_mutual(
                tx_json=m["settle_tx_json"],
                escrows=_escrows_per_input(m, inputs),
                sigs_a=sigs_a, sigs_b=sigs_b)
        else:
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
    if m["stake_sompi"] == 0:  # free game — no signature, no settlement
        return _public_free(m, address, a, b)
    # A v2 match has nothing for a player to sign — it self-settles. A stray submit (a client
    # that still POSTs one) just triggers or confirms the auto-settle, idempotently.
    if (m["escrow_version"] or "v1") == "v2":
        return await _settle_v2(match_id, m, address, a, b)
    if not m["settle_tx_json"]:
        raise SettlementError("nothing prepared to sign — reload and try again")
    if m["settle_txid"]:
        return _public(m, address, a, b)  # already broadcast; a duplicate click

    inputs = json.loads(m["settle_inputs_json"])
    # Which array this player fills and which of its inputs they still owe:
    # 'primary' (winner or draw depositor) or 'cosign' (the loser's mutual
    # agreement). The loser can never write into the primary slots and the
    # winner is never asked to co-sign — the split is by role, not by request.
    which, my_indexes = _my_signing(m, address, a, b)
    if my_indexes:
        stored = (_cosign_list(m, len(inputs)) if which == "cosign"
                  else json.loads(m["settle_sigs_player_json"]))
        got = await service_client.extract_sigs(signed_tx_json=signed_tx_json, indexes=my_indexes)
        extracted = got.get("sigs", {})
        for i in my_indexes:
            sig = extracted.get(str(i))
            if not sig:
                raise SettlementError(f"your wallet didn't sign input {i} — try again")
            stored[i] = sig
        saved = (db.save_settlement_cosign_sigs(match_id, stored) if which == "cosign"
                 else db.save_settlement_sigs(match_id, stored))
        if not saved:
            # The guard only fails if a txid landed while we were signing, i.e.
            # the other player completed the set first. Their broadcast covers us.
            return _public(db.get_match(match_id), address, a, b)

    # Re-read so the broadcast works off the just-saved signature set (and picks
    # up a txid if the other player's submit landed in between).
    m = db.get_match(match_id)
    # Whether or not this call added a signature, if the settle is now ready but
    # a previous broadcast attempt failed (e.g. a transient sidecar error), a
    # re-submit gets us here and retries the broadcast rather than sitting on a
    # ready tx that never went out.
    mode = _broadcast_mode(m, a, b)
    if not mode:
        return _public(m, address, a, b)
    return await _broadcast(match_id, m, address, a, b, mode)


def _public(m: dict, address: str, a: dict, b: dict) -> dict:
    """What the browser needs to drive the claim panel, and nothing more.

    `mine` is the point of this payload: the wallet has to be asked for a
    signature per input, and a player should never be asked for one that isn't
    theirs."""
    if not m["settle_tx_json"]:
        return {"state": "unprepared"}
    inputs = json.loads(m["settle_inputs_json"])
    primary = json.loads(m["settle_sigs_player_json"])
    # `mine` comes from _my_signing so the loser's mutual co-sign inputs are
    # included (roadmap #1). `which` tells the client whether this ask is the
    # caller's OWN payout ('primary') or a co-sign that releases to the opponent
    # ('cosign'), which is a different button and a different sentence.
    which, mine = _my_signing(m, address, a, b)
    is_cosign_ask = which == "cosign" and bool(mine)
    pot = m["settle_pot_sompi"] or 0
    rake = m["settle_rake_sompi"] or 0
    fee = config.SETTLE_FEE_SOMPI_PER_INPUT * len(inputs)
    distributable = max(0, pot - rake - fee)
    is_draw = m["winner_account_id"] is None
    you_won = (m["winner_account_id"] is not None
               and (a if m["winner_account_id"] == a["id"] else b)["address"] == address)
    # On a draw the sidecar splits the odd sompi to A (halfA = distributable −
    # distributable//2, halfB = distributable//2), so the exact amount this
    # caller receives depends on which side they are. Show THAT number, not a
    # rounded half, so the display matches the on-chain output to the sompi.
    if is_draw:
        half_b = distributable // 2
        half_a = distributable - half_b
        payout = half_a if a["address"] == address else half_b
    elif you_won:
        payout = distributable
    else:
        payout = 0  # the loser gets nothing; their panel is a co-sign, not a claim

    # Mutual win in flight: the winner has signed but the loser hasn't co-signed
    # yet. Tell the winner the pot is auto-releasing so a short wait doesn't read
    # as a stuck button, and expose the seconds left so the panel can count down.
    awaiting_cosign = False
    auto_release_in = None
    if (not is_draw and config.SETTLE_MUTUAL_ENABLED and not m["settle_txid"]):
        primary_done = bool(primary) and all(s is not None for s in primary)
        cosign = _cosign_list(m, len(inputs))
        if primary_done and any(s is None for s in cosign) and not mine:
            awaiting_cosign = True
            prepared = m["settle_prepared_ts"] or 0
            auto_release_in = max(0, int(prepared + config.SETTLE_STALL_SECS - time.time()))

    return {
        "state": "broadcast" if m["settle_txid"] else ("ready" if not mine else "needs_signature"),
        "txid": m["settle_txid"],
        "txJson": m["settle_tx_json"] if mine else None,
        "mySignatureInputs": mine,
        # Waiting on the OTHER player: a draw's other depositor, or (mutual) the
        # loser's co-sign before the arbiter stall-breaker releases.
        "waitingOnOpponent": bool(not mine and any(s is None for s in primary)) or awaiting_cosign,
        # This ask releases the pot to the opponent, not to me — the loser
        # confirming an honest result. Drives the button label and copy.
        "cosignAsk": is_cosign_ask,
        "awaitingCosign": awaiting_cosign,
        "autoReleaseInSecs": auto_release_in,
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
        "youWon": you_won,
    }


# ── covenant escrow v2 settlement (roadmap #2) ──────────────────────────────
# Nothing like the v1 co-sign dance: DAGmate signs the result once and the escrow SCRIPT pays
# the winner (or, on a draw, each depositor). No tx is stored per-player, no signature is
# collected — settle is a single server-side action, idempotent and safe to call from either
# player's poll. Proven end-to-end on mainnet dust (service/spikes_covenant.mjs + test_escrow_v2.mjs).

def _outcome_of(m: dict, a: dict, b: dict) -> str:
    """'A' | 'B' | 'draw' from the recorded result. A = player_a wins, B = player_b, draw =
    no winner (agreed draw, or a FIDE insufficient-material flag)."""
    wid = m["winner_account_id"]
    if wid is None:
        return "draw"
    return "A" if wid == a["id"] else "B"


async def _settle_v2(match_id: str, m: dict, address: str, a: dict, b: dict) -> dict:
    """Sign the verdict and release a v2 match. Returns the public payout state either way.

    Idempotent: if a txid already landed (this player or the other polled first), it reports the
    paid state without touching the chain. A concurrent double-settle is a double-spend the node
    rejects — caught here and reported as the success it is, exactly like v1's broadcast guard."""
    if m["settle_txid"]:
        return _public_v2(m, address, a, b)

    pot = (m["funded_a_sompi"] or 0) + (m["funded_b_sompi"] or 0)
    if pot and pot < 2 * config.SETTLE_V2_FEE_SOMPI_PER_INPUT:
        raise SettlementError(
            "this pot is smaller than the Kaspa network fee needed to release it, so there is "
            "nothing to claim — the match still stands as an on-chain record")
    if not m["escrow_a_address"] or not m["escrow_b_address"]:
        raise SettlementError("this match has no escrow addresses — it was created while the "
                              "Kaspa service was unreachable")

    outcome = _outcome_of(m, a, b)
    # The verdict is the ONLY thing DAGmate signs. Kept and published (settle_v2_verdict_json) so
    # the winner or anyone can relay the settle even if DAGmate never does — the v2 escape hatch.
    verdict = await service_client.oracle_sign_result(match_id=m["hd_index"], outcome=outcome)
    escrows = [
        {"address": m["escrow_a_address"], "redeemHex": m["escrow_a_redeem_hex"], "side": "A"},
        {"address": m["escrow_b_address"], "redeemHex": m["escrow_b_redeem_hex"], "side": "B"},
    ]
    try:
        res = await service_client.settle_v2(
            match_id=m["hd_index"], escrows=escrows, outcome=outcome,
            pk_a=a["pubkey"], pk_b=b["pubkey"], sig_a=verdict["sigA"], sig_b=verdict["sigB"])
    except service_client.ServiceError as e:
        current = db.get_match(match_id)
        if current["settle_txid"]:
            return _public_v2(current, address, a, b)  # someone else settled first — success
        raise SettlementError(str(e))
    # Guarded write: only the first settle records. A loser of the race read the txid above.
    if not db.mark_v2_settled(match_id, res["txid"], json.dumps(verdict)):
        log.info(f"v2 settle for {match_id} raced — keeping the first txid")
    return _public_v2(db.get_match(match_id), address, a, b)


def _public_v2(m: dict, address: str, a: dict, b: dict) -> dict:
    """What the browser needs for a v2 match's payout panel. There is nothing to sign, so the
    shape mirrors a v1 already-broadcast settle: an amount and a txid, plus flags so the UI can
    say 'paid automatically'."""
    is_draw = m["winner_account_id"] is None
    you_won = (not is_draw
               and (a if m["winner_account_id"] == a["id"] else b)["address"] == address)
    fee_per = config.SETTLE_V2_FEE_SOMPI_PER_INPUT
    pot = (m["funded_a_sompi"] or 0) + (m["funded_b_sompi"] or 0)
    fee = fee_per * 2  # one input per escrow
    if is_draw:
        # Each depositor gets their OWN stake back, minus that one input's fee.
        mine = (m["funded_a_sompi"] if address == a["address"] else m["funded_b_sompi"]) or 0
        payout = max(0, mine - fee_per)
    elif you_won:
        payout = max(0, pot - fee)
    else:
        payout = 0
    verdict = json.loads(m["settle_v2_verdict_json"]) if m["settle_v2_verdict_json"] else None
    return {
        "state": "broadcast" if m["settle_txid"] else "settling",
        "txid": m["settle_txid"],
        "escrowVersion": "v2",
        "autoSettled": True,          # no player signature — the covenant paid out
        "mySignatureInputs": [],       # nothing to sign, ever
        "waitingOnOpponent": False,
        "isDraw": is_draw,
        "youWon": you_won,
        "potKas": pot / config.SOMPI_PER_KAS,
        "networkFeeKas": fee / config.SOMPI_PER_KAS,
        "platformFeeKas": 0.0,
        "payoutKas": payout / config.SOMPI_PER_KAS,
        "potSompi": str(pot),
        "networkFeeSompi": str(fee),
        "platformFeeSompi": "0",
        "payoutSompi": str(payout),
        # The published oracle verdict — the escape hatch that lets the pot be released without
        # DAGmate. Surfaced so the UI (or a determined player) can relay it if we ever don't.
        "verdict": verdict,
    }


# ── free games (no wager) ───────────────────────────────────────────────────
def _public_free(m: dict, address: str, a: dict, b: dict) -> dict:
    """A free match has no escrow and no pot, so there is nothing to settle — the claim panel
    just reports the result. Same key shape as a settled payout (so the client's existing
    plumbing works) but every money figure is zero and the state is 'free'."""
    is_draw = m["winner_account_id"] is None
    you_won = (not is_draw
               and (a if m["winner_account_id"] == a["id"] else b)["address"] == address)
    return {
        "state": "free",
        "isFree": True,
        "isDraw": is_draw,
        "youWon": you_won,
        "mySignatureInputs": [],
        "waitingOnOpponent": False,
        "txid": None,
        "potKas": 0.0, "networkFeeKas": 0.0, "platformFeeKas": 0.0, "payoutKas": 0.0,
        "potSompi": "0", "networkFeeSompi": "0", "platformFeeSompi": "0", "payoutSompi": "0",
    }
