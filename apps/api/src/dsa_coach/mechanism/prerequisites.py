"""Prerequisite gating over the pattern DAG (spec §6.4).

Pure graph evaluation. A pattern is *unlocked* when every gating prerequisite is
both sufficiently ready and calibrated — an uncalibrated prerequisite has not
been demonstrated, only guessed at, and guessing is not evidence.

Weak edges (below the gating strength) are advisory ordering only; they influence
curriculum sequence but never block scheduling.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from uuid import UUID

from dsa_coach import tuning
from dsa_coach.mechanism.readiness.base import Prediction


@dataclass(frozen=True)
class PrerequisiteEdge:
    pattern_id: UUID
    requires_pattern_id: UUID
    strength: float

    @property
    def gates(self) -> bool:
        return self.strength >= tuning.PREREQUISITE_GATING_STRENGTH_MIN


class PrerequisiteState(StrEnum):
    """Three states, not two — the distinction the gate depends on.

    Treating `unknown` as a failure deadlocks a new user: with no evidence
    nothing is calibrated, so nearly every pattern locks and the scheduler has
    almost nothing to draw on precisely when it is needed most.
    """

    DEMONSTRATED = "demonstrated"
    REFUTED = "refuted"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class UnlockState:
    unlocked: bool
    blocked_by: tuple[UUID, ...] = ()
    #: Prerequisites with no verdict yet. Unlocked on this basis is *provisional*.
    unknown_prerequisites: tuple[UUID, ...] = ()
    reason: str = ""

    @property
    def provisional(self) -> bool:
        """Unlocked only because prerequisites are unproven, not because they passed.

        The scheduler deprioritises these; they are never presented as earned.
        """
        return self.unlocked and bool(self.unknown_prerequisites)


def prerequisite_state(prediction: Prediction | None) -> PrerequisiteState:
    """Classify a prerequisite.

    An uncalibrated estimate is not evidence in either direction — a confident
    guess is still a guess, and so is a pessimistic one.
    """
    if prediction is None or not prediction.calibrated:
        return PrerequisiteState.UNKNOWN
    if prediction.score >= tuning.PREREQUISITE_READINESS_MIN:
        return PrerequisiteState.DEMONSTRATED
    return PrerequisiteState.REFUTED


def prerequisite_met(prediction: Prediction | None) -> bool:
    """Whether a prerequisite has actually been demonstrated."""
    return prerequisite_state(prediction) is PrerequisiteState.DEMONSTRATED


def evaluate_unlocks(
    pattern_ids: set[UUID],
    edges: list[PrerequisiteEdge],
    predictions: dict[UUID, Prediction],
) -> dict[UUID, UnlockState]:
    """Which patterns may be scheduled, and what is holding the rest back.

    Roots — patterns with no gating prerequisites — are always unlocked. You have
    to be allowed to start somewhere.
    """
    gating: dict[UUID, list[UUID]] = defaultdict(list)
    for edge in edges:
        if edge.gates:
            gating[edge.pattern_id].append(edge.requires_pattern_id)

    result: dict[UUID, UnlockState] = {}
    for pattern_id in pattern_ids:
        required = gating.get(pattern_id, [])
        if not required:
            result[pattern_id] = UnlockState(unlocked=True, reason="no prerequisites")
            continue

        blocked = tuple(req for req in required if not prerequisite_met(predictions.get(req)))
        if blocked:
            result[pattern_id] = UnlockState(
                unlocked=False,
                blocked_by=blocked,
                reason=f"{len(blocked)} of {len(required)} prerequisites not yet demonstr