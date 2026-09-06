"""Phase 1 exit criteria, end to end (spec §16).

The claim being tested: the system produces a defensible, budget-respecting block
from recorded attempts, with **no AI involved**, and both readiness models run and
log predictions so the Phase 6 bake-off has data.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dsa_coach.mechanism.readiness import MODELS, PRIMARY_MODEL
from dsa_coach.models import (
    PatternBaselineBucket,
    PatternRating,
    ReadinessPrediction,
    ReviewSchedule,
    TriggerBatch,
)

BASE = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


def event(
    slug: str,
    *,
    resolution: str = "independent",
    outcome: str = "accepted",
    day: int = 0,
    blocker: str | None = None,
    is_resolve: bool = False,
    active_seconds: int = 900,
) -> dict[str, object]:
    return {
        "event_uuid": str(uuid.uuid4()),
        "problem_slug": slug,
        "submitted_at": (BASE + timedelta(days=day)).isoformat(),
        "resolution": resolution,
        "submission_outcome": outcome,
        "blocker": blocker,
        "is_resolve": is_resolve,
        "active_seconds": active_seconds,
        "language": "python",
    }


async def log(client: AsyncClient, payload: dict[str, object]) -> dict[str, object]:
    response = await client.post("/attempts", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


class TestReadinessRespondsToEvidence:
    async def test_readiness_starts_uncalibrated(self, onboarded: AsyncClient) -> None:
        report = (await onboarded.get("/progress/readiness")).json()

        assert report["calibrated_count"] == 0
        assert all(p["band"] == "calibrating" for p in report["patterns"])
        assert "uncalibrated" in report["disclaimer"]

    async def test_failures_lower_readiness_on_the_right_pattern(
        self, onboarded: AsyncClient
    ) -> None:
        for day, slug in enumerate(["two-sum", "contains-duplicate", "group-anagrams"]):
            await log(onboarded, event(slug, resolution="failed", outcome="wrong_answer", day=day))

        report = (await onboarded.get("/progress/readiness")).json()
        hashmap = next(p for p in report["patterns"] if p["slug"] == "hashmap-counting")
        untouched = next(p for p in report["patterns"] if p["slug"] == "dp-knapsack")

        assert hashmap["estimate"] < untouched["estimate"]
        assert hashmap["evidence_count"] > 0

    async def test_successes_raise_readiness_and_eventually_calibrate(
        self, onboarded: AsyncClient
    ) -> None:
        slugs = ["two-sum", "contains-duplicate", "group-anagrams", "top-k-frequent-elements"]
        for day, slug in enumerate(slugs):
            await log(onboarded, event(slug, day=day))

        report = (await onboarded.get("/progress/readiness")).json()
        hashmap = next(p for p in report["patterns"] if p["slug"] == "hashmap-counting")

        assert hashmap["estimate"] > 0.5
        assert hashmap["evidence_count"] >= 4

    async def test_a_dismissed_questionnaire_still_counts_but_less(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        await log(onboarded, event("two-sum", resolution="unknown", outcome="accepted"))

        rows = (await session.execute(select(ReadinessPrediction))).scalars().all()
        outcomes = {r.actual_outcome for r in rows}

        assert outcomes == {0.7}, "dismissed + accepted falls back to the judge result"


class TestBothModelsRun:
    async def test_every_attempt_logs_a_prediction_per_model(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        """Spec §6.3: the bake-off needs both models forecasting on the same data."""
        await log(onboarded, event("two-sum"))

        rows = (await session.execute(select(ReadinessPrediction))).scalars().all()

        assert {r.model_version for r in rows} == set(MODELS)

    async def test_predictions_are_recorded_before_the_outcome_is_known(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        """A hindsight fit would make the calibration report meaningless."""
        await log(onboarded, event("two-sum"))
        await log(onboarded, event("contains-duplicate", day=1))

        rows = (
            (
                await session.execute(
                    select(ReadinessPrediction)
                    .where(ReadinessPrediction.model_version == PRIMARY_MODEL)
                    .order_by(ReadinessPrediction.predicted_at)
                )
            )
            .scalars()
            .all()
        )

        # The first prediction is made from the blank prior, so it cannot already
        # reflect the success it is predicting.
        assert rows[0].predicted_score < 1.0
        assert rows[0].actual_outcome == 1.0

    async def test_both_models_persist_their_own_state(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        await log(onboarded, event("two-sum"))

        baseline = (
            await session.execute(select(func.count()).select_from(PatternBaselineBucket))
        ).scalar_one()
        glicko = (
            await session.execute(select(func.count()).select_from(PatternRating))
        ).scalar_one()

        assert baseline > 0
        assert glicko > 0

    async def test_the_other_model_can_be_queried_directly(self, onboarded: AsyncClient) -> None:
        """Switching the primary model is a config change, so both must be readable."""
        await log(onboarded, event("two-sum"))

        primary = (await onboarded.get("/progress/readiness")).json()
        other = (await onboarded.get("/progress/readiness?model_version=glicko2-v1")).json()

        assert primary["model_version"] == PRIMARY_MODEL == "baseline-beta-v1"
        assert other["model_version"] == "glicko2-v1"

    async def test_an_unknown_model_falls_back_to_the_primary(self, onboarded: AsyncClient) -> None:
        report = (await onboarded.get("/progress/readiness?model_version=nonsense")).json()

        assert report["model_version"] == PRIMARY_MODEL


class TestDeterministicScheduling:
    async def test_a_block_is_produced_with_no_ai(self, onboarded: AsyncClient) -> None:
        """Invariant 4: the scheduler always works, coach or no coach."""
        for day, slug in enumerate(["two-sum", "contains-duplicate", "valid-palindrome"]):
            await log(onboarded, event(slug, day=day))

        result = (await onboarded.post("/plan/next-block")).json()

        assert result["plan"]["status"] == "active"
        assert len(result["plan"]["items"]) > 0
        assert result["plan"]["generation_context"]["generator"] == (
            "deterministic_block_assembler"
        )
        assert "No AI" in result["plan"]["generation_context"]["note"]

    async def test_the_block_respects_the_daily_budget(self, onboarded: AsyncClient) -> None:
        result = (await onboarded.post("/plan/next-block")).json()

        # Fixture goal is 60 minutes/day.
        assert result["total_minutes"] <= result["budget_minutes"] <= 60

    async def test_recently_attempted_problems_are_not_scheduled_as_new_practice(
        self, onboarded: AsyncClient
    ) -> None:
        """The cooldown governs practice, not review.

        A solved problem may legitimately come back as a due re-solve — that is
        the entire point of the retention track — so the cooldown is asserted
        against the practice roles only.
        """
        await log(onboarded, event("two-sum", day=0))

        result = (await onboarded.post("/plan/next-block")).json()
        practice = {
            i["problem"]["slug"] for i in result["plan"]["items"] if i["role"] != "retention"
        }

        assert "two-sum" not in practice

    async def test_a_failed_problem_stays_inside_its_cooldown(self, onboarded: AsyncClient) -> None:
        """A failure schedules no review, so the cooldown is the only rule left."""
        await log(onboarded, event("3sum", resolution="failed", outcome="wrong_answer"))

        result = (await onboarded.post("/plan/next-block")).json()
        slugs = {i["problem"]["slug"] for i in result["plan"]["items"]}

        assert "3sum" not in slugs

    async def test_locked_patterns_are_reported(self, onboarded: AsyncClient) -> None:
        """Nothing gated should be schedulable, and the UI must be able to say why."""
        result = (await onboarded.post("/plan/next-block")).json()

        assert result["locked_patterns"], "advanced patterns start locked"

    async def test_building_a_block_supersedes_the_provisional_plan(
        self, onboarded: AsyncClient
    ) -> None:
        await onboarded.post("/plan/next-block")

        current = (await onboarded.get("/plan/current")).json()

        assert current["status"] == "active"
        assert current["version"] == 2

    async def test_unlocks_endpoint_explains_what_is_blocked(self, onboarded: AsyncClient) -> None:
        unlocks = (await onboarded.get("/progress/unlocks")).json()
        blocked = [u for u in unlocks if not u["unlocked"]]

        assert blocked
        assert all(u["blocked_by"] for u in blocked)
        assert all(u["reason"] for u in blocked)


class TestRetention:
    async def test_a_solved_problem_enters_the_review_schedule(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        await log(onboarded, event("two-sum"))

        rows = (await session.execute(select(ReviewSchedule))).scalars().all()

        assert len(rows) == 1
        assert rows[0].due_at > BASE

    async def test_a_failed_problem_does_not_enter_the_schedule(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        await log(onboarded, event("two-sum", resolution="failed", outcome="wrong_answer"))

        count = (
            await session.execute(select(func.count()).select_from(ReviewSchedule))
        ).scalar_one()

        assert count == 0

    async def test_a_resolve_does_not_inflate_readiness(self, onboarded: AsyncClient) -> None:
        """Spec §6.2: review must never be mistaken for skill growth."""
        await log(onboarded, event("two-sum"))
        before = (await onboarded.get("/progress/readiness")).json()

        await log(onboarded, event("two-sum", day=30, is_resolve=True, active_seconds=200))
        after = (await onboarded.get("/progress/readiness")).json()

        def hashmap(report: dict) -> float:
            return next(
                p["estimate"] for p in report["patterns"] if p["slug"] == "hashmap-counting"
            )

        assert hashmap(after) == hashmap(before)

    async def test_a_resolve_advances_the_review_schedule(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        await log(onboarded, event("two-sum", active_seconds=1200))
        first_due = (await session.execute(select(ReviewSchedule))).scalar_one().due_at

        await log(onboarded, event("two-sum", day=30, is_resolve=True, active_seconds=300))

        await session.refresh((await session.execute(select(ReviewSchedule))).scalar_one())
        row = (await session.execute(select(ReviewSchedule))).scalar_one()
        assert row.due_at > first_due
        assert row.reps == 2

    async def test_retention_summary_is_reported(self, onboarded: AsyncClient) -> None:
        await log(onboarded, event("two-sum"))

        summary = (await onboarded.get("/progress/retention")).json()

        assert summary["tracked"] == 1
        assert summary["lapses"] == 0


class TestTrigger:
    async def test_nothing_fires_before_three_relevant_attempts(
        self, onboarded: AsyncClient
    ) -> None:
        await log(onboarded, event("two-sum"))
        await log(onboarded, event("contains-duplicate", day=1))

        assert (await onboarded.get("/plan/triggers")).json() == []

    async def test_three_attempts_produce_exactly_one_evaluation(
        self, onboarded: AsyncClient
    ) -> None:
        for day, slug in enumerate(["two-sum", "contains-duplicate", "valid-palindrome"]):
            await log(onboarded, event(slug, day=day))

        batches = (await onboarded.get("/plan/triggers")).json()

        assert len(batches) == 1
        assert batches[0]["relevant_count"] == 3

    async def test_the_evaluation_is_always_explained(self, onboarded: AsyncClient) -> None:
        """Silence looks broken; an explained result looks like a working product."""
        for day, slug in enumerate(["two-sum", "contains-duplicate", "valid-palindrome"]):
            await log(onboarded, event(slug, day=day))

        batch = (await onboarded.get("/plan/triggers")).json()[0]

        assert batch["explanation"]
        assert "Reviewed after 3 attempts" in batch["explanation"]

    async def test_a_no_change_result_says_so_plainly(self, onboarded: AsyncClient) -> None:
        """First evaluation has no prior snapshot, so nothing can have moved."""
        for day, slug in enumerate(["two-sum", "contains-duplicate", "valid-palindrome"]):
            await log(onboarded, event(slug, day=day))

        batch = (await onboarded.get("/plan/triggers")).json()[0]

        assert batch["outcome"] == "no_change"
        assert batch["material"] is False
        assert "no change" in batch["explanation"].lower()

    async def test_a_second_batch_can_detect_movement(self, onboarded: AsyncClient) -> None:
        for day, slug in enumerate(["two-sum", "contains-duplicate", "valid-palindrome"]):
            await log(onboarded, event(slug, day=day))

        for day, slug in enumerate(["3sum", "group-anagrams", "binary-search"], start=3):
            await log(onboarded, event(slug, resolution="failed", outcome="wrong_answer", day=day))

        batches = (await onboarded.get("/plan/triggers")).json()

        assert len(batches) == 2
        assert batches[0]["material"] is True
        assert batches[0]["outcome"] == "pending_agent"
        assert batches[0]["reasons"]

    async def test_clean_resolves_do_not_advance_the_counter(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        """A review-heavy day must not fire evaluations on nothing new."""
        await log(onboarded, event("two-sum", active_seconds=1200))
        await log(onboarded, event("valid-parentheses", day=1, active_seconds=1200))

        for day in (30, 31):
            await log(
                onboarded,
                event("two-sum", day=day, is_resolve=True, active_seconds=200),
            )

        count = (await session.execute(select(func.count()).select_from(TriggerBatch))).scalar_one()

        assert count == 0, "two first exposures plus clean re-solves is not three"

    async def test_the_counter_does_not_double_count_on_replay(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        """Invariant 7 has to survive into the derived layers too."""
        events = [event(s, day=d) for d, s in enumerate(["two-sum", "3sum", "subsets"])]
        for payload in events:
            await log(onboarded, payload)
        for payload in events:
            await onboarded.post("/attempts", json=payload)

        count = (await session.execute(select(func.count()).select_from(TriggerBatch))).scalar_one()

        assert count == 1


class TestEvidenceCounting:
    async def test_evidence_count_reflects_attempts_not_storage_rows(
        self, onboarded: AsyncClient
    ) -> None:
        """The Beta model keeps three bucket rows per pattern.

        Counting every row per update reported three times the real evidence —
        precisely the kind of inflated number this project must not show.
        """
        slugs = ["two-sum", "contains-duplicate", "group-anagrams"]
        for day, slug in enumerate(slugs):
            await log(onboarded, event(slug, day=day))

        report = (await onboarded.get("/progress/readiness")).json()
        hashmap = next(p for p in report["patterns"] if p["slug"] == "hashmap-counting")

        assert hashmap["evidence_count"] == len(slugs)
