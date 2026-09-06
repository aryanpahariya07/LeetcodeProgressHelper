"""Plan and Today."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from dsa_coach.auth import CurrentUser, DbSession, Scope, require_scope
from dsa_coach.models import ItemStatus, Plan, PlanItem, PlanStatus
from dsa_coach.schemas import PlanItemOut, PlanOut, TodayOut

router = APIRouter(tags=["plan"], dependencies=[Depends(require_scope(Scope.DASHBOARD))])


async def _current_plan(session: DbSession, user_id: object) -> Plan | None:
    return (
        await session.execute(
            select(Plan)
            .where(
                Plan.user_id == user_id,
                Plan.status.in_([PlanStatus.PROVISIONAL, PlanStatus.ACTIVE]),
            )
            .options(selectinload(Plan.items).selectinload(PlanItem.problem))
            .order_by(Plan.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


@router.get("/plan/current", response_model=PlanOut)
async def current_plan(user: CurrentUser, session: DbSession) -> PlanOut:
    plan = await _current_plan(session, user.id)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No plan yet.")
    return PlanOut.model_validate(plan)


@router.get("/today", response_model=TodayOut)
async def today(user: CurrentUser, session: DbSession) -> TodayOut:
    """The first block with outstanding work.

    Phase 0 has no scheduler, so "today" is simply the earliest incomplete block
    of the provisional plan rather than a date-driven selection.
    """
    plan = await _current_plan(session, user.id)
    if plan is None:
        return TodayOut(
            plan=None, block_id=None, items=[], total_target_minutes=0, is_provisional=False
        )

    pending = [i for i in plan.items if i.status == ItemStatus.PENDING]
    if not pending:
        return TodayOut(
            plan=PlanOut.model_validate(plan),
            block_id=None,
            items=[],
            total_target_minutes=0,
            is_provisional=plan.status == PlanStatus.PROVISIONAL,
        )

    block_id = pending[0].block_id
    items = [i for i in pending if i.block_id == block_id]

    return TodayOut(
        plan=PlanOut.model_validate(plan),
        block_id=block_id,
        items=[PlanItemOut.model_validate(i) for i in items],
        total_target_minutes=sum(i.target_minutes for i in items),
        is_provisional=plan.status == PlanStatus.PROVISIONAL,
    )
