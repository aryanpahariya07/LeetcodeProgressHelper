"""Beta-Binomial baseline — the PRIMARY readiness model (spec §6.3).

Per pattern, first-exposure attempts are split into coarse rating buckets, and
each bucket carries a Beta posterior over the outcome score.

Why this is primary rather than Glicko-2:

- Its uncertainty is a genuine posterior, not a heuristic deviation.
- It degrades gracefully at n=3, which is the regime this product actually
  operates in — a realistic first month yields a handful of first exposures per
  pattern, far below what Glicko-2 was designed for.
- It makes no assumptions this domain violates.

Its one real disadvantage is that difficulty enters only through bucketing, where
Glicko-2 uses it continuously. Whether that advantage survives sparse data is the
open question the Phase 6 bake-off settles.

Roughly twenty lines of actual mathematics. That is the point.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

from dsa_coach import tuning
from dsa_coach.mechanism.evidence import Evidence, ProblemRef
from dsa_coach.mechanism.readiness.base import Prediction
from dsa_coach.models import Level

VERSION = "baseline-beta-v1"


@dataclass(frozen=True)
class Bucket:
    alpha: float
    beta: float

    @property
    def n(self) -> float:
        return self.alpha + self.beta

    @property
    def mean(self) -> float:
        return self.alpha / self.n

    @property
    def sd(self) -> float:
        """Posterior standard deviation — the uncertainty the UI gates on."""
        return math.sqrt((self.alpha * self.beta) / (self.n**2 * (self.n + 1)))

    def observe(self, score: float, weight: float) -> Bucket:
        return Bucket(
            alpha=self.alpha + weight * score,
            beta=self.beta + weight * (1.0 - score),
        )


@dataclass(frozen=True)
class BaselineState:
    buckets: tuple[Bucket, ...]
    #: Kept so a pooled fallback can still reflect the starting prior.
    prior_mean: float

    def pooled(self) -> Bucket:
        return Bucket(
            alpha=sum(b.alpha for b in self.buckets),
            beta=sum(b.beta for b in self.buckets),
        )


def bucket_index(rating: int) -> int:
    """Which coarse rating band a problem falls into."""
    low, high = tuning.BASELINE_BUCKET_BOUNDS
    if rating < low:
        return 0
    if rating < high:
        return 1
    return 2


def rating_reliability(rating_rd: int) -> float:
    """Discount for an unreliable problem rating.

    Mild by design: the Beta model uses a rating only to choose a bucket, so
    rating error mostly costs an occasional misfiled observation.
    """
    normalized = min(rating_rd, 350) / 350
    return 1.0 - tuning.BASELINE_MAX_RATING_RD_PENALTY * normalized


class BetaBaselineReadinessModel:
    version = VERSION

    def blank(self, level: Level) -> BaselineState:
        mean = tuning.BASELINE_PRIOR_MEAN[level]
        strength = tuning.BASELINE_PRIOR_STRENGTH
        prior = Bucket(alpha=mean * strength, beta=(1.0 - mean) * strength)
        return BaselineState(buckets=(prior, prior, prior), prior_mean=mean)

    def predict(self, state: BaselineState, problem: ProblemRef) -> Prediction:
        bucket = self._effective_bucket(state, bucket_index(problem.rating))
        return Prediction(score=bucket.mean, uncertainty=bucket.sd)

    def update(self, state: BaselineState, evidence: Evidence) -> BaselineState:
        index = bucket_index(evidence.problem.rating)
        weight = evidence.weight * rating_reliability(evidence.problem.rating_rd)

        buckets = list(state.buckets)
        buckets[index] = buckets[index].observe(evidence.score, weight)
        return replace(state, buckets=tuple(buckets))

    def summary(self, state: BaselineState, reference: ProblemRef) -> Prediction:
        return self.predict(state, reference)

    def _effective_bucket(self, state: BaselineState, index: int) -> Bucket:
        """The target bucket, or the pooled posterior if it has no real evidence.

        Falling back to pooled keeps a first attempt in an unseen band from being
        judged against nothing but the prior.
        """
        bucket = state.buckets[index]
        prior_n = tuning.BASELINE_PRIOR_STRENGTH
        if bucket.n > prior_n + 1e-9:
            return bucket

        pooled = state.pooled()
        # `pooled` triple-counts the prior; scale it back to a single prior's worth.
        surplus = pooled.n - 3 * prior_n
        if surplus <= 1e-9:
            return bucket
        scale = (prior_n + surplus) / pooled.n
        return Bucket(alpha=pooled.alpha * scale, beta=pooled.beta * scale)

    def serialize(self, state: BaselineState) -> dict[str, object]:
        return {
            "buckets": [[b.alpha, b.beta] for b in state.buckets],
            "prior_mean": state.prior_mean,
        }

    def deserialize(self, raw: dict[str, object]) -> BaselineState:
        buckets_raw: Any = raw["buckets"]
        return BaselineState(
            buckets=tuple(Bucket(alpha=float(a), beta=float(b)) for a, b in buckets_raw),
            prior_mean=float(raw["prior_mean"]),  # type: ignore[arg-type]
        )
