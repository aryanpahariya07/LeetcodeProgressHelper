"""Retention scheduling (spec §6.5).

The central claim under test: intervals follow *demonstrated recall*, never
self-reported confidence.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fsrs import Rating

from dsa_coach import tuning
from dsa_coach.mechanism.retention import (
    ResolvePerformance,
    apply_review,
    derive_grade,
    is_lapsed,
    schedule_first_solve,
)

NOW = datetime(2026, 3, 1, tzinfo=UTC)


def perf(**overrides: object) -> ResolvePerformance:
    base: dict[str, object] = {
        "solved": True,
        "used_help": False,
        "submit_count": 1,
        "active_seconds": 600,
        "baseline_active_seconds": 1200,
    }
    base.update(overrides)
    return ResolvePerformance(**base)  # type: ignore[arg-type]


class TestGradeDerivation:
    def test_failing_to_reproduce_is_again(self) -> None:
        assert derive_grade(perf(solved=False)) is Rating.Again

    def test_much_faster_than_first_time_is_easy(self) -> None:
        # 300 / 1200 = 0.25, below the easy ratio.
        assert derive_grade(perf(active_seconds=300)) is Rating.Easy

    def test_nearly_as_slow_as_first_time_is_hard(self) -> None:
        # 1100 / 1200 = 0.92, above the hard ratio.
        assert derive_grade(perf(active_seconds=1100)) is Rating.Hard

    def test_middling_speed_is_good(self) -> None:
        assert derive_grade(perf(active_seconds=700)) is Rating.Good

    def test_needing_multiple_submissions_caps_the_grade(self) -> None:
        """Fast but scrappy did not come back cleanly."""
        assert derive_grade(perf(active_seconds=300, submit_count=3)) is Rating.Hard

    def test_help_reduces_the_grade_by_one(self) -> None:
        without = derive_grade(perf(active_seconds=300))
        with_help = derive_grade(perf(active_seconds=300, used_help=True))

        assert without is Rating.Easy
        assert with_help is Rating.Good

    def test_help_cannot_push_below_again(self) -> None:
        assert derive_grade(perf(solved=False, used_help=True)) is Rating.Again

    def test_missing_timing_is_neutral_not_guessed(self) -> None:
        """Invariant 6: absent data is absent, not inferred."""
        assert derive_grade(perf(active_seconds=None)) is Rating.Good
        assert derive_grade(perf(baseline_active_seconds=None)) is Rating.Good

    def test_confidence_is_not_an_input(self) -> None:
        """Spec §6.5 — the whole correction. Confidence has no field here."""
        assert not hasattr(ResolvePerformance, "confidence_cold_redo")
        assert "confidence" not in ResolvePerformance.__dataclass_fields__


class TestScheduling:
    def test_first_solve_schedules_a_future_review(self) -> None:
        state = schedule_first_solve(NOW)

        assert state.due_at > NOW
        assert state.reps == 1
        assert state.lapses == 0
        assert state.fsrs_version == tuning.FSRS_VERSION

    def test_successful_reviews_lengthen_the_interval(self) -> None:
        state = schedule_first_solve(NOW)
        first_gap = (state.due_at - NOW).days

        state, _ = apply_review(state, perf(active_seconds=400), state.due_at)
        second_gap = (state.due_at - NOW).days

        assert second_gap > first_gap

    def test_a_lapse_shortens_the_interval_and_is_counted(self) -> None:
        state = schedule_first_solve(NOW)
        state, _ = apply_review(state, perf(active_seconds=400), state.due_at)
        long_gap = state.due_at - state.last_reviewed_at  # type: ignore[operator]

        lapsed, grade = apply_review(state, perf(solved=False), state.due_at)
        short_gap = lapsed.due_at - lapsed.last_reviewed_at  # type: ignore[operator]

        assert grade is Rating.Again
        assert short_gap < long_gap
        assert lapsed.lapses == 1

    def test_reps_accumulate(self) -> None:
        state = schedule_first_solve(NOW)
        for _ in range(3):
            state, _ = apply_review(state, perf(active_seconds=400), state.due_at)

        assert state.reps == 4

    def test_intervals_are_day_scale_not_minutes(self) -> None:
        """Learning steps are disabled: a re-solve is a whole problem."""
        state = schedule_first_solve(NOW)

        assert (state.due_at - NOW).days >= 1

    def test_scheduling_is_deterministic(self) -> None:
        """Fuzzing must stay off — the mechanism layer is reproducible."""

        def run() -> list[str]:
            state = schedule_first_solve(NOW)
            out = []
            for _ in range(5):
                state, _ = apply_review(state, perf(active_seconds=400), state.due_at)
                out.append(state.due_at.isoformat())
            return out

        assert run() == run()

    @pytest.mark.parametrize(
        ("grade", "expected"),
        [(Rating.Again, True), (Rating.Hard, True), (Rating.Good, False), (Rating.Easy, False)],
    )
    def test_lapse_classification(self, grade: Rating, expected: bool) -> None:
        assert is_lapsed(grade) is expected
