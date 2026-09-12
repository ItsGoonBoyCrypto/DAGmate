"""Glicko-2 rating math — pure functions, no DB, no I/O.

Mark Glickman's Glicko-2 (the system Lichess runs). A player is (rating r, rating deviation rd,
volatility vol). Ratings live on the familiar ~1500 scale; internally the system works on a transformed
scale (mu, phi) with the constant 173.7178 = 400/ln(10) and converts back.

`rate()` applies ONE rating period (a list of results against opponents) and returns the new
(r, rd, vol). `inflate()` grows rd for periods a player sat idle. Validated against Glickman's own
worked example (see tools/test_ratings.py): r=1500/rd=200/vol=0.06 vs three opponents, tau=0.5
-> r≈1464.05, rd≈151.52, vol≈0.05999.
"""
from __future__ import annotations
import math

SCALE = 173.7178          # 400 / ln(10)
DEFAULT_R = 1500.0
DEFAULT_RD = 350.0        # also the cap: "effectively unrated"
DEFAULT_VOL = 0.06
MIN_RD = 40.0             # floor, so an established rating stays adjustable
TAU = 0.4                 # system constant; lower = steadier (money-safe). Glickman suggests 0.3–1.2.
_EPS = 1e-6


def _g(phi: float) -> float:
    return 1.0 / math.sqrt(1.0 + 3.0 * phi * phi / (math.pi * math.pi))


def _E(mu: float, mu_j: float, phi_j: float) -> float:
    return 1.0 / (1.0 + math.exp(-_g(phi_j) * (mu - mu_j)))


def _clamp_rd(rd: float) -> float:
    return max(MIN_RD, min(rd, DEFAULT_RD))


def inflate(rd: float, vol: float, periods: int) -> float:
    """Grow rd for `periods` rating periods with no games played (phi* = sqrt(phi^2 + vol^2), once per
    idle period), capped at DEFAULT_RD. `periods` <= 0 returns rd unchanged."""
    phi = rd / SCALE
    for _ in range(max(0, int(periods))):
        phi = math.sqrt(phi * phi + vol * vol)
        if SCALE * phi >= DEFAULT_RD:
            return DEFAULT_RD
    return _clamp_rd(SCALE * phi)


def rate(r: float, rd: float, vol: float, results: list[tuple[float, float, float]],
         tau: float = TAU) -> tuple[float, float, float]:
    """Apply one rating period. `results` = [(opponent_r, opponent_rd, score)] with score in {1, 0.5, 0}.
    Empty `results` = a period with no games: rd grows by one period, r and vol unchanged.
    Returns (r_new, rd_new, vol_new)."""
    mu = (r - DEFAULT_R) / SCALE
    phi = rd / SCALE

    if not results:
        phi_star = math.sqrt(phi * phi + vol * vol)
        return (r, _clamp_rd(SCALE * phi_star), vol)

    # Step 3: estimated variance v from game outcomes; Step 4: improvement delta.
    v_inv = 0.0
    delta_sum = 0.0
    for (opp_r, opp_rd, s) in results:
        mu_j = (opp_r - DEFAULT_R) / SCALE
        phi_j = opp_rd / SCALE
        g = _g(phi_j)
        e = _E(mu, mu_j, phi_j)
        v_inv += g * g * e * (1.0 - e)
        delta_sum += g * (s - e)
    v = 1.0 / v_inv
    delta = v * delta_sum

    # Step 5: new volatility, via Illinois-method root find on f(x).
    a = math.log(vol * vol)

    def f(x: float) -> float:
        ex = math.exp(x)
        num = ex * (delta * delta - phi * phi - v - ex)
        den = 2.0 * (phi * phi + v + ex) ** 2
        return num / den - (x - a) / (tau * tau)

    A = a
    if delta * delta > phi * phi + v:
        B = math.log(delta * delta - phi * phi - v)
    else:
        k = 1
        while f(a - k * tau) < 0:
            k += 1
        B = a - k * tau
    fA, fB = f(A), f(B)
    while abs(B - A) > _EPS:
        C = A + (A - B) * fA / (fB - fA)
        fC = f(C)
        if fC * fB <= 0:
            A, fA = B, fB
        else:
            fA = fA / 2.0
        B, fB = C, fC
    vol_new = math.exp(A / 2.0)

    # Step 6: pre-rating-period deviation; Step 7: new phi and mu.
    phi_star = math.sqrt(phi * phi + vol_new * vol_new)
    phi_new = 1.0 / math.sqrt(1.0 / (phi_star * phi_star) + 1.0 / v)
    mu_new = mu + phi_new * phi_new * delta_sum

    return (SCALE * mu_new + DEFAULT_R, _clamp_rd(SCALE * phi_new), vol_new)
