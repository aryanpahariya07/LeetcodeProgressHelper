"""Running the coach, and refusing to trust it (spec §7).

The order here is the whole design:

1. Assemble what the coach may see. Server-side, typed, no user id.
2. Ask for a block shape. Bounded retries, only for retryable failures.
3. **Validate against the mechanism layer.** Clamp what can be saved, reject what
   cannot.
4. Record the run, the prescription and the verdict — whatever happened.
5. Hand the surviving shape to the deterministic scheduler, which picks the
   actual problems.

If any of that fails, the scheduler runs on its own. That is not an error path
bolted on afterwards; it is the same path the product uses when no key is
configured (invariant 4).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from dsa_coach import tuning
from dsa_coach.coach import CoachContext, CoachRuntime, build_runtime
from dsa_coach.coach.runtime import AttemptSummary, CoachOutcome, PatternSummary
from dsa_coach.mechanism.prerequisites import PrerequisiteEdge, evaluate_unlocks
from dsa_coach.mechanism.prescription import (
    CatalogueFacts,
    Prescription,
    ValidatedPrescription,
    validate,
)
from dsa_coach.models import (
    AgentRun,
    AgentRunStatus,
    Attempt,
    Pattern,
    PatternPrerequisite,
    Plan,
    PlanItem,
    PrescriptionRecord,
    PrescriptionValidation,
    Problem,
    ProblemPattern,
    TriggerBatch,
    TriggerOutcome,
    User,
    UserGoal,
    ValidationResult,
)
from dsa_coach.services import readiness as readiness_service
from dsa_coach.services import retention as retention_service

logger = logging.getLogger(__name__)

AGENT_NAME = "dsa-coach"
RECENT_ATTEMPT_LIMIT = 12


@dataclass(frozen=True)
class CoachResult:
    """What happened, in enough detail for the UI to be honest about it."""

    run: AgentRun
    prescription: Prescription | None
    validation: ValidatedPrescription | None
    #: True when the scheduler ran on its own — no key, a failure, or a rejection.
    used_fallback: bool
    message: str


async def _goal(session: AsyncSession, user: User) -> UserGoal:
    return (
        await session.execute(
            select(UserGoal).where(UserGoal.user_id == user.id, UserGoal.active.is_(True))
        )
    ).scalar_one()


async def _catalogue_facts(
    session: AsyncSession, user: User, goal: UserGoal
) -> tuple[CatalogueFacts, dict[UUID, PatternSummary]]:
    predictions = await readiness_service.pattern_predictions(session, user)
    report = await readiness_service.readiness_report(session, user)
    patterns = (await session.execute(select(Pattern))).scalars().all()

    edges = [
        PrerequisiteEdge(r.pattern_id, r.requires_pattern_id, r.strength)
        for r in (await session.execute(select(PatternPrerequisite))).scalars().all()
    ]
    unlocks = evaluate_unlocks({p.id for p in patterns}, edges, predictions)

    counts: dict[UUID, int] = {}
    rows = (
        await session.execute(
            select(ProblemPattern.pattern_id, Problem.rating)
            .join(Problem, Problem.id == ProblemPattern.problem_id)
            .where(Problem.is_active.is_(True))
        )
    ).all()
    ratings = [rating for _, rating in rows] or [1200, 2000]
    for pattern_id, _ in rows:
        counts[pattern_id] = counts.get(pattern_id, 0) + 1

    summaries = {
        row.pattern_id: PatternSummary(
            pattern_id=row.pattern_id,
            slug=row.slug,
            name=row.name,
            band=row.prediction.band,
            calibrated=row.prediction.calibrated,
            evidence_count=row.evidence_count,
            unlocked=unlocks[row.pattern_id].unlocked,
            provisional=unlocks[row.pattern_id].provisional,
        )
        for row in report
        if row.pattern_id in unlocks
    }

    facts = CatalogueFacts(
        known_patterns=frozenset(p.id for p in patterns),
        unlocked_patterns=frozenset(pid for pid, s in unlocks.items() if s.unlocked),
        provisional_patterns=frozenset(pid for pid, s in unlocks.items() if s.provisional),
        available_in_band=counts,
        rating_range=(min(ratings), max(ratings)),
        budget_minutes=goal.minutes_per_day,
        max_size=_max_size(goal.minutes_per_day),
    )
    return facts, summaries


def _max_size(minutes_per_day: int) -> int:
    usable = minutes_per_day * tuning.BLOCK_BUDGET_FILL
    return max(1, int(usable // tuning.BASE_PROBLEM_MINUTES) + 1)


async def _recent_attempts(session: AsyncSession, user: User) -> tuple[AttemptSummary, ...]:
    attempts = (
        (
            await session.execute(
                select(Attempt)
                .where(Attempt.user_id == user.id)
                .options(selectinload(Attempt.problem))
                .order_by(Attempt.submitted_at.desc())
                .limit(RECENT_ATTEMPT_LIMIT)
            )
        )
        .scalars()
        .all()
    )

    pattern_rows = (
        await session.execute(
            select(ProblemPattern.problem_id, Pattern.slug).join(
                Pattern, Pattern.id == ProblemPattern.pattern_id
            )
        )
    ).all()
    by_problem: dict[UUID, list[str]] = {}
    for problem_id, slug in pattern_rows:
        by_problem.setdefault(problem_id, []).append(slug)

    return tuple(
        AttemptSummary(
            attempt_id=a.id,
            problem_slug=a.problem.slug,
            problem_rating=a.problem.rating,
            patterns=tuple(by_problem.get(a.problem_id, [])),
            resolution=a.resolution.value,
            blocker=a.blocker.value if a.blocker else None,
            submitted_at=a.submitted_at,
            active_seconds=a.active_seconds,
        )
        for a in attempts
    )


async def build_context(
    session: AsyncSession, user: User, trigger_reasons: tuple[str, ...] = ()
) -> tuple[CoachContext, CatalogueFacts]:
    """Everything the coach may see. Assembled here, never asked for by the model."""
    goal = await _goal(session, user)
    facts, summaries = await _catalogue_facts(session, user, goal)
    retention = await retention_service.summary(session, user, datetime.now(UTC))

    context = CoachContext(
        level=goal.self_assessed_level.value,
        days_per_week=goal.days_per_week,
        minutes_per_day=goal.minutes_per_day,
        target_companies=tuple(goal.target_companies),
        patterns=tuple(summaries.values()),
        recent_attempts=await _recent_attempts(session, user),
        retention_due=retention.due,
        retention_lapses=retention.lapses,
        trigger_reasons=trigger_reasons,
        max_size=facts.max_size,
        rating_range=facts.rating_range,
    )
    return context, facts


async def _ask(runtime: CoachRuntime, context: CoachContext) -> CoachOutcome:
    """Bounded retries, and only for failures that might resolve themselves."""
    outcome = await runtime.prescribe(context)
    attempt = 1

    while (
        outcome.failure is not None
        and outcome.failure.retryable
        and attempt < tuning.COACH_MAX_ATTEMPTS
    ):
        await asyncio.sleep(tuning.COACH_BASE_DELAY_SECONDS * 2 ** (attempt - 1))
        attempt += 1
        outcome = await runtime.prescribe(context)

    return outcome


async def run_coach(
    session: AsyncSession,
    user: User,
    *,
    trigger: str = "manual",
    trigger_reasons: tuple[str, ...] = (),
    runtime: CoachRuntime | None = None,
) -> CoachResult:
    """Ask the coach for a block shape, validate it, and record everything."""
    runtime = runtime or build_runtime()
    started = datetime.now(UTC)
    context, facts = await build_context(session, user, trigger_reasons)

    outcome = await _ask(runtime, context)

    run = AgentRun(
        user_id=user.id,
        agent_name=AGENT_NAME,
        trigger=trigger,
        runtime=runtime.name,
        model=outcome.model,
        status=AgentRunStatus.SUCCEEDED,
        input_summary=(
            f"{len(context.patterns)} patterns, {len(context.recent_attempts)} recent "
            f"attempts, {context.retention_due} reviews due"
        ),
        trace_id=outcome.trace_id,
        usage_metadata=outcome.usage,
        started_at=started,
        finished_at=datetime.now(UTC),
    )

    if outcome.prescription is None:
        failure = outcome.failure
        run.status = (
            AgentRunStatus.UNAVAILABLE
            if failure is not None and not failure.retryable
            else AgentRunStatus.FAILED
        )
        run.error_code = failure.value if failure else "no_prescription"
        run.output_summary = outcome.error_detail or "No prescription produced."
        session.add(run)
        await session.flush()
        return CoachResult(
            run=run,
            prescription=None,
            validation=None,
            used_fallback=True,
            message=(
                "The coach is unavailable, so the plan was built by the deterministic "
                "scheduler. Nothing else is affected."
            ),
        )

    session.add(run)
    await session.flush()

    validated = validate(outcome.prescription, facts)
    record = await _record(session, user, run, outcome.prescription, validated)

    if validated.result is ValidationResult.REJECTED:
        run.output_summary = "Prescription rejected by validation."
        return CoachResult(
            run=run,
            prescription=outcome.prescription,
            validation=validated,
            used_fallback=True,
            message=(
                "The coach's suggestion could not be applied — "
                + "; ".join(v["detail"] for v in validated.violations)
                + ". The deterministic scheduler built the plan instead."
            ),
        )

    run.output_summary = f"{validated.result.value}: {outcome.prescription.diagnosis[:200]}"
    await session.flush()

    message = outcome.prescription.rationale
    if validated.result is ValidationResult.CLAMPED:
        message += (
            " (Adjusted to fit: " + "; ".join(v["detail"] for v in validated.violations) + ".)"
        )

    logger.info("coach prescription %s for %s", validated.result.value, record.id)
    return CoachResult(
        run=run,
        prescription=validated.prescription,
        validation=validated,
        used_fallback=False,
        message=message,
    )


async def _record(
    session: AsyncSession,
    user: User,
    run: AgentRun,
    prescription: Prescription,
    validated: ValidatedPrescription,
) -> PrescriptionRecord:
    """Persist what was asked for and what the mechanism layer made of it.

    The original is stored, not the clamped version — otherwise the record would
    quietly agree with whatever was applied and the audit trail would be useless.
    """
    record = PrescriptionRecord(
        user_id=user.id,
        agent_run_id=run.id,
        focus_patterns=[
            {"pattern_id": str(p.pattern_id), "weight": p.weight}
            for p in prescription.focus_patterns
        ],
        rating_band_low=prescription.rating_band[0],
        rating_band_high=prescription.rating_band[1],
        size=prescription.size,
        mix={
            "weakness": prescription.mix[0],
            "interleaved": prescription.mix[1],
            "retention": prescription.mix[2],
        },
        timed=prescription.timed,
        diagnosis=prescription.diagnosis,
        rationale=prescription.rationale,
        evidence_attempt_ids=[str(i) for i in prescription.evidence_attempt_ids],
        confidence=prescription.confidence,
    )
    session.add(record)
    await session.flush()

    session.add(
        PrescriptionValidation(
            prescription_id=record.id,
            result=validated.result,
            violations=list(validated.violations),
        )
    )
    await session.flush()
    return record


async def pending_trigger(session: AsyncSession, user: User) -> TriggerBatch | None:
    """The most recent material change still waiting for the coach."""
    return (
        await session.execute(
            select(TriggerBatch)
            .where(
                TriggerBatch.user_id == user.id,
                TriggerBatch.outcome == TriggerOutcome.PENDING_AGENT,
            )
            .order_by(TriggerBatch.evaluated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def recent_runs(session: AsyncSession, user: User, limit: int = 20) -> list[AgentRun]:
    return list(
        (
            await session.execute(
                select(AgentRun)
                .where(AgentRun.user_id == user.id)
                .order_by(AgentRun.started_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


async def current_plan(session: AsyncSession, user: User) -> Plan | None:
    return (
        await session.execute(
            select(Plan)
            .where(Plan.user_id == user.id)
            .options(selectinload(Plan.items).selectinload(PlanItem.problem))
            .order_by(Plan.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


__all__ = [
    "AGENT_NAME",
    "CoachResult",
    "TriggerOutcome",
    "build_context",
    "current_plan",
    "pending_trigger",
    "recent_runs",
    "run_coach",
]
