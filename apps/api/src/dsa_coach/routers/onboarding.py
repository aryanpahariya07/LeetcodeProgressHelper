"""Onboarding — creates the user, the goal, and provisional plan version 1."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from dsa_coach.auth import CurrentUser
from dsa_coach.curriculum import build_provisional_plan
from dsa_coach.db import get_session
from dsa_coach.models import Plan, PlanItem, User, UserGoal
from dsa_coach.schemas import GoalOut, OnboardingIn, OnboardingOut, PlanOut, UserOut

router = APIRouter(tags=["onboarding"])


@router.post("/onboarding", response_model=OnboardingOut, status_code=status.HTTP_201_CREATED)
async def onboard(
    payload: OnboardingIn,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> OnboardingOut:
    """Create the single local user and hand back a plan immediately.

    The plan is *provisional* (spec §9): the user is never blocked from starting
    while the system works out where they actually stand.
    """
    existing = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Onboarding already completed. This build is single-user.",
        )

    user = User(
        display_name=payload.display_name,
        timezone=payload.timezone,
        preferred_language=payload.preferred_language,
    )
    session.add(user)
    await session.flush()

    goal = UserGoal(
        user_id=user.id,
        target_companies=payload.target_companies,
        target_date=payload.target_date,
        days_per_week=payload.days_per_week,
        minutes_per_day=payload.minutes_per_day,
        self_assessed_level=payload.self_assessed_level,
        approx_problems_solved=payload.approx_problems_solved,
    )
    session.add(goal)
    await session.flush()

    plan = await build_provisional_plan(session, user, goal)
    loaded = await _load_plan(session, plan.id)

    return OnboardingOut(
        user=UserOut.model_validate(user),
        goal=GoalOut.model_validate(goal),
        plan=PlanOut.model_validate(loaded),
    )


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)


async def _load_plan(session: AsyncSession, plan_id: object) -> Plan:
    return (
        await session.execute(
            select(Plan)
            .where(Plan.id == plan_id)
            .options(selectinload(Plan.items).selectinload(PlanItem.problem))
        )
    ).scalar_one()
