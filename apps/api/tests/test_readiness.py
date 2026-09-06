"""Readiness models — pure functions, tested exhaustively (spec §15).

Both models are held to the same behavioural contract. If one cannot satisfy it,
that is a finding for the §6.3 bake-off, not a test to relax.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from dsa_coach import tuning
from dsa_coach.mechanism.evidence import Evidence, ProblemRef
from dsa_coach.mechanism.readiness import (
    MODELS,
    PRIMARY_MODEL,
    BetaBaselineReadinessModel,
    Glicko2ReadinessModel,
    bucket_index,
)
from dsa_coach.mechanism.readiness.baseline import Bucket, rating_reliability
from dsa_coach.models import Level

AT = datetime(2026, 1, 1, tzinfo=UTC)
MID = ProblemRef(rating=1500, rating_rd=75)

ALL_MODELS = pytest.mark.parametrize("model", list(MODELS.values()), ids=list(MODELS))


def ev(score: float, *, rating: int = 1500, rd: int = 75, weight: float = 1.0) -> Evidence:
    return Evidence(score=score, weight=weight, problem=ProblemRef(rating, rd), at=AT)


def feed(model: Any, level: Level, evidence: list[Evidence]) -> Any:
    state = model.blank(level)
    for item in evidence:
        state = model.update(state, item)
    return state


class TestContract:
    """Every model must satisfy these, whichever is primary."""

    @ALL_MODELS
    def test_predictions_stay_in_range(self, model: Any) -> None:
        state = feed(model, Level.BEGINNER, [ev(1.0)] * 5 + [ev(0.0)] * 5)
        prediction = model.predict(state, MID)

        assert 0.0 <= prediction.score <= 1.0
        assert 0.0 <= prediction.uncertainty <= 1.0

    @ALL_MODELS
    def test_success_raises_and_failure_lowers(self, model: Any) -> None:
        blank = model.blank(Level.BEGINNER)
        start = model.predict(blank, MID).score

        up = model.predict(model.update(blank, ev(1.0)), MID).score
        down = model.predict(model.update(blank, ev(0.0)), MID).score

        assert up > start
        assert down < start

    @ALL_MODELS
    def test_monotonic_in_number_of_successes(self, model: Any) -> None:
        scores = [
            model.predict(feed(model, Level.BEGINNER, [ev(1.0)] * n), MID).score for n in range(6)
        ]

        assert scores == sorted(scores)

    @ALL_MODELS
    def test_uncertainty_falls_with_evidence(self, model: Any) -> None:
        sparse = model.predict(feed(model, Level.BEGINNER, [ev(1.0)] * 2), MID)
        dense = model.predict(feed(model, Level.BEGINNER, [ev(1.0)] * 20), MID)

        assert dense.uncertainty < sparse.uncertainty

    @ALL_MODELS
    def test_blank_state_is_never_calibrated(self, model: Any) -> None:
        """With no evidence the UI must say 'Calibrating', not show a band."""
        prediction = model.predict(model.blank(Level.BEGINNER), MID)

        assert not prediction.calibrated
        assert prediction.band == "calibrating"

    @ALL_MODELS
    def test_single_outlier_barely_moves_a_settled_estimate(self, model: Any) -> None:
        """One failure after a long run of successes is noise, not news."""
        settled = feed(model, Level.BEGINNER, [ev(1.0)] * 20)
        before = model.predict(settled, MID).score
        after = model.predict(model.update(settled, ev(0.0)), MID).score

        assert abs(after - before) < 0.15

    @ALL_MODELS
    def test_repeated_failures_move_more_than_one(self, model: Any) -> None:
        settled = feed(model, Level.BEGINNER, [ev(1.0)] * 20)
        one = model.predict(model.update(settled, ev(0.0)), MID).score
        many = model.predict(feed(model, Level.BEGINNER, [ev(1.0)] * 20 + [ev(0.0)] * 5), MID)

        assert many.score < one

    @ALL_MODELS
    def test_lower_weight_moves_the_estimate_less(self, model: Any) -> None:
        blank = model.blank(Level.BEGINNER)
        full = model.predict(model.update(blank, ev(1.0, weight=1.0)), MID).score
        light = model.predict(model.update(blank, ev(1.0, weight=0.3)), MID).score
        start = model.predict(blank, MID).score

        assert start < light < full

    @ALL_MODELS
    def test_update_is_pure(self, model: Any) -> None:
        state = model.blank(Level.BEGINNER)
        serialized = model.serialize(state)

        model.update(state, ev(1.0))

        assert model.serialize(state) == serialized

    @ALL_MODELS
    def test_round_trips_through_serialization(self, model: Any) -> None:
        state = feed(model, Level.INTERMEDIATE, [ev(1.0), ev(0.0), ev(0.6)])

        restored = model.deserialize(model.serialize(state))

        assert model.predict(restored, MID) == model.predict(state, MID)

    @ALL_MODELS
    def test_advanced_prior_starts_above_beginner(self, model: Any) -> None:
        beginner = model.predict(model.blank(Level.BEGINNER), MID).score
        advanced = model.predict(model.blank(Level.ADVANCED), MID).score

        assert advanced > beginner

    @ALL_MODELS
    def test_converges_toward_the_true_rate(self, model: Any) -> None:
        """Feed a 70% success rate; the estimate should approach it."""
        pattern = [
            ev(1.0),
            ev(1.0),
            ev(1.0),
            ev(1.0),
            ev(1.0),
            ev(1.0),
            ev(1.0),
            ev(0.0),
            ev(0.0),
            ev(0.0),
        ]
        state = feed(model, Level.BEGINNER, pattern * 8)

        assert model.predict(state, MID).score == pytest.approx(0.7, abs=0.12)


class TestPrimarySelection:
    def test_baseline_is_primary(self) -> None:
        """Spec §6.3: the simple model leads until the bake-off says otherwise."""
        assert BetaBaselineReadinessModel.version == PRIMARY_MODEL

    def test_both_models_are_registered(self) -> None:
        """Both must run so the Phase 6 comparison has data."""
        assert set(MODELS) == {
            BetaBaselineReadinessModel.version,
            Glicko2ReadinessModel.version,
        }


class TestBetaBaseline:
    @pytest.mark.parametrize(
        ("rating", "expected"),
        [(1200, 0), (1399, 0), (1400, 1), (1649, 1), (1650, 2), (2000, 2)],
    )
    def test_bucket_boundaries(self, rating: int, expected: int) -> None:
        assert bucket_index(rating) == expected

    def test_buckets_are_independent(self) -> None:
        """Success on easy problems must not claim readiness on hard ones."""
        model = BetaBaselineReadinessModel()
        state = feed(model, Level.BEGINNER, [ev(1.0, rating=1200)] * 12)

        easy = model.predict(state, ProblemRef(1200, 75))
        hard = model.predict(state, ProblemRef(1800, 75))

        assert easy.score > hard.score

    def test_unseen_bucket_falls_back_to_pooled_evidence(self) -> None:
        """A first attempt in a new band is judged on what is known, not the prior."""
        model = BetaBaselineReadinessModel()
        state = feed(model, Level.BEGINNER, [ev(1.0, rating=1200)] * 12)

        blank_prediction = model.predict(model.blank(Level.BEGINNER), ProblemRef(1800, 75))
        pooled_prediction = model.predict(state, ProblemRef(1800, 75))

        assert pooled_prediction.score > blank_prediction.score

    def test_unreliable_ratings_are_discounted(self) -> None:
        assert rating_reliability(75) > rating_reliability(300)
        assert rating_reliability(300) == pytest.approx(
            1 - tuning.BASELINE_MAX_RATING_RD_PENALTY * (300 / 350)
        )

    def test_manual_rating_evidence_moves_less_than_contest_rating(self) -> None:
        model = BetaBaselineReadinessModel()
        blank = model.blank(Level.BEGINNER)

        reliable = model.predict(model.update(blank, ev(1.0, rd=75)), MID).score
        vague = model.predict(model.update(blank, ev(1.0, rd=300)), MID).score

        assert vague < reliable

    def test_posterior_sd_shrinks_as_expected(self) -> None:
        wide = Bucket(alpha=1.0, beta=1.0)
        narrow = Bucket(alpha=50.0, beta=50.0)

        assert wide.sd > narrow.sd
        assert narrow.mean == pytest.approx(0.5)

    def test_calibrates_within_a_realistic_number_of_attempts(self) -> None:
        """The whole point of the baseline is working at small n (spec §6.3)."""
        model = BetaBaselineReadinessModel()
        state = feed(model, Level.BEGINNER, [ev(1.0), ev(1.0), ev(0.0), ev(1.0), ev(1.0), ev(0.0)])

        assert model.predict(state, MID).calibrated


class TestGlicko:
    def test_beating_a_hard_problem_moves_more_than_an_easy_one(self) -> None:
        """The continuous use of difficulty is Glicko's one real advantage."""
        model = Glicko2ReadinessModel()
        blank = model.blank(Level.INTERMEDIATE)

        easy_win = model.update(blank, ev(1.0, rating=1000))
        hard_win = model.update(blank, ev(1.0, rating=2000))

        assert hard_win.rating > easy_win.rating

    def test_rd_shrinks_with_evidence(self) -> None:
        model = Glicko2ReadinessModel()
        blank = model.blank(Level.BEGINNER)

        after = feed(model, Level.BEGINNER, [ev(1.0), ev(0.0), ev(1.0)])

        assert after.rd < blank.rd

    def test_uncertain_problem_rating_is_handled_by_g_phi(self) -> None:
        """Spec §6.2: rating reliability enters through Glicko's own machinery."""
        model = Glicko2ReadinessModel()
        blank = model.blank(Level.BEGINNER)

        precise = model.update(blank, ev(1.0, rating=1800, rd=50))
        vague = model.update(blank, ev(1.0, rating=1800, rd=350))

        assert precise.rating > vague.rating

    def test_volatility_stays_positive_and_bounded(self) -> None:
        model = Glicko2ReadinessModel()
        state = feed(model, Level.BEGINNER, [ev(1.0), ev(0.0)] * 15)

        assert 0.0 < state.volatility < 1.0


class TestBands:
    """Invariant 12: never present an uncalibrated estimate as a number."""

    def test_high_uncertainty_always_reads_as_calibrating(self) -> None:
        from dsa_coach.mechanism.readiness.base import Prediction

        assert Prediction(score=0.95, uncertainty=0.9).band == "calibrating"

    @pytest.mark.parametrize(
        ("score", "expected"),
        [(0.1, "not_ready"), (0.45, "developing"), (0.65, "approaching"), (0.9, "ready")],
    )
    def test_calibrated_scores_map_to_bands(self, score: float, expected: str) -> None:
        from dsa_coach.mechanism.readiness.base import Prediction

        assert Prediction(score=score, uncertainty=0.05).band == expected


class TestEvidenceTiming:
    def test_evidence_rejects_impossible_values(self) -> None:
        with pytest.raises(ValueError, match="score"):
            Evidence(score=1.5, weight=1.0, problem=MID, at=AT)
        with pytest.raises(ValueError, match="weight"):
            Evidence(score=0.5, weight=2.0, problem=MID, at=AT + timedelta(days=1))
