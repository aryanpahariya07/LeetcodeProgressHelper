"""Glicko-2 readiness model — EXPERIMENTAL (spec §6.2, §6.3).

The user is a player; each problem is an opponent whose rating and rating
deviation are known. This is the principled way to account for problem-rating
reliability: it enters through Glicko's g(phi) term rather than an ad-hoc penalty.

**Deviations from textbook Glicko-2**, which mean its statistical guarantees are
approximate here and are exactly why §6.3's calibration comparison exists:

1. Each attempt is its own rating period rather than a batch of games. Glickman
   assumes 10-15 games per period; waiting for that would make the estimate
   useless for scheduling.
2. Evidence carries a weight in [0, 1] (capture confidence, repeat-attempt decay).
   Weights scale the v and delta contributions, treating an attempt as a partial
   game. Standard Glicko-2 has no such notion.
3. Outcomes are continuous in [0, 1] — solved-after-hint is 0.6 — not win/draw/loss.

This model is deleted if it does not beat the Beta baseline on real data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from dsa_coach import tuning
from dsa_coach.mechanism.evidence import Evidence, ProblemRef
from dsa_coach.mechanism.readiness.base import Prediction
from dsa_coach.models import Level

VERSION = "glicko2-v1"

#: Glicko-2's internal scale factor.
SCALE = 173.7178


@dataclass(frozen=True)
class GlickoState:
    rating: float
    rd: float
    volatility: float


def _g(phi: float) -> float:
    return 1.0 / math.sqrt(1.0 + 3.0 * phi**2 / math.pi**2)


def _expected(mu: float, mu_j: float, phi_j: float) -> float:
    return 1.0 / (1.0 + math.exp(-_g(phi_j) * (mu - mu_j)))


def _new_volatility(phi: float, sigma: float, v: float, delta: float) -> float:
    """Glickman's Illinois-method solution for the new volatility."""
    tau = tuning.GLICKO_TAU
    a = math.log(sigma**2)

    def f(x: float) -> float:
        ex = math.exp(x)
        numerator = ex * (delta**2 - phi**2 - v - ex)
        denominator = 2.0 * (phi**2 + v + ex) ** 2
        return numerator / denominator - (x - a) / tau**2

    big_a = a
    if delta**2 > phi**2 + v:
        big_b = math.log(delta**2 - phi**2 - v)
    else:
        k = 1
        while f(a - k * tau) < 0 and k < 100:
            k += 1
        big_b = a - k * tau

    f_a, f_b = f(big_a), f(big_b)
    epsilon = tuning.GLICKO_CONVERGENCE_EPSILON

    guard = 0
    while abs(big_b - big_a) > epsilon and guard < 100:
        c = big_a + (big_a - big_b) * f_a / (f_b - f_a)
        f_c = f(c)
        if f_c * f_b <= 0:
            big_a, f_a = big_b, f_b
        else:
            f_a /= 2.0
        big_b, f_b = c, f_c
        guard += 1

    return math.exp(big_a / 2.0)


class Glicko2ReadinessModel:
    version = VERSION

    def blank(self, level: Level) -> GlickoState:
        return GlickoState(
            rating=tuning.GLICKO_PRIOR_RATING[level],
            rd=tuning.GLICKO_START_RD,
            volatility=tuning.GLICKO_START_VOLATILITY,
        )

    def predict(self, state: GlickoState, problem: ProblemRef) -> Prediction:
        mu = (state.rating - 1500.0) / SCALE
        mu_j = (problem.rating - 1500.0) / SCALE
        phi_j = problem.rating_rd / SCALE
        return Prediction(
            score=_expected(mu, mu_j, phi_j),
            uncertainty=min(state.rd / tuning.GLICKO_START_RD, 1.0),
        )

    def update(self, state: GlickoState, evidence: Evidence) -> GlickoState:
        mu = (state.rating - 1500.0) / SCALE
        phi = state.rd / SCALE
        mu_j = (evidence.problem.rating - 1500.0) / SCALE
        phi_j = evidence.problem.rating_rd / SCALE

        g_j = _g(phi_j)
        e_j = _expected(mu, mu_j, phi_j)
        w = evidence.weight

        # Deviation 2: weight scales the contribution, as a partial game.
        v_inverse = w * g_j**2 * e_j * (1.0 - e_j)
        if v_inverse <= 1e-12:
            # The outcome was a foregone conclusion; nothing to learn.
            return state
        v = 1.0 / v_inverse
        delta_sum = w * g_j * (evidence.score - e_j)
        delta = v * delta_sum

        sigma_prime = _new_volatility(phi, state.volatility, v, delta)
        phi_star = math.sqrt(phi**2 + sigma_prime**2)
        phi_prime = 1.0 / math.sqrt(1.0 / phi_star**2 + 1.0 / v)
        mu_prime = mu + phi_prime**2 * delta_sum

        return GlickoState(
            rating=mu_prime * SCALE + 1500.0,
            rd=phi_prime * SCALE,
            volatility=sigma_prime,
        )

    def summary(self, state: GlickoState, reference: ProblemRef) -> Prediction:
        return self.predict(state, reference)

    def serialize(self, state: GlickoState) -> dict[str, object]:
        return {"rating": state.rating, "rd": state.rd, "volatility": state.volatility}

    def deserialize(self, raw: dict[str, object]) -> GlickoState:
        return GlickoState(
            rating=float(raw["rating"]),  # type: ignore[arg-type]
            rd=float(raw["rd"]),  # type: ignore[arg-type]
            volatility=float(raw["volatility"]),  # type: ignore[arg-type]
        )
