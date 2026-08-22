"""DAGmate site backend — on-chain deposit watcher (docs/DAGMATE_SPEC.md §2).

The only thing that may move a match from `awaiting_deposit` to `live`. It
polls the real balance of both escrow addresses and starts the game once each
side holds at least its stake, confirmed.

This is the trust boundary for a public deployment: a match going `live` is
what makes a stake settleable to a winner, so anything that can fake this
transition is a way to play for someone else's money. The rules it enforces:

  - EACH escrow must independently hold >= the full stake. Checking the pot in
    aggregate would let one player fund both sides' worth and play a "free"
    game against an opponent who paid nothing.
  - Only CONFIRMED value counts (config.DEPOSIT_CONFIRM_DAA deep), so a match
    can't be started on a UTXO that could still be reorged away.
  - Every transition is a guarded UPDATE (see database.mark_match_live), so
    re-polling the same match — or running two of these — can't double-start
    it or double-notify.
  - Funding that never completes expires the match rather than leaving one
    player's stake in limbo until the 14-day CLTV.

It is also strictly additive: it never spends, never signs, and holds no keys.
The worst a bug here can do is fail to start a match, which is recoverable;
it cannot move funds.
"""
from __future__ import annotations

import asyncio
import logging
import time

import bot_client
import clocks
import config
import database as db
from service_client import ServiceError, escrow_balances

log = logging.getLogger("dagmate.deposits")


async def poll_once() -> int:
    """One sweep over every match awaiting a deposit. Returns how many went
    live. Raises nothing: the caller is a forever-loop."""
    matches = db.list_matches_awaiting_deposit()
    if not matches:
        return 0

    addresses = sorted({a for m in matches
                        for a in (m["escrow_a_address"], m["escrow_b_address"])})
    try:
        balances = await escrow_balances(addresses, config.DEPOSIT_CONFIRM_DAA)
    except ServiceError as e:
        # Node/sidecar down. Do NOT expire anything on this pass — we can't
        # tell "nobody paid" from "we can't see the chain", and expiring a
        # funded match on a blind guess is the expensive mistake.
        log.warning(f"deposit poll skipped, sidecar unreachable: {e}")
        return 0

    started = 0
    for m in matches:
        try:
            if await _check_match(m, balances):
                started += 1
        except Exception as e:  # one bad match must not stall the rest
            log.exception(f"deposit check failed for match {m['id']}: {e}")
    return started


async def _check_match(m: dict, balances: dict[str, dict]) -> bool:
    stake = int(m["stake_sompi"])
    a = balances.get(m["escrow_a_address"], {}).get("confirmedSompi", 0)
    b = balances.get(m["escrow_b_address"], {}).get("confirmedSompi", 0)
    row = db.record_deposits(m["id"], a, b, stake)

    # Read the latched timestamps rather than the live amounts: a side that was
    # once seen fully funded stays funded even if this poll under-reports it.
    both_funded = row["funded_a_ts"] is not None and row["funded_b_ts"] is not None
    if both_funded:
        initial_ms, increment_ms = clocks.settings_for(m["mode"])
        if db.mark_match_live(m["id"], initial_ms=initial_ms, increment_ms=increment_ms,
                              now_ms=clocks.now_ms()):
            log.info(f"match {m['id']} funded ({a}+{b} sompi, stake {stake}) — live")
            await _notify_both(m, "Both stakes are in — your match is live.")
            return True
        return False

    age = int(time.time()) - int(m["created_ts"])
    if age > config.DEPOSIT_DEADLINE_SECS and db.expire_match(m["id"]):
        paid = "A" if row["funded_a_ts"] else ("B" if row["funded_b_ts"] else "neither side")
        log.info(f"match {m['id']} expired unfunded after {age}s (funded: {paid})")
        await _notify_both(m, "Match cancelled — both stakes weren't deposited in time. "
                              "Any stake you sent stays yours and is reclaimable from your "
                              "own escrow after the 14-day timelock.")
    return False


async def _notify_both(m: dict, summary: str):
    for pid in (m["player_a_account_id"], m["player_b_account_id"]):
        await bot_client.notify_settled(pid, m["id"], summary)


async def watch_loop():
    log.info(f"deposit watcher started (every {config.DEPOSIT_POLL_SECS}s, "
             f"{config.DEPOSIT_CONFIRM_DAA} DAA confirmations)")
    while True:
        try:
            await poll_once()
        except Exception as e:
            log.exception(f"deposit watcher iteration failed: {e}")
        await asyncio.sleep(config.DEPOSIT_POLL_SECS)
