"""DAGmate site backend — game clocks and the forfeit path.

Why this exists at all: without a clock, a player who is losing has every
incentive to simply stop moving. The match then sits untouched until the
14-day CLTV reclaim, both stakes are refunded, and walking away becomes a
free undo on any lost position. A clock is what makes a won game collectable.

Everything here is server-authoritative. The browser is sent the numbers so it
can render a smooth countdown, but it is never asked what time it is — a
client that lies about its clock would be lying about who owns the pot.

Time model: Fischer increment, one mechanism for both modes (`daily` is just a
very slow rapid game). A clock runs only while a match is `live`, and only for
the side to move. Stored state is therefore:
  clock_{white,black}_ms  — time banked as at the START of the current turn
  clock_turn_started_ms   — when the side to move started thinking
so the mover's live remaining is `banked - (now - turn_started)`, and the
waiting player's is just `banked`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import bot_client
import chess_logic
import config
import database as db
import service_client

log = logging.getLogger("dagmate.clocks")


def now_ms() -> int:
    return int(time.time() * 1000)


def settings_for(mode: str) -> tuple[int, int]:
    """(initial_ms, increment_ms). Unknown modes fall back to rapid rather
    than raising — a match that somehow has a bad mode should still get a
    clock, because a match with no clock is the failure we're preventing."""
    cfg = config.CLOCK_MODES.get(mode) or config.CLOCK_MODES["rapid"]
    return cfg["initial_secs"] * 1000, cfg["increment_secs"] * 1000


def remaining_ms(m: dict, at_ms: int | None = None) -> tuple[int, int]:
    """Live (white_ms, black_ms), floored at zero. Safe on a match whose clock
    was never started — an un-started clock reads as full, never as flagged."""
    white = m["clock_white_ms"]
    black = m["clock_black_ms"]
    if white is None or black is None:
        initial, _ = settings_for(m["mode"])
        return initial, initial
    started = m["clock_turn_started_ms"]
    if m["status"] != "live" or started is None:
        return white, black  # not ticking (awaiting deposit, or already over)
    elapsed = max(0, (at_ms if at_ms is not None else now_ms()) - started)
    if m["turn"] == "white":
        return max(0, white - elapsed), black
    return white, max(0, black - elapsed)


def flagged_color(m: dict, at_ms: int | None = None) -> str | None:
    """Which side (if any) has run out. Only ever the side to move: the waiting
    player's clock isn't running, so they cannot flag on someone else's turn."""
    if m["status"] != "live" or m["clock_turn_started_ms"] is None:
        return None
    white, black = remaining_ms(m, at_ms)
    if m["turn"] == "white" and white <= 0:
        return "white"
    if m["turn"] == "black" and black <= 0:
        return "black"
    return None


def public(m: dict, at_ms: int | None = None) -> dict:
    at = at_ms if at_ms is not None else now_ms()
    white, black = remaining_ms(m, at)
    cfg = config.CLOCK_MODES.get(m["mode"]) or config.CLOCK_MODES["rapid"]
    return {
        "label": cfg["label"],
        "whiteMs": white, "blackMs": black,
        "incrementMs": m["clock_increment_ms"] or 0,
        "running": m["status"] == "live" and m["clock_turn_started_ms"] is not None,
        "turn": m["turn"],
        # The client renders a countdown from this offset rather than from its
        # own wall clock, so a skewed device shows the right time and a lying
        # one still can't change the outcome.
        "serverNowMs": at,
    }


def charge_move(m: dict, mover_color: str, at_ms: int) -> int:
    """The mover's new banked time: what's left after this think, plus the
    increment. Never negative — a flag is handled before we get here."""
    white, black = remaining_ms(m, at_ms)
    left = white if mover_color == "white" else black
    return max(0, left) + (m["clock_increment_ms"] or 0)


