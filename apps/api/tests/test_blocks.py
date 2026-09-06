"""Block assembly and prerequisite gating (spec §6.4, §6.7)."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from dsa_coach import tuning
from dsa_coach.mechanism.blocks import (
    BlockMix,
    Candidate,
    assemble_block,
    estimate_minutes,
)
from dsa_coach.mechanism.prerequisites import (
    PrerequisiteEdge,
    UnlockState,
    evaluate_unlocks,
    prerequisite_met,
    topological_order,
)
from dsa_coach.mechanism.readiness.base import Prediction
from dsa_coach.models import ItemRole


def cand(minutes: int = 25, score: float = 0.6, rating: int = 1500) -> Candidate:
    return Candidate(problem_id=uuid4(), rating=rating, minutes=minutes, predicted_score=score)


def pool(n: int, **kwargs: object) -> list[Candidate]:
    return [cand(**kwargs) for _ in range(n)]  # type: ignore[arg-type]


class TestMinuteEstimates:
    def test_a_coin_flip_problem_costs_the_base(self) -> None:
        assert estimate_minutes(0.5) == tuning.BASE_PROBLEM_MINUTES

    def test_less_likely_problems_cost_more(self) -> None:
        assert estimate_minutes(0.2) > estimate_minutes(0.5) > estimate_minutes(0.9)

    def test_estimates_are_clamped(self) -> None:
        assert estimate_minutes(0.0) <= tuning.MAX_PROBLEM_MINUTES
        assert estimate_minutes(1.0) >= tuning.MIN_PROBLEM_MINUTES

    def test_estimates_stay_positive_across_the_whole_range(self) -> None:
        for tenth in range(11):
            assert estimate_minutes(tenth / 10) >= tuning.MIN_PROBLEM_MINUTES


class TestBlockMix:
    def test_counts_sum_to_the_requested_size(self) -> None:
        for size in range(1, 21):
            assert sum(BlockMix().counts(size).values()) == size

    def test_weakness_dominates(self) -> None:
        counts = BlockMix().counts(10)

        assert counts[ItemRole.WEAKNESS] > counts[ItemRole.INTERLEAVED]
        assert counts[ItemRole.INTERLEAVED] > 0, "interleaving must never vanish"

    def test_interleaving_survives_small_blocks(self) -> None:
        """Blocked practice inflates in-session performance and hurts transfer."""
        counts = BlockMix().counts(4)

        assert counts[ItemRole.INTERLEAVED] >= 1


class TestAssembly:
    def test_respects_the_time_budget(self) -> None:
        block = assemble_block(
            weakness=pool(20, minutes=30),
            interleaved=pool(20, minutes=30),
            retention=pool(20, minutes=30),
            budget_minutes=100,
            size=10,
        )

        assert block.total_minutes <= 100

    def test_under_schedules_rather_than_over_schedules(self) -> None:
        block = assemble_block(
            weakness=pool(20, minutes=30),
            interleaved=pool(20, minutes=30),
            retention=pool(20, minutes=30),
            budget_minutes=120,
            size=10,
        )

        assert block.total_minutes <= int(120 * tuning.BLOCK_BUDGET_FILL)

    def test_produces_the_intended_mix_when_pools_are_full(self) -> None:
        """60/25/15 of 10 is 6.0/2.5/1.5; largest-remainder gives the spare item
        to interleaved, which ties with retention and wins on a stable sort."""
        block = assemble_block(
            weakness=pool(20, minutes=10),
            interleaved=pool(20, minutes=10),
            retention=pool(20, minutes=10),
            budget_minutes=600,
            size=10,
        )
        roles = [item.role for item in block.items]

        assert roles.count(ItemRole.WEAKNESS) == 6
        assert roles.count(ItemRole.INTERLEAVED) == 3
        assert roles.count(ItemRole.RETENTION) == 1

    def test_mix_is_exact_when_the_size_divides_cleanly(self) -> None:
        block = assemble_block(
            weakness=pool(30, minutes=10),
            interleaved=pool(30, minutes=10),
            retention=pool(30, minutes=10),
            budget_minutes=1200,
            size=20,
        )
        roles = [item.role for item in block.items]

        assert roles.count(ItemRole.WEAKNESS) == 12
        assert roles.count(ItemRole.INTERLEAVED) == 5
        assert roles.count(ItemRole.RETENTION) == 3

    def test_never_repeats_a_problem(self) -> None:
        shared = pool(5, minutes=10)
        block = assemble_block(
            weakness=shared,
            interleaved=shared,
            retention=shared,
            budget_minutes=600,
            size=10,
        )
        ids = [item.candidate.problem_id for item in block.items]

        assert len(ids) == len(set(ids))

    def test_is_deterministic_under_a_fixed_seed(self) -> None:
        pools = {"weakness": pool(10), "interleaved": pool(10), "retention": pool(10)}

        first = assemble_block(**pools, budget_minutes=300, size=8, seed=42)  # type: ignore[arg-type]
        second = assemble_block(**pools, budget_minutes=300, size=8, seed=42)  # type: ignore[arg-type]

        assert [i.candidate.problem_id for i in first.items] == [
            i.candidate.problem_id for i in second.items
        ]

    def test_empty_pools_produce_an_empty_block_not_a_crash(self) -> None:
        block = assemble_block(
            weakness=[], interleaved=[], retention=[], budget_minutes=120, size=8
        )

        assert block.is_empty
        assert block.shortfalls

    def test_a_thin_pool_reports_the_shortfall(self) -> None:
        """Substitutions are explained, never silent."""
        block = assemble_block(
            weakness=pool(1, minutes=10),
            interleaved=pool(10, minutes=10),
            retention=[],
            budget_minutes=600,
            size=10,
        )

        assert any("retention" in s for s in block.shortfalls)

    def test_redistributes_a_shortfall_to_fill_the_block(self) -> None:
        block = assemble_block(
            weakness=pool(10, minutes=10),
            interleaved=pool(10, minutes=10),
            retention=[],
            budget_minutes=600,
            size=10,
        )

        assert len(block.items) == 10

    def test_places_at_least_one_item_even_on_a_tiny_budget(self) -> None:
        block = assemble_block(
            weakness=pool(5, minutes=60),
            interleaved=[],
            retention=[],
            budget_minutes=15,
            size=5,
        )

        assert len(block.items) == 1

    def test_prefers_informative_problems(self) -> None:
        """Attempts teach most where the outcome is least certain."""
        certain = Candidate(uuid4(), 1500, 25, predicted_score=0.99)
        uncertain = Candidate(uuid4(), 1500, 25, predicted_score=0.52)

        block = assemble_block(
            weakness=[certain, uncertain],
            interleaved=[],
            retention=[],
            budget_minutes=600,
            size=1,
        )

        assert block.items[0].candidate.problem_id == uncertain.problem_id


class TestPrerequisites:
    def test_a_root_pattern_is_unlocked(self) -> None:
        root = uuid4()

        result = evaluate_unlocks({root}, [], {})

        assert result[root].unlocked

    def test_an_undemonstrated_prerequisite_locks_the_pattern(self) -> None:
        base, advanced = uuid4(), uuid4()
        edges = [PrerequisiteEdge(advanced, base, 1.0)]

        result = evaluate_unlocks({base, advanced}, edges, {})

        assert not result[advanced].unlocked
        assert result[advanced].blocked_by == (base,)

    def test_a_demonstrated_prerequisite_unlocks_the_pattern(self) -> None:
        base, advanced = uuid4(), uuid4()
        edges = [PrerequisiteEdge(advanced, base, 1.0)]
        predictions = {base: Prediction(score=0.8, uncertainty=0.05)}

        result = evaluate_unlocks({base, advanced}, edges, predictions)

        assert result[advanced].unlocked

    def test_a_confident_guess_is_not_a_demonstration(self) -> None:
        """High score but uncalibrated must not unlock anything."""
        base, advanced = uuid4(), uuid4()
        edges = [PrerequisiteEdge(advanced, base, 1.0)]
        predictions = {base: Prediction(score=0.95, uncertainty=0.9)}

        result = evaluate_unlocks({base, advanced}, edges, predictions)

        assert not result[advanced].unlocked

    def test_weak_edges_are_advisory_and_do_not_gate(self) -> None:
        base, advanced = uuid4(), uuid4()
        weak = tuning.PREREQUISITE_GATING_STRENGTH_MIN - 0.1
        edges = [PrerequisiteEdge(advanced, base, weak)]

        result = evaluate_unlocks({base, advanced}, edges, {})

        assert result[advanced].unlocked

    def test_prerequisite_met_requires_both_score_and_calibration(self) -> None:
        assert not prerequisite_met(None)
        assert not prerequisite_met(Prediction(score=0.9, uncertainty=0.5))
        assert not prerequisite_met(Prediction(score=0.2, uncertainty=0.01))
        assert prerequisite_met(Prediction(score=0.9, uncertainty=0.01))

    def test_topological_order_places_prerequisites_first(self) -> None:
        a, b, c = uuid4(), uuid4(), uuid4()
        edges = [PrerequisiteEdge(c, b, 1.0), PrerequisiteEdge(b, a, 1.0)]

        order = topological_order({a, b, c}, edges)

        assert order.index(a) < order.index(b) < order.index(c)

    def test_topological_order_is_deterministic(self) -> None:
        ids = {uuid4() for _ in range(8)}

        assert topological_order(ids, []) == topological_order(ids, [])

    def test_a_cycle_is_reported_not_silently_tolerated(self) -> None:
        a, b = uuid4(), uuid4()
        edges = [PrerequisiteEdge(a, b, 1.0), PrerequisiteEdge(b, a, 1.0)]

        with pytest.raises(ValueError, match="cycle"):
            topological_order({a, b}, edges)

    def test_unlock_state_defaults_are_sane(self) -> None:
        state = UnlockState(unlocked=True)

        assert state.blocked_by == ()


def _ids(n: int) -> list[UUID]:
    return [uuid4() for _ in range(n)]


class TestRetentionIsNotCrowdedOut:
    """Forgetting solved problems is the silent failure of self-directed practice.

    It must not lose to integer rounding on a small block.
    """

    def test_a_small_block_still_reserves_a_review_slot(self) -> None:
        # 15% of 3 rounds to zero without the guarantee.
        block = assemble_block(
            weakness=pool(10, minutes=10),
            interleaved=pool(10, minutes=10),
            retention=pool(4, minutes=10),
            budget_minutes=120,
            size=3,
        )
        roles = [item.role for item in block.items]

        assert roles.count(ItemRole.RETENTION) >= 1

    def test_no_slot_is_reserved_when_nothing_is_due(self) -> None:
        block = assemble_block(
            weakness=pool(10, minutes=10),
            interleaved=pool(10, minutes=10),
            retention=[],
            budget_minutes=120,
            size=3,
        )
        roles = [item.role for item in block.items]

        assert ItemRole.RETENTION not in roles

    def test_the_reserved_slot_comes_out_of_the_larger_share(self) -> None:
        block = assemble_block(
            weakness=pool(10, minutes=10),
            interleaved=pool(10, minutes=10),
            retention=pool(2, minutes=10),
            budget_minutes=600,
            size=3,
        )

        assert len(block.items) == 3
