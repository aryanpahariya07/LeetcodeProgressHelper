"""Progress and plan-generation endpoints (Phase 1)."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from dsa_coach.auth import CurrentUser, DbSession, Scope, require_scope
from dsa_coach.mechanism.prerequisites import PrerequisiteEdge, evaluate_unlocks
from dsa_coach.mechanism.readiness import MODELS, PRIMARY_MODEL
from dsa_coach.models import Pattern, PatternPrerequisite
from dsa_coach.schemas import (
    BlockResultOut,
    PatternReadinessOut,
    PlacementOut,
    PlanOut,
    ReadinessReportOut,
    RetentionOut,
    TriggerBatchOut,
    UnlockOut,
)
from dsa_coach.services import placement as placement_service
from dsa_coach.services import readiness as readiness_service
from dsa_coach.services import retention as retention_service
from dsa_coach.services import scheduling as scheduling_service
from dsa_coach.services import triggers as trigger_service

router = APIRouter(tags=["progress"], dependencies=[Depends(require_scope(Scope.DASHBOARD))])

DISCLAIMER = (
    "Readiness is an uncalibrated estimate, shown as a band rather than a "
    "percentage. It has not yet been validated against actual outcomes."
)


@router.get("/progress/readiness", response_model=ReadinessReportOut)
async def readiness(
    user: CurrentUser,
    session: DbSession,
    model_version: Annotated[str | None, Query()] = None,
) -> ReadinessReportOut:
    """Readiness per pattern.

    `model_version` lets the two models be compared directly; it defaults to the
    primary one (spec §6.3).
    """
    version = model_version if model_version in MODELS else PRIMARY_MODEL
    report = await readiness_service.readiness_report(session, user, version)

    patterns = [
        PatternReadinessOut(
            pattern_id=row.pattern_id,
            slug=row.slug,
            name=row.name,
            band=row.prediction.band,
            calibrated=row.prediction.calibrated,
            evidence_count=row.evidence_count,
            estimate=round(row.prediction.score, 4),
            uncertainty=round(row.prediction.uncertainty, 4),
        )
        for row in report
    ]

    return ReadinessReportOut(
        model_version=version,
        patterns=patterns,
        calibrated_count=sum(1 for p in patterns if p.calibrated),
        total_count=len(patterns),
        disclaimer=DISCLAIMER,
    )


@router.get("/progress/retention", response_model=RetentionOut)
async def retention(user: CurrentUser, session: DbSession) -> RetentionOut:
    summary = await retention_service.summary(session, user, datetime.now(UTC))
    return RetentionOut(tracked=summary.tracked, due=summary.due, lapses=summary.lapses)


@router.get("/progress/unlocks", response_model=list[UnlockOut])
async def unlocks(user: CurrentUser, session: DbSession) -> list[UnlockOut]:
    """Which patterns are available, and what is holding the rest back."""
    predictions = await readiness_service.pattern_predictions(session, user)
    patterns = (await session.execute(select(Pattern).order_by(Pattern.slug))).scalars().all()
    slugs = {p.id: p.slug for p in patterns}

    edges = [
        PrerequisiteEdge(r.pattern_id, r.requires_pattern_id, r.strength)
        for r in (await session.execute(select(PatternPrerequisite))).scalars().all()
    ]
    states = evaluate_unlocks(set(slugs), edges, predictions)

    return [
        UnlockOut(
            slug=slugs[pattern_id],
            unlocked=state.unlocked,
            provisional=state.provisional,
            blocked_by=[slugs[b] for b in state.blocked_by if b in slugs],
            unknown_prerequisites=[slugs[u] for u in state.unknown_prerequisites if u in slugs],
            reason=state.reason,
        )
        for pattern_id, state in sorted(states.items(), key=lambda kv: slugs[kv[0]])
    ]


@router.get("/progress/placement", response_model=PlacementOut)
async def placement(user: CurrentUser, session: DbSession) -> PlacementOut:
    """How far placement has got.

    Placement runs through ordinary practice (spec §9). An incomplete placement
    never blocks anything — it only means the plan is still provisional.
    """
    progress = await placement_service.progress(session, user)
    return PlacementOut(
        complete=progress.complete,
        attempts=progress.attempts,
        max_attempts=progress.max_attempts,
        remaining=progress.remaining,
        covered=progress.covered,
        calibrated=progress.calibrated,
        target=progress.target,
        reason=progress.reason,
    )


@router.post("/plan/next-block", response_model=BlockResultOut)
async def next_block(user: CurrentUser, session: DbSession) -> BlockResultOut:
    """Build the next block deterministically.

    No AI is involved. This is the path that keeps working when the coach is
    unavailable (invariant 4).
    """
    result = await scheduling_service.build_next_block(session, user)
    progress = result.placement
    return BlockResultOut(
        plan=PlanOut.model_validate(result.plan),
        placement=(
            None
            if progress is None
            else PlacementOut(
                complete=progress.complete,
                attempts=progress.attempts,
                max_attempts=progress.max_attempts,
                remaining=progress.remaining,
                covered=progress.covered,
                calibrated=progress.calibrated,
                target=progress.target,
                reason=progress.reason,
            )
        ),
        focus_patterns=list(result.focus_pattern_slugs),
        locked_patterns=list(result.locked_pattern_slugs),
        total_minutes=result.block.total_minutes,
        budget_minutes=result.block.budget_minutes,
        shortfalls=list(result.block.shortfalls),
    )


@router.get("/plan/triggers", response_model=list[TriggerBatchOut])
async def triggers(user: CurrentUser, session: DbSession) -> list[TriggerBatchOut]:
    """Every three-attempt evaluation, including the ones that changed nothing.

    An explained no-change is a first-class result (spec §6.6), not silence.
    """
    batches = await trigger_service.recent_batches(session, user)
    return [TriggerBatchOut.model_validate(b) for b in batches]
