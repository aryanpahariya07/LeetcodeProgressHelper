"""The readiness model interface.

Two implementations exist (spec §6.3): a Beta-Binomial baseline, which is
**primary**, and Glicko-2, which is **experimental** and must beat the baseline
on real data or be deleted. Both run on every attempt and log their predictions,
so the Phase 6 comparison has something to compare.

Switching the primary model is a configuration change — nothing outside this
package should reference either implementation by name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeVar

from dsa_coach import tuning
from dsa_coach.mechanism.evidence import Evidence, ProblemRef
from dsa_coach.models import Level

State = TypeVar("State")


@dataclass(frozen=True)
class Prediction:
    """An estimate, plus how much to trust it.

    `score` is NOT a calibrated probability. It is an uncalibrated estimate, and
    the UI must present it as a band until a calibration report earns otherwise
    (spec §6.3, invariant 12).
    """

    score: float
    uncertainty: float

    @property
    def calibrated(self) -> bool:
        """Whether there is enough evidence to show anything but 'Calibrating'."""
        return self.uncertainty <= tuning.CALIBRATED_UNCERTAINTY_MAX

    @property
    def band(self) -> str:
        """Display band. `calibrating` while uncertainty is still high."""
        if not self.calibrated:
            return "calibrating"
        for threshold, name in tuning.READINESS_BANDS:
            if self.score < threshold:
                return name
        return tuning.READINESS_BANDS[-1][1]


class ReadinessModel(Protocol[State]):
    """A pluggable estimator of how ready the user is on one pattern."""

    version: str

    def blank(self, level: Level) -> State:
        """Starting state before any evidence, primed by self-assessed level."""
        ...

    def predict(self, state: State, problem: ProblemRef) -> Prediction:
        """Expected outcome if this user attempted this problem now."""
        ...

    def update(self, state: State, evidence: Evidence) -> State:
        """Fold in one piece of evidence. Pure — returns a new state."""
        ...

    def summary(self, state: State, reference: ProblemRef) -> Prediction:
        """Overall readiness on this pattern, expressed against a reference problem.

        There is no absolute readiness number: "ready" only means anything
        relative to some difficulty. The reference is normally the median rating
        of the pattern's active catalogue problems.
        """
        ...

    def serialize(self, state: State) -> dict[str, object]:
        """State as JSON-safe data, for storage."""
        ...

    def deserialize(self, raw: dict[str, object]) -> State:
        """Inverse of `serialize`."""
        ...
