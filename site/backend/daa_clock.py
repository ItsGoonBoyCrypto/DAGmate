"""DAGmate — the DAA clock: convert wall-clock game time into the DAA-score
deadlines the forfeit covenant reads (roadmap #3a / DAGMATE_MOVE_CHANNEL.md).

The wall-clock in clocks.py stays the authority for the UX countdown. This module
is a SEPARATE, pure mapping used only for the v3 trustless-forfeit path: it turns
"this player has N seconds left" into "deadlineDaa = the DAA score by which their
move must be co-signed, or the opponent may forfeit-claim".

Direction of safety (see config.DAA_PER_SEC): deadlineDaa = now_daa + secs·rate.
The covenant lets a forfeit through once chain DAA ≥ deadlineDaa. Real seconds for
the chain to get there = secs·rate / actual_rate. We set `rate` on the HIGH side of
the measured ~9.61/s (default 10.0) plus a fixed margin, so that ratio is ≥ 1 —
a player always gets AT LEAST their wall-clock time in real seconds, never less.
Drift can only ever be generous to the player being timed.

Nothing here touches the DB or the network; `now_daa` is passed in (the caller
fetches it via service_client.daa_score()). That keeps it trivially testable.
"""
from __future__ import annotations

import math

import config


def secs_to_daa(secs: float) -> int:
    """DAA ticks for `secs` seconds of clock, rounded UP (never round a deadline
    down — that would shorten a player's time)."""
    if secs < 0:
        secs = 0
    return math.ceil(secs * config.DAA_PER_SEC)


def deadline_daa(now_daa: int, remaining_secs: float) -> int:
    """The DAA score by which the side-to-move must have their next position
    co-signed. now_daa + their remaining clock (in DAA) + the fixed safety margin."""
    return int(now_daa) + secs_to_daa(remaining_secs) + config.DAA_DEADLINE_MARGIN


def increment_daa() -> int:
    """The per-move Fischer increment, in DAA — the INCREMENT term of the S9
    unilateral-move lower bound. Read from the mode's wall-clock increment."""
    # increment is per-mode; callers that know the mode pass its increment via
    # increment_daa_for(). This bare form is the zero-increment default.
    return 0


def increment_daa_for(mode: str) -> int:
    cfg = config.CLOCK_MODES.get(mode) or config.CLOCK_MODES["rapid"]
    return secs_to_daa(cfg["increment_secs"])


def challenge_window_daa() -> int:
    """The optimistic challenge window W, in DAA (config seconds · rate)."""
    return secs_to_daa(config.CHALLENGE_WINDOW_SECS)


def min_next_deadline_daa(c_deadline_daa: int, opp_budget_secs: float, mode: str) -> int:
    """The S9 LOWER bound a unilateral move's nextDeadlineDaa must meet:
        nextDeadlineDaa ≥ C.deadlineDaa + opponent_budget(DAA) + increment(DAA)
    i.e. the mover must grant the opponent at least their full remaining clock plus
    the increment. The covenant enforces `≥`; this computes the exact minimum so the
    move builder can set it (and the verifier can re-derive and refuse a short one)."""
    return int(c_deadline_daa) + secs_to_daa(opp_budget_secs) + increment_daa_for(mode)


def real_secs_for_daa(daa_ticks: int) -> float:
    """Inverse, for display/telemetry only: how many real seconds `daa_ticks` DAA
    is expected to take at the configured rate."""
    return daa_ticks / config.DAA_PER_SEC if config.DAA_PER_SEC else 0.0
