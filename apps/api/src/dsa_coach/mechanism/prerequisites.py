"""Prerequisite gating over the pattern DAG (spec §6.4).

Pure graph evaluation over three prerequisite states, not two:

- **demonstrated** — calibrated and at the required level. Unlocks.
- **refuted** — calibrated and below it. Locks.
- **unknown** — not calibrated. Unlocks *provisionally*.

The three-way split is what keeps a new user moving. Treating `unknown` as a
failure locks nearly every pattern before any evidence exists, leaving the
scheduler weakest exactly when it is needed most. Treating it as a pass would be
worse — it would let a guess count as a demonstration. Provisional unlocking says
what is actually true: nothing is known yet, so the pattern is available but is
never chosen as a focus.

Weak edges (below the gating strength) are advisory ordering only; they influence
curriculum sequence but never block scheduling.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
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

        states = {req: prerequisite_state(predictions.get(req)) for req in required}
        refuted = tuple(r for r, s in states.items() if s is PrerequisiteState.REFUTED)
        unknown = tuple(r for r, s in states.items() if s is PrerequisiteState.UNKNOWN)

        # Only a *refuted* prerequisite locks. Unknown ones let the pattern
        # through provisionally, so a new user is never stuck with an empty plan
        # waiting for evidence they have no way to produce.
        if refuted:
            result[pattern_id] = UnlockState(
                unlocked=False,
                blocked_by=refuted,
                unknown_prerequisites=unknown,
                reason=(
                    f"{len(refuted)} of {len(required)} prerequisites attempted and "
                    "not yet at the required level"
                ),
            )
        elif unknown:
            result[pattern_id] = UnlockState(
                unlocked=True,
                unknown_prerequisites=unknown,
                reason=(
                    f"{len(unknown)} of {len(required)} prerequisites still unproven "
                    "— available, but not prioritised"
                ),
            )
        else:
            result[pattern_id] = UnlockState(unlocked=True, reason="all prerequisites demonstrated")

    return result


def topological_order(pattern_ids: set[UUID], edges: list[PrerequisiteEdge]) -> list[UUID]:
    """Patterns ordered so prerequisites come first. Deterministic.

    Raises ValueError on a cycle: a cyclic prerequisite graph is a catalogue bug
    that would otherwise lock every pattern involved forever.
    """
    requires: dict[UUID, set[UUID]] = defaultdict(set)
    for edge in edges:
        if edge.pattern_id in pattern_ids and edge.requires_pattern_id in pattern_ids:
            requires[edge.pattern_id].add(edge.requires_pattern_id)

    ordered: list[UUID] = []
    placed: set[UUID] = set()
    # Sorted for determinism — UUID order is arbitrary but stable.
    remaining = sorted(pattern_ids, key=str)

    while remaining:
        ready = [p for p in remaining if requires[p] <= placed]
        if not ready:
            raise ValueError(f"cycle in pattern prerequisites among {len(remaining)} patterns")
        for pattern_id in ready:
            ordered.append(pattern_id)
            placed.add(pattern_id)
        remaining = [p for p in remaining if p not in placed]

    return ordered
