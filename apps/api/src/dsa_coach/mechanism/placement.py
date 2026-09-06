"""Adaptive placement (spec §9).

**Placement is not a test.** It is the first practice block, with its problems
chosen to be informative rather than comfortable. The user is never blocked
waiting for it: a provisional plan exists from onboarding, placement runs through
ordinary practice, and the plan sharpens as evidence arrives.

Two ideas do the work here:

- **Aim at a coin flip.** An attempt you are equally likely to pass or fail
  carries the most information about where you stand. One you would certainly
  pass, or certainly fail, tells you almost nothing.
- **Breadth before depth.** Three problems on one pattern say less than three
  problems across three patterns, when the question is "where does this person
  stand overall".

Pure functions. The service layer supplies the state.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from dsa_coach import tuning
from dsa_coach.mechanism.blocks import Candidate
from dsa_coach.mechanism.readiness.base import Prediction


@dataclass(frozen=True)
class PlacementProgress:
    """Where placement has got to, and why it is or is not finished."""

    complete: bool
    attempts: int
    max_attempts: int
    #: Foundational patterns with at least one observation.
    covered: int
    #: Foundational patterns that have enough evidence to show a band.
    calibrated: int
    target: int
    reason: str

    @property
    def remaining(self) -> int:
        return max(0, self.max_attempts - self.attempts)


def evaluate_placement(
    *,
    attempts: int,
    foundational: dict[UUID, Prediction | None],
) -> PlacementProgress:
    """Decide whether placement has seen enough.

    Two stopping conditions, per spec §9: every foundational pattern calibrated,
    or the problem ceiling. Whichever comes first — but never before the floor,
    so a short lucky run cannot end it early.
    """
    target = len(foundational)
    covered = sum(1 for p in foundational.values() if p is not None)
    calibrated = sum(1 for p in foundational.values() if p is not None and p.calibrated)

    if target == 0:
        return PlacementProgress(
            complete=True,
            attempts=attempts,
            max_attempts=tuning.PLACEMENT_MAX_PROBLEMS,
            covered=0,
            calibrated=0,
            target=0,
            reason="no foundational patterns in the catalogue",
        )

    if attempts < tuning.PLACEMENT_MIN_PROBLEMS:
        return PlacementProgress(
            complete=False,
            attempts=attempts,
            max_attempts=tuning.PLACEMENT_MAX_PROBLEMS,
            covered=covered,
            calibrated=calibrated,
            target=target,
            reason=(
                f"{tuning.PLACEMENT_MIN_PROBLEMS - attempts} more attempts before "
                "there is enough to work from"
            ),
        )

    if calibrated >= target:
        return PlacementProgress(
            complete=True,
            attempts=attempts,
            max_attempts=tuning.PLACEMENT_MAX_PROBLEMS,
            covered=covered,
            calibrated=calibrated,
            target=target,
            reason="every foundational pattern has enough evidence",
        )

    if attempts >= tuning.PLACEMENT_MAX_PROBLEMS:
        return PlacementProgress(
            complete=True,
            attempts=attempts,
            max_attempts=tuning.PLACEMENT_MAX_PROBLEMS,
            covered=covered,
            calibrated=calibrated,
            target=target,
            # Honest about what this means: enough to plan from, not enough to
            # be confident about. Patterns still read "Calibrating" until they
            # earn otherwise.
            reason=(
                "reached the placement limit — planning from the evidence so far, "
                "which is still thin in places"
            ),
        )

    return PlacementProgress(
        complete=False,
        attempts=attempts,
        max_attempts=tuning.PLACEMENT_MAX_PROBLEMS,
        covered=covered,
        calibrated=calibrated,
        target=target,
        reason=f"{calibrated} of {target} foundational patterns have enough evidence",
    )


def information_value(candidate: Candidate) -> float:
    """How much an attempt would tell us. Higher is better.

    Peaks where the predicted outcome is a coin flip and falls away towards
    certainty in either direction.
    """
    return -abs(candidate.predicted_score - tuning.PLACEMENT_TARGET_SCORE)


def select_placement_candidates(
    candidates: list[Candidate],
    *,
    observed_patterns: set[UUID],
    size: int,
) -> list[Candidate]:
    """Choose the next placement problems: breadth first, then informativeness.

    One problem per unobserved pattern before any pattern gets a second, so a
    short placement still spans the foundations rather than drilling into one.
    Deterministic: ties break on problem id.
    """
    ordered = sorted(
        candidates,
        key=lambda c: (-information_value(c), str(c.problem_id)),
    )

    chosen: list[Candidate] = []
    used_patterns = set(observed_patterns)
    taken: set[UUID] = set()

    # Pass one: cover ground.
    for candidate in ordered:
        if len(chosen) >= size:
            break
        fresh = [p for p in candidate.pattern_ids if p not in used_patterns]
        if not fresh:
            continue
        chosen.append(candidate)
        taken.add(candidate.problem_id)
        used_patterns.update(candidate.pattern_ids)

    # Pass two: fill any remaining slots with the most informative left.
    for candidate in ordered:
        if len(chosen) >= size:
            break
        if candidate.problem_id in taken:
            continue
        chosen.append(candidate)
        taken.add(candidate.problem_id)

    return chosen