async def forfeit_if_flagged(m: dict, at_ms: int | None = None) -> bool:
    """End a match whose side-to-move has run out. Returns whether this call
    is the one that ended it — the write is guarded, so a flag landing at the
    same instant as the opponent's move can't settle the match twice."""
    color = flagged_color(m, at_ms)
    if not color:
        return False
    result, winner_color = chess_logic.timeout_result(m["fen"], color)
    # Tournament rule: a flag is ALWAYS a loss. A knockout can't carry a timeout
    # draw (FIDE 6.9 would score a flag with no mating material as a draw, which
    # would stall the bracket), and "you ran out of time — you're out" is a
    # clean decisive rule for a wagered timed game.
    if m["tournament_id"] and winner_color is None:
        winner_color = "black" if color == "white" else "white"
        result = "timeout"
    winner_id = None
    if winner_color == "white":
        winner_id = m["player_a_account_id"]
    elif winner_color == "black":
        winner_id = m["player_b_account_id"]
    # Roadmap #3a: a v3 match tries the TRUSTLESS forfeit first — if the latest co-signed checkpoint
    # authorises the winner, the pot is claimed into the pending covenant with NO oracle signature.
    # It only takes when the co-signed state agrees the flagger lost; otherwise (a draw-timeout, no
    # checkpoint, or the last co-signed state authorises the loser) we fall back to the oracle settle
    # below — the same path v2 uses — so a match always resolves.
    trustless = await _try_v3_forfeit(m, winner_color, winner_id)
    if not trustless:
        if not db.settle_match_if_live(m["id"], result=result, winner_account_id=winner_id):
            return False
    log.info(f"match {m['id']}: {color} flagged — {result}{' (trustless forfeit claimed)' if trustless else ''}")
    summary = (f"{color.capitalize()} ran out of time — drawn, because the other side had "
               "no material to mate with."
               if winner_id is None else
               (f"{color.capitalize()} ran out of time — the pot is yours after the challenge window."
                if trustless else f"{color.capitalize()} ran out of time."))
    for pid in (m["player_a_account_id"], m["player_b_account_id"]):
        await bot_client.notify_settled(pid, m["id"], summary)
    # A tournament match decided on the clock still has to advance the bracket —
    # the forfeit path bypasses _settle_game_over, so it carries its own hook.
    # Deferred import: main imports this module at startup, so a top-level import
    # here would be a cycle.
    if m["tournament_id"] and winner_id:
        import main
        await main.advance_tournament(m["tournament_id"], m["round"])
    return True


async def _try_v3_forfeit(m: dict, winner_color: str | None, winner_id: str | None) -> bool:
    """Attempt the trustless forfeit for a v3 match (roadmap #3a). Returns True only if the pot was
    claimed on-chain into the pending covenant (and the match marked settled); False to fall back to
    the oracle settle. Takes ONLY when the latest FULLY co-signed checkpoint names the winner as its
    claimant — i.e. both players agreed the state where the flagger is the one to move. A draw-timeout,
    a match with no co-signed checkpoint, or one whose last co-signed state authorises the loser (e.g.
    the opponent stopped counter-signing before abandoning) all return False → oracle safety net."""
    if (m["escrow_version"] or "") != "v3" or winner_color is None or winner_id is None:
        return False
    if not m["cosigned_cp_json"] or not m["sess_pk_a"] or not m["sess_pk_b"]:
        return False
    cp = json.loads(m["cosigned_cp_json"])
    winner_side = "A" if winner_color == "white" else "B"
    if cp.get("claimant") != winner_side or not (cp.get("sigA") and cp.get("sigB")):
        return False
    a = db.get_account(m["player_a_account_id"])
    b = db.get_account(m["player_b_account_id"])
    escrows = []
    if m["escrow_a_address"]:
        escrows.append({"address": m["escrow_a_address"], "redeemHex": m["escrow_a_redeem_hex"]})
    if m["escrow_b_address"]:
        escrows.append({"address": m["escrow_b_address"], "redeemHex": m["escrow_b_redeem_hex"]})
    if not escrows:
        return False
    try:
        res = await service_client.forfeit_claim_v3(
            escrows=escrows, match_id=m["hd_index"], pk_a=a["pubkey"], pk_b=b["pubkey"],
            sess_pk_a=m["sess_pk_a"], sess_pk_b=m["sess_pk_b"], w_daa=m["w_daa"],
            deadline_daa=cp["deadlineDaa"], ply=cp["ply"], claimant=winner_side,
            sig_a=cp["sigA"], sig_b=cp["sigB"])
    except Exception as e:  # node down, sig the covenant rejects, escrow already spent — fall back
        log.warning(f"match {m['id']}: v3 trustless forfeit failed ({e}); falling back to oracle")
        return False
    ok = db.mark_forfeit_claimed(m["id"], res["txid"], cp["deadlineDaa"], res["pendingAddress"],
                                 res["pendingRedeem"], winner_id, result="timeout")
    if ok:
        log.info(f"match {m['id']}: v3 trustless forfeit {res['txid']} -> pending {res['pendingAddress']}")
    return ok


