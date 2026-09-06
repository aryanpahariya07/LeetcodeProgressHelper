"""Health check."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from dsa_coach import __version__
from dsa_coach.db import get_session
from dsa_coach.models import Problem
from dsa_coach.schemas import HealthOut

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut)
async def health(session: Annotated[AsyncSession, Depends(get_session)]) -> HealthOut:
    try:
        count = (
            await session.execute(
                select(func.count()).select_from(Problem).where(Problem.is_active.is_(True))
            )
        ).scalar_one()
        database: str = "ok"
    except SQLAlchemyError:
        count = 0
        database = "unavailable"

    return HealthOut(
        status="ok",
        version=__version__,
        database=database,
        catalogue_problems=count,
    )
