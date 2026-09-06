"""Adaptive placement (spec §9).

The claim: a new user reaches an evidence-based plan through ordinary practice
and is **never blocked** waiting for a test to finish.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from dsa_coach import tuning
from dsa_coach.mechanism.blocks import Candidate
from dsa_coach.mechanism.placement import (
    evaluate_placement,
    information_value,
    select_placement_candidates,
)
from dsa_coach.mechanism.readiness.base import Prediction
from dsa_coach.models import Level

BASE = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


def settled(score: float) -> Prediction:
    return Prediction(score=score, uncertainty=0.05)


def unsettled(score: float = 0.5) -> Prediction:
    return Prediction(score=score, uncertainty=0.9)


def cand(score: float, patterns: tuple[uuid.UUID, ...] = ()) -> Candidate:
    return Candidate(
        problem_id=uuid.uuid4(),
        rating=1500,
        minutes=25,
        predicted_score=score,
        pattern_ids=patterns,
    )


def event(slug: str, *, day: int = 0, resolution: str = "independent") -> dict[str, object]:
    return {
        "event_uuid": str(uuid.uuid4()),
        "problem_slug": slug,
        "submitted_at": (BASE + timedelta(days=day)).isoformat(),
        "resolution": resolution,
        "submission_outcome": "accepted" if resolution == "independent" else "wrong_answer",
        "active_seconds": 900,
    }


async def log(client: AsyncClient, payload: dict[str, object]) -> None:
    response = await client.post("/attempts", json=payload)
    assert response.status_code == 201, response.text


class TestStopping:
    def test_placement_does_not_end_before_the_floor(self) -> None:
        """A short lucky run must not end placement after two problems."""
        patterns = {uuid.uuid4(): settled(0.8) for _ in range(3)}

        result = evaluate_placement(attempts=2, foundational=patterns)

        assert not result.complete
        assert "more attempts" in result.reason

    def test_placement_ends_when_every_foundation_is_calibrated(self) -> None:
        patterns = {uuid.uuid4(): settled(0.7) for _ in range(3)}

        result = evaluate_placement(attempts=6, foundational=patterns)

        assert result.complete
        assert "enough evidence" in result.reason

    def test_placement_ends_at_the_ceiling_even_if_uncertain(self) -> None:
        """The ceiling stops the opening week feeling like an exam."""
        patterns = {uuid.uuid4(): unsettled() for _ in range(5)}

        result = evaluate_placement(attempts=tuning.PLACEMENT_MAX_PROBLEMS, foundational=patterns)

        assert result.complete
        assert "still thin" in result.reason, "honest about what the ceiling means"

    def test_placement_continues_between_the_floor_and_the_ceiling(self) -> None:
        patterns = {uuid.uuid4(): unsettled() for _ in range(4)}

        result = evaluate_placement(attempts=6, foundational=patterns)

        assert not result.complete
        assert result.remaining == tuning.PLACEMENT_MAX_PROBLEMS - 6

    def test_an_uncovered_pattern_is_distinguished_from_an_uncertain_one(self) -> None:
        a, b = uuid.uuid4(), uuid.uuid4()

        result = evaluate_placement(attempts=6, foundational={a: unsettled(), b: None})

        assert result.covered == 1
        assert result.calibrated == 0
        assert result.target == 2

    def test_a_catalogue_with_no_foundations_does_not_hang(self) -> None:
        result = evaluate_placement(attempts=0, foundational={})

        assert result.complete


class TestSelection:
    def test_a_coin_flip_is_the_most_informative(self) -> None:
        assert information_value(cand(0.5)) > information_value(cand(0.8))
        assert information_value(cand(0.5)) > information_value(cand(0.2))

    def test_certainty_in_either_direction_teaches_nothing(self) -> None:
        assert information_value(cand(0.99)) == pytest.approx(information_value(cand(0.01)))

    def test_selection_prefers_uncertain_problems(self) -> None:
        chosen = select_placement_candidates(
            [cand(0.95), cand(0.52), cand(0.05)],
            observed_patterns=set(),
            size=1,
            target_rating=1500,
        )

        assert chosen[0].predicted_score == 0.52

    def test_selection_covers_new_patterns_before_repeating_one(self) -> None:
        """Three problems across three patterns say more than three on one."""
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        pool = [
            cand(0.50, (a,)),
            cand(0.51, (a,)),
            cand(0.52, (a,)),
            cand(0.70, (b,)),
            cand(0.75, (c,)),
        ]

        chosen = select_placement_candidates(
            pool, observed_patterns=set(), size=3, target_rating=1500
        )
        covered = {p for item in chosen for p in item.pattern_ids}

        assert covered == {a, b, c}

    def test_already_observed_patterns_do_not_count_as_fresh_ground(self) -> None:
        a, b = uuid.uuid4(), uuid.uuid4()
        pool = [cand(0.50, (a,)), cand(0.80, (b,))]

        chosen = select_placement_candidates(
            pool, observed_patterns={a}, size=1, target_rating=1500
        )

        assert chosen[0].pattern_ids == (b,)

    def test_remaining_slots_fall_back_to_informativeness(self) -> None:
        a = uuid.uuid4()
        pool = [cand(0.50, (a,)), cand(0.52, (a,)), cand(0.95, (a,))]

        chosen = select_placement_candidates(
            pool, observed_patterns=set(), size=2, target_rating=1500
        )

        assert len(chosen) == 2
        assert all(c.predicted_score < 0.9 for c in chosen)

    def test_selection_is_deterministic(self) -> None:
        pool = [cand(0.4 + i / 100, (uuid.uuid4(),)) for i in range(10)]

        first = select_placement_candidates(
            pool, observed_patterns=set(), size=4, target_rating=1500
        )
        second = select_placement_candidates(
            pool, observed_patterns=set(), size=4, target_rating=1500
        )

        assert [c.problem_id for c in first] == [c.problem_id for c in second]

    def test_an_empty_pool_yields_nothing_rather_than_failing(self) -> None:
        assert (
            select_placement_candidates([], observed_patterns=set(), size=5, target_rating=1500)
            == []
        )

    def test_never_returns_the_same_problem_twice(self) -> None:
        a = uuid.uuid4()
        pool = [cand(0.5, (a,)), cand(0.6, (a,))]

        chosen = select_placement_candidates(
            pool, observed_patterns=set(), size=5, target_rating=1500
        )

        assert len({c.problem_id for c in chosen}) == len(chosen)


class TestPlacementFlow:
    async def test_a_new_user_is_placing(self, onboarded: AsyncClient) -> None:
        placement = (await onboarded.get("/progress/placement")).json()

        assert placement["complete"] is False
        assert placement["attempts"] == 0
        assert placement["max_attempts"] == tuning.PLACEMENT_MAX_PROBLEMS

    async def test_a_placing_user_still_gets_a_usable_block(self, onboarded: AsyncClient) -> None:
        """Spec §9: placement never blocks the user from doing something useful."""
        result = (await onboarded.post("/plan/next-block")).json()

        assert len(result["plan"]["items"]) > 0
        assert result["placement"]["complete"] is False

    async def test_the_placement_block_is_labelled_honestly(self, onboarded: AsyncClient) -> None:
        result = (await onboarded.post("/plan/next-block")).json()

        assert result["plan"]["status"] == "provisional"
        assert "working out where you stand" in result["plan"]["summary"]

    async def test_the_placement_block_spans_several_patterns(self, onboarded: AsyncClient) -> None:
        """Breadth is the whole point — one pattern would tell us much less."""
        result = (await onboarded.post("/plan/next-block")).json()
        slugs = [i["problem"]["slug"] for i in result["plan"]["items"]]

        assert len(set(slugs)) == len(slugs)
        assert len(slugs) >= 2

    async def test_attempts_advance_placement(self, onboarded: AsyncClient) -> None:
        for day, slug in enumerate(["two-sum", "valid-palindrome"]):
            await log(onboarded, event(slug, day=day))

        placement = (await onboarded.get("/progress/placement")).json()

        assert placement["attempts"] == 2
        assert placement["covered"] >= 2

    async def test_grinding_one_problem_does_not_advance_placement(
        self, onboarded: AsyncClient
    ) -> None:
        """Placement is asking about breadth, not persistence."""
        for day in range(4):
            await log(onboarded, event("two-sum", day=day * 10))

        placement = (await onboarded.get("/progress/placement")).json()

        assert placement["attempts"] == 1

    async def test_placement_completes_at_the_ceiling(self, onboarded: AsyncClient) -> None:
        slugs = [
            "two-sum",
            "contains-duplicate",
            "valid-palindrome",
            "binary-search",
            "valid-parentheses",
            "max-consecutive-ones",
            "move-zeroes",
            "climbing-stairs",
            "merge-sorted-array",
            "reverse-linked-list",
            "invert-binary-tree",
            "maximum-average-subarray-i",
        ]
        for day, slug in enumerate(slugs):
            await log(onboarded, event(slug, day=day))

        placement = (await onboarded.get("/progress/placement")).json()

        assert placement["attempts"] >= tuning.PLACEMENT_MAX_PROBLEMS
        assert placement["complete"] is True

    async def test_a_completed_placement_produces_an_active_plan(
        self, onboarded: AsyncClient
    ) -> None:
        """The transition the phase exists for: provisional -> evidence-based."""
        slugs = [
            "two-sum",
            "contains-duplicate",
            "valid-palindrome",
            "binary-search",
            "valid-parentheses",
            "max-consecutive-ones",
            "move-zeroes",
            "climbing-stairs",
            "merge-sorted-array",
            "reverse-linked-list",
            "invert-binary-tree",
            "maximum-average-subarray-i",
        ]
        for day, slug in enumerate(slugs):
            await log(onboarded, event(slug, day=day))

        result = (await onboarded.post("/plan/next-block")).json()

        assert result["plan"]["status"] == "active"
        assert result["plan"]["generation_context"]["generator"] == (
            "deterministic_block_assembler"
        )
        assert result["placement"] is None

    async def test_uncertainty_falls_as_evidence_arrives(self, onboarded: AsyncClient) -> None:
        """Spec §16 Phase 3 exit: uncertainty demonstrably falls with evidence."""

        async def hashmap_uncertainty() -> float:
            report = (await onboarded.get("/progress/readiness")).json()
            row = next(p for p in report["patterns"] if p["slug"] == "hashmap-counting")
            return float(row["uncertainty"])

        before = await hashmap_uncertainty()
        for day, slug in enumerate(["two-sum", "contains-duplicate", "group-anagrams"]):
            await log(onboarded, event(slug, day=day))
        after = await hashmap_uncertainty()

        assert after < before


class TestStartingLevel:
    """Spec §9: placement starts near the rating implied by self-report.

    At cold start every pattern carries the same prior, so every candidate scores
    as equally informative. Without this tie-break the choice fell to arbitrary
    id order — and once handed a beginner a single 1900-rated problem that
    consumed the entire daily budget.
    """

    def test_a_beginner_is_pointed_at_easier_problems(self) -> None:
        a, b = uuid.uuid4(), uuid.uuid4()
        easy = Candidate(uuid.uuid4(), 1150, 18, predicted_score=0.35, pattern_ids=(a,))
        hard = Candidate(uuid.uuid4(), 1950, 60, predicted_score=0.35, pattern_ids=(b,))

        chosen = select_placement_candidates(
            [hard, easy],
            observed_patterns=set(),
            size=1,
            target_rating=tuning.PLACEMENT_START_RATING[Level.BEGINNER],
        )

        assert chosen[0].rating == 1150

    def test_an_advanced_user_is_pointed_higher(self) -> None:
        a, b = uuid.uuid4(), uuid.uuid4()
        easy = Candidate(uuid.uuid4(), 1150, 18, predicted_score=0.65, pattern_ids=(a,))
        harder = Candidate(uuid.uuid4(), 1700, 40, predicted_score=0.65, pattern_ids=(b,))

        chosen = select_placement_candidates(
            [easy, harder],
            observed_patterns=set(),
            size=1,
            target_rating=tuning.PLACEMENT_START_RATING[Level.ADVANCED],
        )

        assert chosen[0].rating == 1700

    def test_informativeness_still_outranks_the_starting_rating(self) -> None:
        """The tie-break only applies to ties."""
        a, b = uuid.uuid4(), uuid.uuid4()
        on_target_but_certain = Candidate(
            uuid.uuid4(), 1200, 20, predicted_score=0.98, pattern_ids=(a,)
        )
        off_target_but_uncertain = Candidate(
            uuid.uuid4(), 1900, 55, predicted_score=0.50, pattern_ids=(b,)
        )

        chosen = select_placement_candidates(
            [on_target_but_certain, off_target_but_uncertain],
            observed_patterns=set(),
            size=1,
            target_rating=1200,
        )

        assert chosen[0].predicted_score == 0.50
