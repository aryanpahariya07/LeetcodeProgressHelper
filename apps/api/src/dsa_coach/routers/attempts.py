"""Manual attempt logging — the permanent fallback path (spec §4.3, CAP-11).

Built in Phase 0 and never removed: if the extension breaks, or before it exists,
this is how evidence gets in.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from dsa_coach.auth import CurrentUser, DbSession, Scope, require_scope
from dsa_coach.ingest import ingest_events
from dsa_coach.models import Attempt, AttemptSource
from dsa_coach.schemas import AttemptEventIn, AttemptOut, EventResultOut

router = APIRouter(tags=["attempts"], dependencies=[Depends(require_scope(Scope.DASHBOARD))])


@router.post("/attempts", response_model=EventResultOut, status_code=status.HTTP_201_CREATED)
async def log_attempt(
    payload: AttemptEventIn,
    user: CurrentUser,
    session: DbSession,
) -> EventResultOut:
    """Log one attempt by hand. Idempotent by `event_uuid` like every other path."""
    results = await ingest_events(session, user, [payload], AttemptSource.MANUAL)
    return results[0]


@router.get("/attempts", response_model=list[AttemptOut])
async def list_attempts(
    user: CurrentUser,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AttemptOut]:
    rows = (
        (
            await session.execute(
                select(Attempt)
                .where(Attempt.user_id == user.id)
                .options(selectinload(Attempt.problem))
                .order_by(Attempt.submitted_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [AttemptOut.model_validate(row) for row in rows]
