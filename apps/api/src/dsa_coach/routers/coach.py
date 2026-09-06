"""Coach endpoints (Phase 4).

Every route here can complete without the AI provider. That is not a courtesy —
it is invariant 4, and it is why `used_fallback` is part of the response rather
than something hidden in a log.
"""

from fastapi import APIRouter, Depends

from dsa_coach.auth import CurrentUser, DbSession, Scope, require_scope
from dsa_coach.models import ValidationResult
from dsa_coach.schemas import AgentRunOut, CoachRunOut, PlanOut, ViolationOut
from dsa_coach.services import coach as coach_service
from dsa_coach.services import scheduling as scheduling_service

router = APIRouter(tags=["coach"], dependencies=[Depends(require_scope(Scope.DASHBOARD))])


@router.post("/coach/prescribe", response_model=CoachRunOut)
async def prescribe(user: CurrentUser, session: DbSession) -> CoachRunOut:
    """Ask the coach for the next block, then build it.

    The coach prescribes a shape. The mechanism layer validates it. The
    deterministic scheduler picks the problems. If any of that falls through,
    the scheduler runs alone and the response says so.
    """
    pending = await coach_service.pending_trigger(session, user)
    reasons = tuple(pending.reasons) if pending else ()

    result = await coach_service.run_coach(
        session,
        user,
        trigger="material_change" if pending else "manual",
        trigger_reasons=reasons,
    )

    block = await scheduling_service.build_next_block(
        session, user, prescription=result.prescription
    )

    if pending is not None and not result.used_fallback:
        pending.outcome = coach_service.TriggerOutcome.PRESCRIBED
        pending.agent_run_id = result.run.id

    validation = result.validation
    return CoachRunOut(
        run_id=result.run.id,
        runtime=result.run.runtime,
        model=result.run.model,
        status=result.run.status.value,
        used_fallback=result.used_fallback,
        validation=(None if validation is None else validation.result.value),
        violations=[
            ViolationOut(kind=v["kind"], detail=v["detail"])
            for v in (validation.violations if validation else ())
        ],
        diagnosis=result.prescription.diagnosis if result.prescription else None,
        message=result.message,
        plan=PlanOut.model_validate(block.plan),
    )


@router.get("/coach/runs", response_model=list[AgentRunOut])
async def runs(user: CurrentUser, session: DbSession) -> list[AgentRunOut]:
    """Every coach invocation, including the ones that failed (spec §7.4)."""
    return [AgentRunOut.model_validate(r) for r in await coach_service.recent_runs(session, user)]


__all__ = ["ValidationResult", "router"]
