"""Trigger relevance and material-change detection (spec §6.6)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from dsa_coach import tuning
from dsa_coach.mechanism.readiness.base import Prediction
from dsa_coach.mechanism.triggers import (
    BlockerObservation,
    RelevanceFacts,
    detect_material_change,
    is_relevant,
)
from dsa_coach.models import Blocker, Resolution

PATTERN = uuid4()
NAMES = {PATTERN: "sliding-window"}


def settled(score: float) -> Prediction:
    return Prediction(score=score, uncertainty=0.05)


def unchanged(score: float = 0.5) -> dict:
    return {PATTERN: settled(score)}


class TestRelevance:
    def test_a_first_exposure_is_relevant(self) -> None:
        assert is_relevant(RelevanceFacts(is_resolve=False, resolution=Resolution.INDEPENDENT))

    def test_a_clean_resolve_is_not_relevant(self) -> None:
        """A review-heavy day must not fire evaluations on nothing new."""
        facts = RelevanceFacts(
            is_resolve=True, resolution=Resolution.INDEPENDENT, resolve_lapsed=False
        )

        assert not is_relevant(facts)

    def test_a_lapsed_resolve_is_relevant(self) -> None:
        """Forgetting something is genuinely new information."""
        facts = RelevanceFacts(is_resolve=True, resolution=Resolution.FAILED, resolve_lapsed=True)

        assert is_relevant(facts)

    def test_a_repeat_within_one_sitting_is_not_relevant(self) -> None:
        facts = RelevanceFacts(
            is_resolve=False,
            resolution=Resolution.FAILED,
            already_counted_this_sitting=True,
        )

        assert not is_relevant(facts)

    def test_a_failed_first_exposure_is_still_relevant(self) -> None:
        assert is_relevant(RelevanceFacts(is_resolve=False, resolution=Resolution.FAILED))


class TestMaterialChange:
    def test_stable_evidence_produces_an_explained_no_change(self) -> None:
        """Silence looks broken; an explained no-change looks like it is working."""
        result = detect_material_change(
            before=unchanged(0.5),
            after=unchanged(0.5),
            unlocked_before=set(),
            unlocked_after=set(),
            recent_blockers=[],
        )

        assert not result.material
        assert result.reasons == ()
        assert "no change" in result.explanation.lower()
        assert "consistent with the current plan" in result.explanation

    def test_small_movement_is_not_material(self) -> None:
        drift = tuning.MATERIAL_READINESS_DELTA / 2
        result = detect_material_change(
            before=unchanged(0.50),
            after=unchanged(0.50 + drift),
            unlocked_before=set(),
            unlocked_after=set(),
            recent_blockers=[],
        )

        assert not result.material

    def test_large_movement_is_material_and_explained(self) -> None:
        result = detect_material_change(
            before=unchanged(0.40),
            after=unchanged(0.60),
            unlocked_before=set(),
            unlocked_after=set(),
            recent_blockers=[],
            pattern_names=NAMES,
        )

        assert result.material
        assert any("sliding-window" in r and "rose" in r for r in result.reasons)

    def test_a_fall_is_reported_as_a_fall(self) -> None:
        result = detect_material_change(
            before=unchanged(0.70),
            after=unchanged(0.45),
            unlocked_before=set(),
            unlocked_after=set(),
            recent_blockers=[],
            pattern_names=NAMES,
        )

        assert any("fell" in r for r in result.reasons)

    def test_becoming_calibrated_is_material(self) -> None:
        result = detect_material_change(
            before={PATTERN: Prediction(score=0.5, uncertainty=0.9)},
            after={PATTERN: Prediction(score=0.5, uncertainty=0.02)},
            unlocked_before=set(),
            unlocked_after=set(),
            recent_blockers=[],
            pattern_names=NAMES,
        )

        assert result.material
        assert any("became calibrated" in r for r in result.reasons)

    def test_a_newly_unlocked_pattern_is_material(self) -> None:
        result = detect_material_change(
            before=unchanged(),
            after=unchanged(),
            unlocked_before=set(),
            unlocked_after={PATTERN},
            recent_blockers=[],
            pattern_names=NAMES,
        )

        assert result.material
        assert any("unlocked" in r for r in result.reasons)

    def test_repeated_identical_blockers_are_material_on_their_own(self) -> None:
        """Readiness can barely move while the reason for failing stays identical."""
        observations = [
            BlockerObservation(PATTERN, Blocker.PATTERN_KNOWN_IMPL_FAILED, Resolution.FAILED)
            for _ in range(tuning.MATERIAL_REPEATED_BLOCKER_COUNT)
        ]

        result = detect_material_change(
            before=unchanged(),
            after=unchanged(),
            unlocked_before=set(),
            unlocked_after=set(),
            recent_blockers=observations,
            pattern_names=NAMES,
        )

        assert result.material
        assert any("pattern_known_impl_failed" in r for r in result.reasons)

    def test_two_identical_blockers_are_not_yet_material(self) -> None:
        observations = [
            BlockerObservation(PATTERN, Blocker.EDGE_CASES, Resolution.FAILED) for _ in range(2)
        ]

        result = detect_material_change(
            before=unchanged(),
            after=unchanged(),
            unlocked_before=set(),
            unlocked_after=set(),
            recent_blockers=observations,
        )

        assert not result.material

    def test_blockers_on_solved_attempts_are_ignored(self) -> None:
        observations = [
            BlockerObservation(PATTERN, Blocker.EDGE_CASES, Resolution.INDEPENDENT)
            for _ in range(5)
        ]

        result = detect_material_change(
            before=unchanged(),
            after=unchanged(),
            unlocked_before=set(),
            unlocked_after=set(),
            recent_blockers=observations,
        )

        assert not result.material

    @pytest.mark.parametrize("flag", ["block_complete", "weekly_boundary"])
    def test_structural_boundaries_are_material(self, flag: str) -> None:
        result = detect_material_change(
            before=unchanged(),
            after=unchanged(),
            unlocked_before=set(),
            unlocked_after=set(),
            recent_blockers=[],
            **{flag: True},
        )

        assert result.material

    def test_a_new_pattern_with_no_history_does_not_fire(self) -> None:
        """First evidence on an unseen pattern is not a 'change'."""
        result = detect_material_change(
            before={},
            after=unchanged(0.9),
            unlocked_before=set(),
            unlocked_after=set(),
            recent_blockers=[],
        )

        assert not result.material
