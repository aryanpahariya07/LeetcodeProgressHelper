"""Material-change detection (spec §6.6).

The user asked for the plan to be reconsidered every three attempts. Three
attempts almost never justifies changing anything, and a plan that reshuffles
every other day stops being trusted. The reconciliation: evaluate every three
relevant attempts, but only *act* when something material moved — and say so
explicitly when nothing did.

Silence looks like a broken product. An explained "no change" looks like a
working one.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from uuid import UUID

from dsa_coach import tuning
from dsa_coach.mechanism.readiness.base import Prediction
from dsa_coach.models import Blocker, Resolution


@dataclass(frozen=True)
class RelevanceFacts:
    """What decides whether an attempt advances the trigger counter."""

    is_resolve: bool
    resolution: Resolution
    #: For a re-solve: whether it came back badly (Again/Hard).
    resolve_lapsed: bool = False
    #: Whether an earlier attempt on this problem already counted this sitting.
    already_counted_this_sitting: bool = False


def is_relevant(facts: RelevanceFacts) -> bool:
    """Whether an attempt counts toward the three-attempt trigger.

    Routine successful re-solves do not: a review-heavy day would otherwise fire
    evaluations on evidence that says nothing new about skill. A *lapsed*
    re-solve does count — that is genuinely new information.
    """
    if facts.already_counted_this_sitting:
        return False
    if facts.is_resolve:
        return facts.resolve_lapsed
    return True


@dataclass(frozen=True)
class MaterialChange:
    material: bool
    reasons: tuple[str, ...]
    explanation: str


@dataclass(frozen=True)
class BlockerObservation:
    pattern_id: UUID
    blocker: Blocker | None
    resolution: Resolution


def detect_material_change(
    *,
    before: dict[UUID, Prediction],
    after: dict[UUID, Prediction],
    unlocked_before: set[UUID],
    unlocked_after: set[UUID],
    recent_blockers: list[BlockerObservation],
    block_complete: bool = False,
    weekly_boundary: bool = False,
    pattern_names: dict[UUID, str] | None = None,
) -> MaterialChange:
    """Decide whether the evidence justifies revisiting the plan."""
    names = pattern_names or {}
    reasons: list[str] = []

    def name(pattern_id: UUID) -> str:
        return names.get(pattern_id, str(pattern_id)[:8])

    for pattern_id, new in after.items():
        old = before.get(pattern_id)
        if old is None:
            continue
        delta = new.score - old.score
        if abs(delta) >= tuning.MATERIAL_READINESS_DELTA:
            direction = "rose" if delta > 0 else "fell"
            reasons.append(f"readiness on {name(pattern_id)} {direction} by {abs(delta):.2f}")

        if old.calibrated != new.calibrated:
            state = "became calibrated" if new.calibrated else "lost calibration"
            reasons.append(f"{name(pattern_id)} {state}")

    newly_unlocked = unlocked_after - unlocked_before
    for pattern_id in sorted(newly_unlocked, key=str):
        reasons.append(f"{name(pattern_id)} unlocked")

    newly_locked = unlocked_before - unlocked_after
    for pattern_id in sorted(newly_locked, key=str):
        reasons.append(f"{name(pattern_id)} became locked")

    reasons.extend(_repeated_blocker_reasons(recent_blockers, name))

    if block_complete:
        reasons.append("current block complete")
    if weekly_boundary:
        reasons.append("weekly review point")

    if reasons:
        return MaterialChange(
            material=True,
            reasons=tuple(reasons),
            explanation="Reviewed after 3 attempts — " + "; ".join(reasons) + ".",
        )

    return MaterialChange(
        material=False,
        reasons=(),
        explanation=(
            "Reviewed after 3 attempts — no change. Your results are consistent "
            "with the current plan."
        ),
    )


def _repeated_blocker_reasons(observations: list[BlockerObservation], name: object) -> list[str]:
    """The same blocker three times on one pattern is a signal on its own.

    Readiness may barely move while the *reason* for failure stays identical, and
    that pattern of failure is precisely what a coach should notice.
    """
    counts: Counter[tuple[UUID, Blocker]] = Counter()
    for observation in observations:
        if observation.blocker is None:
            continue
        if observation.resolution is Resolution.INDEPENDENT:
            continue
        counts[(observation.pattern_id, observation.blocker)] += 1

    reasons = []
    for (pattern_id, blocker), count in sorted(counts.items(), key=lambda kv: str(kv[0])):
        if count >= tuning.MATERIAL_REPEATED_BLOCKER_COUNT:
            label = name(pattern_id)  # type: ignore[operator]
            reasons.append(f"{blocker.value} on {label} {count} times running")
    return reasons
