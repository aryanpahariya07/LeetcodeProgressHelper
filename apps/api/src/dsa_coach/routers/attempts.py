"""Manual attempt logging — the permanent fallback path (spec §4.3, CAP-11).

Built in Phase 0 and never removed: if the extension breaks, or before it exists,
this is how evidence gets in.
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from dsa_coach import tuning
from dsa_coach.auth import CurrentUser, DbSession, Scope, require_scope
from dsa_coach.ingest import ingest_events
from dsa_coach.models import Attempt, AttemptSource
from dsa_coach.schemas import (
    AmendmentResultOut,
    AttemptAmendIn,
    AttemptEventIn,
    AttemptOut,
    EventResultOut,
)
from dsa_coach.services import readiness as readiness_service

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


@router.patch("/attempts/{attempt_id}", response_model=AmendmentResultOut)
async def amend_attempt(
    attempt_id: UUID,
    payload: AttemptAmendIn,
    user: CurrentUser,
    session: DbSession,
) -> AmendmentResultOut:
    """Correct an attempt, and rebuild everything derived from it (spec §3.3).

    The questionnaire fires at submission, but "solved after the editorial"
    usually happens afterwards — you fail, dismiss the prompt, read the
    editorial, then solve. The prompt fires at the wrong moment to capture the
    thing it most needs, so the correction path is not a nicety.

    Two properties matter here:

    - **Corrections are recorded, not overwritten.** The previous values are
      appended to `prior_values`, so the history of what you said and when
      survives the correction.
    - **The correction actually reaches the numbers.** Readiness is folded
      forward incrementally, so a corrected attempt cannot be edited in place;
      the models are replayed. `attempts_replayed` is returned rather than
      logged, because an amendment that silently changed nothing is exactly the
      failure this endpoint was written to fix.
    """
    attempt = (
        await session.execute(
            select(Attempt)
            .where(Attempt.id == attempt_id, Attempt.user_id == user.id)
            .options(selectinload(Attempt.problem))
        )
    ).scalar_one_or_none()
    if attempt is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown attempt.")

    cutoff = datetime.now(UTC) - timedelta(days=tuning.AMENDMENT_WINDOW_DAYS)
    if attempt.submitted_at < cutoff:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Attempts are amendable for {tuning.AMENDMENT_WINDOW_DAYS} days. "
                "This one is older than that."
            ),
        )

    changes: dict[str, object] = {}
    if payload.resolution is not None and payload.resolution != attempt.resolution:
        changes["resolution"] = attempt.resolution.value
        attempt.resolution = payload.resolution
    if payload.clear_blocker:
        if attempt.blocker is not None:
            changes["blocker"] = attempt.blocker.value
            attempt.blocker = None
    elif payload.blocker is not None and payload.blocker != attempt.blocker:
        changes["blocker"] = attempt.blocker.value if attempt.blocker else None
        attempt.blocker = payload.blocker
    if (
        payload.confidence_cold_redo is not None
        and payload.confidence_cold_redo != attempt.confidence_cold_redo
    ):
        changes["confidence_cold_redo"] = attempt.confidence_cold_redo
        attempt.confidence_cold_redo = payload.confidence_cold_redo
    if payload.notes is not None and payload.notes != attempt.notes:
        changes["notes"] = attempt.notes
        attempt.notes = payload.notes

    if not changes:
        # Nothing to record and nothing to replay. Returning the attempt
        # unchanged is honest; stamping `amended_at` would claim a correction
        # that did not happen.
        return AmendmentResultOut(
            attempt=AttemptOut.model_validate(attempt),
            attempts_replayed=0,
            changed_fields=[],
        )

    amended_at = datetime.now(UTC)
    # Append rather than replace: `prior_values` is the record of every
    # correction, not just the most recent one.
    history = list(attempt.prior_values or [])
    history.append({"amended_at": amended_at.isoformat(), "was": changes})
    attempt.prior_values = history
    attempt.amended_at = amended_at
    await session.flush()

    result = await readiness_service.recompute(session, user)

    return AmendmentResultOut(
        attempt=AttemptOut.model_validate(attempt),
        attempts_replayed=result.attempts_replayed,
        changed_fields=sorted(changes),
    )