async def finalise_due_forfeits() -> int:
    """Sweep the pot from the pending covenant to the winner once each claim's challenge window has
    passed (roadmap #3a). Gated on wall-time since the claim (>= the window) so we don't spam the node
    with early attempts; forfeit_finalise still enforces the on-chain DAA window, so an early attempt
    just no-ops. Returns how many were finalised."""
    if not config.ESCROW_V3_ENABLED:
        return 0
    now = time.time()
    done = 0
    for m in db.list_forfeit_pending():
        if now - (m["settled_ts"] or now) < config.CHALLENGE_WINDOW_SECS:
            continue  # window not up yet
        try:
            a = db.get_account(m["player_a_account_id"])
            b = db.get_account(m["player_b_account_id"])
            winner_side = "A" if m["winner_account_id"] == m["player_a_account_id"] else "B"
            res = await service_client.forfeit_finalise_v3(
                pending_redeem=m["forfeit_pending_redeem"], pending_address=m["forfeit_pending_address"],
                claimant=winner_side, pk_a=a["pubkey"], pk_b=b["pubkey"], w_daa=m["w_daa"])
            if db.mark_forfeit_finalised(m["id"], res["txid"]):
                done += 1
                log.info(f"match {m['id']}: forfeit finalised {res['txid']} — pot paid to {winner_side}")
        except Exception as e:
            log.info(f"match {m['id']}: forfeit finalise not ready ({e}); will retry")
    return done


async def _warn_if_low(m: dict, at_ms: int):
    """One low-time DM per player per match, for the side to move only."""
    color = m["turn"]
    if (m["clock_warned_white"] if color == "white" else m["clock_warned_black"]):
        return
    initial, _ = settings_for(m["mode"])
    white, black = remaining_ms(m, at_ms)
    left = white if color == "white" else black
    if left > initial * config.CLOCK_WARN_FRACTION:
        return
    if not db.mark_clock_warned(m["id"], color):
        return
    account_id = m["player_a_account_id"] if color == "white" else m["player_b_account_id"]
    await bot_client.notify_clock_warning(account_id, m["id"], _human(left), f"/play/{m['id']}")


def _human(ms: int) -> str:
    secs = max(0, ms // 1000)
    if secs >= 3600:
        return f"{secs // 3600}h {(secs % 3600) // 60}m"
    if secs >= 60:
        return f"{secs // 60}m {secs % 60}s"
    return f"{secs}s"


async def poll_once() -> int:
    """One sweep over live matches: forfeit anyone who has flagged, warn anyone
    running low. Returns how many matches were forfeited."""
    at = now_ms()
    forfeited = 0
    for m in db.list_live_matches():
        try:
            if await forfeit_if_flagged(m, at):
                forfeited += 1
            else:
                await _warn_if_low(m, at)
        except Exception as e:  # one bad match must not stall the rest
            log.exception(f"clock check failed for match {m['id']}: {e}")
    return forfeited


async def watch_loop():
    log.info(f"clock watcher started (every {config.CLOCK_POLL_SECS}s)")
    while True:
        try:
            await poll_once()
            # v3: sweep any forfeit whose challenge window has now passed. Cheap when there are none
            # (one indexed query); gated by wall-time inside, so it won't hammer the node.
            await finalise_due_forfeits()
        except Exception as e:
            log.exception(f"clock watcher iteration failed: {e}")
        await asyncio.sleep(config.CLOCK_POLL_SECS)
