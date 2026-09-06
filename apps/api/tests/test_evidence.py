"""Evidence derivation — what counts, and how much (spec §6.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from dsa_coach import tuning
from dsa_coach.mechanism.evidence import (
    AttemptFacts,
    Evidence,
    ProblemRef,
    counted_attempt_index,
    outcome_score,
    split_across_patterns,
    to_evidence,
)
from dsa_coach.models import CaptureConfidence, Resolution, SubmissionOutcome

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
PROBLEM = ProblemRef(rating=1500, rating_rd=75)


def facts(**overrides: object) -> AttemptFacts:
    base: dict[str, object] = {
        "attempt_id": uuid4(),
        "problem_id": uuid4(),
        "problem": PROBLEM,
        "resolution": Resolution.INDEPENDENT,
        "submission_outcome": SubmissionOutcome.ACCEPTED,
        "capture_confidence": CaptureConfidence.HIGH,
        "is_resolve": False,
        "submitted_at": NOW,
        "prior_attempt_times": (),
    }
    base.update(overrides)
    return AttemptFacts(**base)  # type: ignore[arg-type]


class TestOutcomeScore:
    @pytest.mark.parametrize(
        ("resolution", "expected"),
        [
            (Resolution.INDEPENDENT, 1.0),
            (Resolution.AFTER_HINT, 0.6),
            (Resolution.AFTER_EDITORIAL, 0.25),
            (Resolution.FAILED, 0.0),
        ],
    )
    def test_reported_resolutions_score_at_full_weight(
        self, resolution: Resolution, expected: float
    ) -> None:
        score, multiplier = outcome_score(resolution, SubmissionOutcome.ACCEPTED)

        assert score == expected
        assert multiplier == 1.0

    def test_hint_scores_above_editorial(self) -> None:
        """Recognition is the harder, more transferable half of the skill."""
        hint, _ = outcome_score(Resolution.AFTER_HINT, SubmissionOutcome.ACCEPTED)
        editorial, _ = outcome_score(Resolution.AFTER_EDITORIAL, SubmissionOutcome.ACCEPTED)

        assert hint > editorial

    def test_dismissed_questionnaire_falls_back_at_reduced_weight(self) -> None:
        """A dismissal is missing information, not a failure (spec §3.2)."""
        score, multiplier = outcome_score(Resolution.UNKNOWN, SubmissionOutcome.ACCEPTED)

        assert score == tuning.DISMISSED_ACCEPTED_SCORE
        assert multiplier == tuning.DISMISSED_WEIGHT_MULTIPLIER

    def test_dismissed_failure_scores_low(self) -> None:
        score, _ = outcome_score(Resolution.UNKNOWN, SubmissionOutcome.WRONG_ANSWER)

        assert score == tuning.DISMISSED_FAILED_SCORE


class TestWhatCounts:
    def test_a_resolve_is_never_readiness_evidence(self) -> None:
        """Review must not inflate skill (spec §6.2)."""
        assert to_evidence(facts(is_resolve=True)) is None

    def test_a_first_attempt_counts(self) -> None:
        evidence = to_evidence(facts())

        assert evidence is not None
        assert evidence.score == 1.0
        assert evidence.weight == 1.0

    def test_second_attempt_in_the_same_sitting_does_not_count(self) -> None:
        """Two goes at one problem in an afternoon is one piece of evidence."""
        result = to_evidence(facts(prior_attempt_times=(NOW - timedelta(hours=1),)))

        assert result is None

    def test_a_later_sitting_counts_but_at_reduced_weight(self) -> None:
        earlier = NOW - timedelta(days=3)
        evidence = to_evidence(facts(prior_attempt_times=(earlier,)))

        assert evidence is not None
        assert evidence.weight < 1.0

    def test_a_problem_stops_counting_after_the_cap(self) -> None:
        sittings = tuple(
            NOW - timedelta(days=d) for d in range(1, tuning.MAX_COUNTED_ATTEMPTS_PER_PROBLEM + 1)
        )

        assert to_evidence(facts(prior_attempt_times=sittings)) is None

    @pytest.mark.parametrize(
        ("confidence", "expected"),
        [
            (CaptureConfidence.HIGH, 1.0),
            (CaptureConfidence.MEDIUM, 0.7),
            (CaptureConfidence.LOW, 0.4),
        ],
    )
    def test_capture_confidence_sets_the_weight(
        self, confidence: CaptureConfidence, expected: float
    ) -> None:
        evidence = to_evidence(facts(capture_confidence=confidence))

        assert evidence is not None
        assert evidence.weight == pytest.approx(expected)

    def test_counted_index_ignores_attempts_clustered_in_one_sitting(self) -> None:
        cluster = (NOW - timedelta(days=5), NOW - timedelta(days=5, hours=1))

        assert counted_attempt_index(facts(prior_attempt_times=cluster)) == 1


class TestPatternSplit:
    def test_weight_is_divided_not_duplicated(self) -> None:
        """A 0.7/0.3 problem is not a full observation about both patterns."""
        evidence = Evidence(score=1.0, weight=1.0, problem=PROBLEM, at=NOW)
        a, b = uuid4(), uuid4()

        split = split_across_patterns(evidence, {a: 0.7, b: 0.3})

        assert split[a].weight == pytest.approx(0.7)
        assert split[b].weight == pytest.approx(0.3)
        assert sum(e.weight for e in split.values()) == pytest.approx(1.0)

    def test_score_is_shared_across_patterns(self) -> None:
        evidence = Evidence(score=0.6, weight=1.0, problem=PROBLEM, at=NOW)
        a, b = uuid4(), uuid4()

        split = split_across_patterns(evidence, {a: 0.5, b: 0.5})

        assert all(e.score == 0.6 for e in split.values())
