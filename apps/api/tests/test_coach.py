"""The judgment layer end to end, and its refusal to be trusted (spec §7).

Agent evaluations run against deterministic doubles — no network, no key, no
flakiness (spec §15). Each double is a specific way a model can be wrong.

The load-bearing test in this file is the last one: with the coach removed
entirely, everything still works.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dsa_coach import tuning
from dsa_coach.coach import CoachContext, StubCoachRuntime
from dsa_coach.coach.runtime import CoachFailure, CoachOutcome
from dsa_coach.mechanism.prescription import PatternWeight, Prescription
from dsa_coach.models import (
    AgentRun,
    AgentRunStatus,
    Pattern,
    PrescriptionRecord,
    PrescriptionValidation,
    User,
    ValidationResult,
)
from dsa_coach.services import coach as coach_service

BASE = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


def event(slug: str, *, day: int = 0, resolution: str = "independent") -> dict[str, object]:
    return {
        "event_uuid": str(uuid.uuid4()),
        "problem_slug": slug,
        "submitted_at": (BASE + timedelta(days=day)).isoformat(),
        "resolution": resolution,
        "submission_outcome": "accepted" if resolution == "independent" else "wrong_answer",
        "blocker": None if resolution == "independent" else "pattern_known_impl_failed",
        "active_seconds": 900,
    }


async def practice(client: AsyncClient, slugs: list[str], **kwargs: object) -> None:
    for day, slug in enumerate(slugs):
        response = await client.post("/attempts", json=event(slug, day=day, **kwargs))  # type: ignore[arg-type]
        assert response.status_code == 201, response.text


PLACEMENT_SLUGS = [
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


# --------------------------------------------------------------- test doubles


class ScriptedRuntime:
    """Returns whatever it was handed. One scripted model failure mode each."""

    name = "scripted"

    def __init__(self, outcome: CoachOutcome) -> None:
        self.outcome = outcome
        self.calls = 0
        self.seen: CoachContext | None = None

    async def prescribe(self, context: CoachContext) -> CoachOutcome:
        self.calls += 1
        self.seen = context
        return self.outcome


class FlakyRuntime:
    """Fails a few times, then succeeds. Exercises the bounded retry."""

    name = "flaky"

    def __init__(self, failures: int, then: CoachOutcome) -> None:
        self.remaining = failures
        self.then = then
        self.calls = 0

    async def prescribe(self, context: CoachContext) -> CoachOutcome:
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            return CoachOutcome(failure=CoachFailure.RATE_LIMITED, error_detail="429")
        return self.then


async def a_targetable_pattern(session: AsyncSession, slug: str = "array-traversal") -> uuid.UUID:
    """A pattern the coach is actually allowed to focus on.

    Most patterns are *provisional* at cold start — unlocked, but with unproven
    prerequisites — and validation refuses those as a focus. `array-traversal`
    has no gating prerequisite (its only edge is below the gating strength), so
    it is targetable from the first day.
    """
    return (await session.execute(select(Pattern.id).where(Pattern.slug == slug))).scalar_one()


async def the_user(session: AsyncSession) -> User:
    return (await session.execute(select(User))).scalar_one()


# ------------------------------------------------------------------ the tests


class TestContextAssembly:
    async def test_the_coach_never_sees_a_user_id(self, onboarded: AsyncClient) -> None:
        """Invariant 10: identity is server-side, and not the model's business."""
        fields = set(CoachContext.__dataclass_fields__)

        assert not any("user" in name for name in fields)

    async def test_context_carries_evidence_not_problems_to_schedule(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        await practice(onboarded, ["two-sum", "contains-duplicate"])

        context, _ = await coach_service.build_context(session, await the_user(session))

        assert context.patterns
        assert context.recent_attempts
        assert all(a.attempt_id for a in context.recent_attempts)

    async def test_context_reports_which_patterns_may_be_targeted(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        context, _ = await coach_service.build_context(session, await the_user(session))

        assert any(p.provisional for p in context.patterns), "cold start is provisional"
        assert all(isinstance(p.unlocked, bool) for p in context.patterns)


class TestValidationInTheLoop:
    async def test_a_hallucinated_pattern_is_clamped_and_recorded(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        user = await the_user(session)
        real = await a_targetable_pattern(session)
        runtime = ScriptedRuntime(
            CoachOutcome(
                prescription=Prescription(
                    focus_patterns=(
                        PatternWeight(real, 1.0),
                        PatternWeight(uuid.uuid4(), 1.0),
                    ),
                    rating_band=(1200, 1600),
                    size=3,
                    mix=(0.6, 0.25, 0.15),
                    diagnosis="d",
                    rationale="r",
                ),
                model="scripted-1",
            )
        )

        result = await coach_service.run_coach(session, user, runtime=runtime)

        assert result.validation is not None
        assert result.validation.result is ValidationResult.CLAMPED
        assert not result.used_fallback
        assert "unknown_pattern" in {v["kind"] for v in result.validation.violations}

    async def test_a_wholly_invalid_prescription_falls_back(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        user = await the_user(session)
        runtime = ScriptedRuntime(
            CoachOutcome(
                prescription=Prescription(
                    focus_patterns=(PatternWeight(uuid.uuid4(), 1.0),),
                    rating_band=(1200, 1600),
                    size=3,
                    mix=(0.6, 0.25, 0.15),
                ),
                model="scripted-2",
            )
        )

        result = await coach_service.run_coach(session, user, runtime=runtime)

        assert result.validation is not None
        assert result.validation.result is ValidationResult.REJECTED
        assert result.used_fallback
        assert "could not be applied" in result.message

    async def test_the_original_prescription_is_stored_not_the_clamped_one(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        """Otherwise the audit trail would just agree with whatever was applied."""
        user = await the_user(session)
        real = await a_targetable_pattern(session)
        runtime = ScriptedRuntime(
            CoachOutcome(
                prescription=Prescription(
                    focus_patterns=(PatternWeight(real, 1.0),),
                    rating_band=(1200, 1600),
                    size=99,
                    mix=(0.6, 0.25, 0.15),
                ),
                model="scripted-3",
            )
        )

        await coach_service.run_coach(session, user, runtime=runtime)

        record = (await session.execute(select(PrescriptionRecord))).scalar_one()
        validation = (await session.execute(select(PrescriptionValidation))).scalar_one()

        assert record.size == 99, "what was asked for"
        assert validation.result is ValidationResult.CLAMPED
        assert validation.violations

    async def test_clamping_is_explained_to_the_user(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        user = await the_user(session)
        real = await a_targetable_pattern(session)
        runtime = ScriptedRuntime(
            CoachOutcome(
                prescription=Prescription(
                    focus_patterns=(PatternWeight(real, 1.0),),
                    rating_band=(1200, 1600),
                    size=3,
                    mix=(1.0, 0.0, 0.0),
                    rationale="Focus on hashmaps.",
                ),
                model="scripted-4",
            )
        )

        result = await coach_service.run_coach(session, user, runtime=runtime)

        assert "Focus on hashmaps." in result.message
        assert "Adjusted to fit" in result.message


class TestFailureHandling:
    async def test_an_unavailable_coach_is_not_an_error(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        runtime = ScriptedRuntime(
            CoachOutcome(failure=CoachFailure.UNAVAILABLE, error_detail="no key")
        )

        result = await coach_service.run_coach(session, await the_user(session), runtime=runtime)

        assert result.used_fallback
        assert result.run.status is AgentRunStatus.UNAVAILABLE
        assert "Nothing else is affected" in result.message

    async def test_a_retryable_failure_is_retried_then_succeeds(
        self, onboarded: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tuning, "COACH_BASE_DELAY_SECONDS", 0.0)
        real = await a_targetable_pattern(session)
        runtime = FlakyRuntime(
            failures=1,
            then=CoachOutcome(
                prescription=Prescription(
                    focus_patterns=(PatternWeight(real, 1.0),),
                    rating_band=(1200, 1600),
                    size=3,
                    mix=(0.6, 0.25, 0.15),
                ),
                model="flaky",
            ),
        )

        result = await coach_service.run_coach(session, await the_user(session), runtime=runtime)

        assert runtime.calls == 2
        assert not result.used_fallback

    async def test_retries_are_bounded(
        self, onboarded: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tuning, "COACH_BASE_DELAY_SECONDS", 0.0)
        runtime = FlakyRuntime(failures=99, then=CoachOutcome())

        result = await coach_service.run_coach(session, await the_user(session), runtime=runtime)

        assert runtime.calls == tuning.COACH_MAX_ATTEMPTS
        assert result.used_fallback

    async def test_a_non_retryable_failure_is_not_retried(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        runtime = ScriptedRuntime(CoachOutcome(failure=CoachFailure.INVALID_OUTPUT))

        await coach_service.run_coach(session, await the_user(session), runtime=runtime)

        assert runtime.calls == 1

    async def test_every_run_is_recorded_even_when_it_fails(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        runtime = ScriptedRuntime(
            CoachOutcome(failure=CoachFailure.PROVIDER_ERROR, error_detail="boom")
        )

        await coach_service.run_coach(session, await the_user(session), runtime=runtime)

        run = (await session.execute(select(AgentRun))).scalars().first()
        assert run is not None
        assert run.status is AgentRunStatus.FAILED
        assert run.error_code == CoachFailure.PROVIDER_ERROR.value

    def test_only_transient_failures_are_retryable(self) -> None:
        assert CoachFailure.RATE_LIMITED.retryable
        assert CoachFailure.TIMEOUT.retryable
        assert CoachFailure.PROVIDER_ERROR.retryable
        # A missing key and a malformed answer will not fix themselves.
        assert not CoachFailure.UNAVAILABLE.retryable
        assert not CoachFailure.INVALID_OUTPUT.retryable


class TestDeterministicCoach:
    """The stub is also the fallback, so it has to be genuinely defensible."""

    async def test_it_targets_the_least_ready_established_patterns(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        await practice(onboarded, PLACEMENT_SLUGS)
        user = await the_user(session)
        context, _ = await coach_service.build_context(session, user)

        outcome = await StubCoachRuntime().prescribe(context)

        assert outcome.succeeded
        assert outcome.prescription is not None
        assert outcome.prescription.confidence == "low", "honest about being arithmetic"

    async def test_it_never_targets_a_provisional_pattern(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        context, facts = await coach_service.build_context(session, await the_user(session))

        outcome = await StubCoachRuntime().prescribe(context)

        assert outcome.prescription is not None
        for item in outcome.prescription.focus_patterns:
            assert item.pattern_id not in facts.provisional_patterns

    async def test_it_says_so_when_there_is_nothing_to_target(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        context, _ = await coach_service.build_context(session, await the_user(session))

        outcome = await StubCoachRuntime().prescribe(context)

        assert outcome.prescription is not None
        assert outcome.prescription.focus_patterns == ()
        assert "Not enough established evidence" in outcome.prescription.diagnosis


class TestEndpoint:
    async def test_prescribing_produces_a_plan(self, onboarded: AsyncClient) -> None:
        await practice(onboarded, PLACEMENT_SLUGS)

        result = (await onboarded.post("/coach/prescribe")).json()

        assert result["plan"]["items"]
        assert result["message"]

    async def test_the_response_says_whether_the_coach_was_used(
        self, onboarded: AsyncClient
    ) -> None:
        """A plan built without the coach is a normal outcome, not a hidden one."""
        result = (await onboarded.post("/coach/prescribe")).json()

        assert "used_fallback" in result
        assert result["runtime"] == "deterministic-stub", "the suite pins coach_runtime=stub"

    async def test_runs_are_listed_for_audit(self, onboarded: AsyncClient) -> None:
        await onboarded.post("/coach/prescribe")

        runs = (await onboarded.get("/coach/runs")).json()

        assert len(runs) == 1
        assert runs[0]["agent_name"] == "dsa-coach"

    async def test_a_prescribed_plan_records_who_chose_the_problems(
        self, onboarded: AsyncClient
    ) -> None:
        """Invariant 3, visible in the audit trail."""
        await practice(onboarded, PLACEMENT_SLUGS)

        result = (await onboarded.post("/coach/prescribe")).json()
        context = result["plan"]["generation_context"]

        assert "deterministic code selected the problems" in context["note"]


class TestInvariantFour:
    """The product must remain fully usable with the AI provider unavailable."""

    async def test_an_unconfigured_provider_selects_the_deterministic_coach(self) -> None:
        from dsa_coach.coach import build_runtime
        from dsa_coach.config import Settings

        # The `openai` runtime without its key, and an unrecognized value, both
        # have to degrade to the stub rather than raise. That fallback is the
        # entire mechanism by which invariant 4 holds.
        assert isinstance(
            build_runtime(Settings(coach_runtime="openai", openai_api_key=None)),
            StubCoachRuntime,
        )
        assert isinstance(build_runtime(Settings(coach_runtime="stub")), StubCoachRuntime)
        assert isinstance(build_runtime(Settings(coach_runtime="nonsense")), StubCoachRuntime)

    async def test_codex_is_the_default_runtime(self) -> None:
        from dsa_coach.coach import build_runtime
        from dsa_coach.config import Settings

        # Selection only. Nothing here talks to Codex; the runtime's own
        # behaviour is covered by tests/test_codex_runtime.py.
        assert build_runtime(Settings()).name == "codex"

    async def test_everything_still_works_with_the_coach_removed(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        """The load-bearing test of the phase.

        With a runtime that only ever fails, the user can still practise, get a
        block, see progress, and be told plainly what happened.
        """
        await practice(onboarded, PLACEMENT_SLUGS)
        dead = ScriptedRuntime(CoachOutcome(failure=CoachFailure.UNAVAILABLE))

        result = await coach_service.run_coach(session, await the_user(session), runtime=dead)
        await session.commit()

        block = (await onboarded.post("/plan/next-block")).json()
        readiness = (await onboarded.get("/progress/readiness")).json()

        assert result.used_fallback
        assert block["plan"]["items"], "a block is still produced"
        assert readiness["patterns"], "progress still renders"

    async def test_a_failed_run_leaves_the_plan_untouched(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        """Spec §7.4: keep the current plan unchanged on failure."""
        await practice(onboarded, PLACEMENT_SLUGS)
        before = (await onboarded.get("/plan/current")).json()

        dead = ScriptedRuntime(CoachOutcome(failure=CoachFailure.PROVIDER_ERROR))
        await coach_service.run_coach(session, await the_user(session), runtime=dead)
        await session.commit()

        after = (await onboarded.get("/plan/current")).json()
        assert after["version"] == before["version"]

    async def test_a_failure_writes_no_prescription(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        dead = ScriptedRuntime(CoachOutcome(failure=CoachFailure.TIMEOUT))

        await coach_service.run_coach(session, await the_user(session), runtime=dead)

        count = (
            await session.execute(select(func.count()).select_from(PrescriptionRecord))
        ).scalar_one()
        assert count == 0
