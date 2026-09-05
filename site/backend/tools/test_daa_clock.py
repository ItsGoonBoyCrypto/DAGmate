"""Tests for the DAA clock (roadmap #3a). Run: python tools/test_daa_clock.py

Pure math — no DB, no network. The load-bearing property is the SAFETY DIRECTION:
a DAA deadline must never be shorter in real time than the wall-clock time the
player was shown, at the actually-measured chain rate. We assert that against the
mainnet-measured 9.61 DAA/s, using the configured (high-side) rate for the math.
"""
from __future__ import annotations

import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DAGMATE_SITE_DB", os.path.join(tempfile.mkdtemp(prefix="dagmate-daa-"), "t.db"))

import config  # noqa: E402
import daa_clock as dc  # noqa: E402

MEASURED_DAA_PER_SEC = 9.61  # mainnet 2026-09-05; the clock must be safe at THIS actual rate
bad = 0


def check(label, cond):
    global bad
    if not cond:
        bad += 1
    print(f"   {'ok ' if cond else 'BAD'} {label}")


# ── secs_to_daa: rounds UP, uses the configured rate ──
check("secs_to_daa(0) == 0", dc.secs_to_daa(0) == 0)
check("secs_to_daa rounds up", dc.secs_to_daa(1.0 / config.DAA_PER_SEC + 0.0001) == 2)
check("secs_to_daa(600) == ceil(600*rate)", dc.secs_to_daa(600) == math.ceil(600 * config.DAA_PER_SEC))
check("negative secs clamped to 0", dc.secs_to_daa(-5) == 0)

# ── deadline_daa: now + secs·rate + fixed margin ──
now = 530_000_000
check("deadline_daa = now + secs_to_daa + margin",
      dc.deadline_daa(now, 600) == now + dc.secs_to_daa(600) + config.DAA_DEADLINE_MARGIN)
check("deadline strictly after now for any positive clock", dc.deadline_daa(now, 1) > now)

# ── THE SAFETY PROPERTY: at the real measured rate, the player gets >= their wall time ──
for wall_secs in (5, 30, 300, 600, 3 * 24 * 3600):
    ticks = dc.deadline_daa(now, wall_secs) - now
    real_secs = ticks / MEASURED_DAA_PER_SEC  # how long the chain actually takes to reach the deadline
    check(f"real time for a {wall_secs}s deadline ({real_secs:.0f}s) >= wall time (generous, never short)",
          real_secs >= wall_secs)

# ── S9 unilateral-move lower bound ──
c_deadline = dc.deadline_daa(now, 600)
opp_budget_secs = 300
mn = dc.min_next_deadline_daa(c_deadline, opp_budget_secs, "rapid")
check("min_next_deadline = C.deadline + oppBudget(DAA) + increment(DAA)",
      mn == c_deadline + dc.secs_to_daa(opp_budget_secs) + dc.increment_daa_for("rapid"))
check("increment_daa_for(rapid) == ceil(5s * rate)", dc.increment_daa_for("rapid") == math.ceil(5 * config.DAA_PER_SEC))
check("min_next_deadline grants strictly more than the opponent's raw budget", mn > c_deadline + dc.secs_to_daa(opp_budget_secs) - 1)

# ── challenge window ──
check("challenge_window_daa == CHALLENGE_WINDOW_SECS * rate",
      dc.challenge_window_daa() == math.ceil(config.CHALLENGE_WINDOW_SECS * config.DAA_PER_SEC))
check("challenge window is hours, not seconds (>= 1h of DAA)", dc.challenge_window_daa() >= math.ceil(3600 * config.DAA_PER_SEC))

# ── config sanity: the high-side rate must be >= what we measured, or deadlines go short ──
check("configured DAA_PER_SEC >= measured (deadlines never short)", config.DAA_PER_SEC >= MEASURED_DAA_PER_SEC)

print(f"\n{'DAA CLOCK OK' if bad == 0 else str(bad) + ' FAILURE(S)'} — {('all safe' if bad == 0 else 'FIX')}")
sys.exit(1 if bad else 0)
