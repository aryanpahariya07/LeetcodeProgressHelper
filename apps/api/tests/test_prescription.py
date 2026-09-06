"""Prescription validation and clamping (spec §7.2).

This is the boundary that makes invariant 3 hold. Every test here is a way the
coach could be wrong — hallucinating a pattern, reaching past a prerequisite,
asking for more than the day allows, quietly removing interleaving — and the
answer in each case is that the mechanism layer notices and says so.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from dsa_coach import tuning
from dsa_coach.mechanism.prescription import (
    CatalogueFacts,
    PatternWeight,
    Prescription,
    Violation,
    validate,
)
from dsa_coach.models import ValidationResult

KNOWN = uuid4()
LOCKED = uuid4()
PROVISIONAL = uuid4()
STRANGER = uuid4()


def facts(**overrides: object) -> CatalogueFacts:
    base: dict[str, object] = {
        "known_patterns": frozenset({KNOWN, LOCKED, PROVISIONAL}),
        "unlocked_patterns": frozenset({KNOWN, PROVISIONAL}),
        "provisional_patterns": frozenset({PROVISIONAL}),
        "available_in_band": {KNOWN: 12, PROVISIONAL: 5},
        "rating_range": (1150, 1950),
        "budget_minutes": 60,
        "max_size": 3,
    }
    base.update(overrides)
    return CatalogueFacts(**base)  # type: ignore[arg-type]


def prescription(**overrides: object) -> Prescription:
    base: dict[str, object] = {
        "focus_patterns": (PatternWeight(KNOWN, 1.0),),
        "rating_band": (1200, 1600),
        "size": 3,
        "mix": (0.6, 0.25, 0.15),
        "diagnosis": "sliding window is weak",
        "rationale": "more sliding window",
    }
    base.update(overrides)
    return Prescription(**base)  # type: ignore[arg-type]


def kinds(result: object) -> set[str]:
    return {v["kind"] for v in result.violations}  # type: ignore[attr-defined]


class TestAcceptance:
    def test_a_feasible_prescription_passes_untouched(self) -> None:
        result = validate(prescription(), facts())

        assert result.result is ValidationResult.ACCEPTED
        assert result.violations == ()
        assert result.prescription == prescription()

    def test_the_result_is_usable(self) -> None:
        assert validate(prescription(), facts()).usable


class TestFocusPatterns:
    def test_a_hallucinated_pattern_is_dropped(self) -> None:
        """The coach cannot invent a pattern into existence."""
        result = validate(
            prescription(focus_patterns=(PatternWeight(KNOWN, 1.0), PatternWeight(STRANGER, 1.0))),
            facts(),
        )

        assert result.result is ValidationResult.CLAMPED
        assert Violation.UNKNOWN_PATTERN.value in kinds(result)
        assert [p.pattern_id for p in result.prescription.focus_patterns] == [KNOWN]

    def test_a_locked_pattern_cannot_be_targeted(self) -> None:
        result = validate(
            prescription(focus_patterns=(PatternWeight(KNOWN, 1.0), PatternWeight(LOCKED, 1.0))),
            facts(),
        )

        assert Violation.LOCKED_PATTERN.value in kinds(result)
        assert [p.pattern_id for p in result.prescription.focus_patterns] == [KNOWN]

    def test_a_provisionally_unlocked_pattern_cannot_be_targeted(self) -> None:
        """Available to practise, but aiming at it means aiming at a guess."""
        result = validate(
            prescription(
                focus_patterns=(PatternWeight(KNOWN, 1.0), PatternWeight(PROVISIONAL, 1.0))
            ),
            facts(),
        )

        assert Violation.UNPROVEN_PATTERN.value in kinds(result)

    def test_a_prescription_with_nothing_left_is_rejected(self) -> None:
        result = validate(prescription(focus_patterns=(PatternWeight(STRANGER, 1.0),)), facts())

        assert result.result is ValidationResult.REJECTED
        assert result.prescription is None
        assert not result.usable
        assert Violation.NO_FOCUS_PATTERNS.value in kinds(result)

    def test_a_zero_weight_pattern_is_dropped(self) -> None:
        result = validate(
            prescription(focus_patterns=(PatternWeight(KNOWN, 1.0), PatternWeight(KNOWN, 0.0))),
            facts(),
        )

        assert len(result.prescription.focus_patterns) == 1


class TestRatingBand:
    def test_an_inverted_band_is_swapped(self) -> None:
        result = validate(prescription(rating_band=(1700, 1300)), facts())

        assert Violation.BAND_INVERTED.value in kinds(result)
        assert result.prescription.rating_band == (1300, 1700)

    def test_a_band_beyond_the_catalogue_is_clamped(self) -> None:
        result = validate(prescription(rating_band=(500, 4000)), facts())

        assert Violation.BAND_OUT_OF_RANGE.value in kinds(result)
        assert result.prescription.rating_band == (1150, 1950)

    def test_a_band_with_no_problems_widens_rather_than_emptying(self) -> None:
        """An empty block is worse than a wider one."""
        result = validate(prescription(), facts(available_in_band={}))

        assert Violation.BAND_EMPTY.value in kinds(result)
        assert result.prescription.rating_band == (1150, 1950)


class TestSize:
    def test_a_block_larger_than_the_budget_is_reduced(self) -> None:
        result = validate(prescription(size=25), facts())

        assert Violation.SIZE_OVER_BUDGET.value in kinds(result)
        assert result.prescription.size == 3

    def test_the_violation_explains_the_budget(self) -> None:
        result = validate(prescription(size=25), facts())
        detail = next(v["detail"] for v in result.violations if v["kind"] == "size_over_budget")

        assert "60 minutes" in detail

    @pytest.mark.parametrize("size", [0, -4])
    def test_a_non_positive_size_is_raised_to_one(self, size: int) -> None:
        result = validate(prescription(size=size), facts())

        assert result.prescription.size == 1


class TestMix:
    def test_an_unnormalised_mix_is_normalised(self) -> None:
        result = validate(prescription(mix=(6.0, 2.5, 1.5)), facts())

        assert Violation.MIX_NOT_NORMALISED.value in kinds(result)
        assert sum(result.prescription.mix) == pytest.approx(1.0)

    def test_removing_interleaving_is_refused(self) -> None:
        """Blocked practice feels better and works worse. Not negotiable."""
        result = validate(prescription(mix=(1.0, 0.0, 0.0)), facts())

        assert Violation.MIX_NO_INTERLEAVING.value in kinds(result)
        assert result.prescription.mix[1] >= tuning.MIN_INTERLEAVED_SHARE

    def test_the_refusal_explains_itself(self) -> None:
        result = validate(prescription(mix=(1.0, 0.0, 0.0)), facts())
        detail = next(v["detail"] for v in result.violations if v["kind"] == "mix_no_interleaving")

        assert "transfer" in detail

    def test_an_empty_mix_falls_back_to_the_default(self) -> None:
        result = validate(prescription(mix=(0.0, 0.0, 0.0)), facts())

        assert result.prescription.mix[0] == pytest.approx(tuning.BLOCK_MIX_WEAKNESS)

    def test_a_negative_share_is_floored(self) -> None:
        result = validate(prescription(mix=(0.9, 0.2, -0.1)), facts())

        assert all(share >= 0 for share in result.prescription.mix)
        assert sum(result.prescription.mix) == pytest.approx(1.0)

    def test_restoring_interleaving_still_sums_to_one(self) -> None:
        result = validate(prescription(mix=(0.95, 0.05, 0.0)), facts())

        assert sum(result.prescription.mix) == pytest.approx(1.0, abs=1e-6)


class TestStructure:
    def test_a_prescription_cannot_name_a_problem(self) -> None:
        """Invariant 3, enforced by the type rather than by instruction.

        There is no field for a problem id, so the coach has no way to supply
        one — it cannot hallucinate a problem, re-prescribe a solved one, or
        reach past a prerequisite.
        """
        fields = set(Prescription.__dataclass_fields__)

        assert not any("problem" in name for name in fields)

    def test_validation_is_pure(self) -> None:
        original = prescription(size=99)

        validate(original, facts())

        assert original.size == 99

    def test_multiple_problems_are_all_reported(self) -> None:
        """Clamping never silently fixes things; every adjustment is listed."""
        result = validate(
            prescription(
                focus_patterns=(PatternWeight(KNOWN, 1.0), PatternWeight(STRANGER, 1.0)),
                rating_band=(9000, 100),
                size=99,
                mix=(1.0, 0.0, 0.0),
            ),
            facts(),
        )

        assert len(result.violations) >= 4
        assert result.result is ValidationResult.CLAMPED


def _unused(value: UUID) -> None:  # pragma: no cover - keeps the import honest
    del value
