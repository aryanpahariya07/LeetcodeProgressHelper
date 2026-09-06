"""Turning an attempt into evidence.

Pure functions. Given an attempt's facts, produce the score and weight the
readiness models consume — and, importantly, decide whether it counts at all.

Spec §6.2's required adjustments live here rather than inside either model, so
both models see identically-derived evidence and the §6.3 comparison is fair.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from dsa_coach import tuning
from dsa_coach.models import CaptureConfidence, Resolution, SubmissionOutcome


@dataclass(frozen=True)
class ProblemRef:
    """The facts about a problem that the readiness models may use."""

    rating: int
    rating_rd: int


@dataclass(frozen=True)
class Evidence:
    """One attempt, reduced to what moves a readiness estimate."""

    score: float
    weight: float
    problem: ProblemRef
    at: datetime

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError(f"score out of range: {self.score}")
        if not 0.0 <= self.weight <= 1.0:
            raise ValueError(f"weight out of range: {self.weight}")


@dataclass(frozen=True)
class AttemptFacts:
    """Everything needed to decide whether an attempt is evidence, and how much."""

    attempt_id: UUID
    problem_id: UUID
    problem: ProblemRef
    resolution: Resolution
    submission_outcome: SubmissionOutcome
    capture_confidence: CaptureConfidence
    is_resolve: bool
    submitted_at: datetime
    #: Prior attempts on this same problem, most recent first.
    prior_attempt_times: tuple[datetime, ...] = ()


def outcome_score(
    resolution: Resolution, submission_outcome: SubmissionOutcome
) -> tuple[float, float]:
    """Score in [0, 1] and a weight multiplier.

    A dismissed questionnaire is not a failure — it is an absence of information,
    so it falls back to the judge result at reduced weight (spec §6.2).
    """
    if resolution is not Resolution.UNKNOWN:
        return tuning.OUTCOME_SCORES[resolution.value], 1.0

    accepted = submission_outcome is SubmissionOutcome.ACCEPTED
    score = tuning.DISMISSED_ACCEPTED_SCORE if accepted else tuning.DISMISSED_FAILED_SCORE
    return score, tuning.DISMISSED_WEIGHT_MULTIPLIER


def counted_attempt_index(facts: AttemptFacts) -> int | None:
    """How many times this problem has already counted, or None if this one does not.

    Repeated attempts at one problem are capped, and attempts inside a single
    sitting collapse to one (spec §6.2).
    """
    window = timedelta(hours=tuning.SESSION_WINDOW_HOURS)
    distinct_sittings = 0
    last: datetime | None = None

    for prior in sorted(facts.prior_attempt_times):
        if last is None or prior - last > window:
            distinct_sittings += 1
            last = prior

    if last is not None and facts.submitted_at - last <= window:
        # Same sitting as an attempt already counted.
        return None
    if distinct_sittings >= tuning.MAX_COUNTED_ATTEMPTS_PER_PROBLEM:
        return None
    return distinct_sittings


def to_evidence(facts: AttemptFacts) -> Evidence | None:
    """Reduce an attempt to readiness evidence, or None if it must not count.

    Returns None for re-solves: those are retention evidence and updating
    readiness with them would let review inflate skill (spec §6.2).
    """
    if facts.is_resolve:
        return None

    index = counted_attempt_index(facts)
    if index is None:
        return None

    score, resolution_multiplier = outcome_score(facts.resolution, facts.submission_outcome)
    weight = tuning.CONFIDENCE_WEIGHTS[facts.capture_confidence.value] * resolution_multiplier

    # A second or third look at the same problem is weaker evidence than the first.
    weight *= 1.0 / (index + 1)

    return Evidence(
        score=score,
        weight=min(weight, 1.0),
        problem=facts.problem,
        at=facts.submitted_at,
    )


def split_across_patterns(
    evidence: Evidence, pattern_weights: dict[UUID, float]
) -> dict[UUID, Evidence]:
    """Split one attempt's evidence across the problem's patterns by weight.

    A problem that is 0.7 sliding-window and 0.3 hashmap is 0.7 of an observation
    about the first and 0.3 about the second — never a full observation about both,
    which would manufacture evidence from nothing.
    """
    return {
        pattern_id: Evidence(
            score=evidence.score,
            weight=evidence.weight * pattern_weight,
            problem=evidence.problem,
            at=evidence.at,
        )
        for pattern_id, pattern_weight in pattern_weights.items()
    }
