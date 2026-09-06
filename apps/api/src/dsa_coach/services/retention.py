"""Retention service — database glue around FSRS (spec §6.5)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from fsrs import Rating
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dsa_coach.mechanism.retention import (
    ResolvePerformance,
    ReviewState,
    apply_review,
    is_lapsed,
    schedule_first_solve,
)
from dsa_coach.models import Attempt, Resolution, ReviewSchedule, SubmissionOutcome, User

SOLVED_RESOLUTIONS = {
    Resolution.INDEPENDENT,
    Resolution.AFTER_HINT,
    Resolution.AFTER_EDITORIAL,
}
HELPED_RESOLUTIONS = {Resolution.AFTER_HINT, Resolution.AFTER_EDITORIAL}


@dataclass(frozen=True)
class RetentionOutcome:
    scheduled: bool
    grade: Rating | None
    lapsed: bool
    due_at: datetime | None


def attempt_solved(attempt: Attempt) -> bool:
    """Whether the attempt represents a solve.

    A dismissed questionnaire falls back to the judge result — that much is
    objective even when the subjective half is missing.
    """
    if attempt.resolution in SOLVED_RESOLUTIONS:
        return True
    if attempt.resolution is Resolution.UNKNOWN:
        return attempt.submission_outcome is SubmissionOutcome.ACCEPTED
    return False


async def _row(session: AsyncSession, user_id: UUID, problem_id: UUID) -> ReviewSchedule | None:
    return (
        await session.execute(
            select(ReviewSchedule).where(
                ReviewSchedule.user_id == user_id, ReviewSchedule.problem_id == problem_id
            )
        )
    ).scalar_one_or_none()


def _to_state(row: ReviewSchedule) -> ReviewState:
    return ReviewState(
        stability=row.stability,
        difficulty=row.difficulty,
        due_at=row.due_at,
        last_reviewed_at=row.last_reviewed_at,
        lapses=row.lapses,
        reps=row.reps,
        fsrs_state=row.fsrs_state,
        fsrs_step=row.fsrs_step,
        fsrs_version=row.fsrs_version,
    )


def _write(row: ReviewSchedule, state: ReviewState) -> None:
    row.stability = state.stability
    row.difficulty = state.difficulty
    row.due_at = state.due_at
    row.last_reviewed_at = state.last_reviewed_at
    row.lapses = state.lapses
    row.reps = state.reps
    row.fsrs_state = state.fsrs_state
    row.fsrs_step = state.fsrs_step
    row.fsrs_version = state.fsrs_version


async def _first_exposure_seconds(
    session: AsyncSession, user_id: UUID, problem_id: UUID
) -> int | None:
    """The user's own time on their first attempt at this problem."""
    return (
        await session.execute(
            select(Attempt.active_seconds)
            .where(
                Attempt.user_id == user_id,
                Attempt.problem_id == problem_id,
                Attempt.is_resolve.is_(False),
            )
            .order_by(Attempt.submitted_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def apply_attempt(session: AsyncSession, user: User, attempt: Attempt) -> RetentionOutcome:
    """Update the retention schedule from one attempt."""
    existing = await _row(session, user.id, attempt.problem_id)

    if attempt.is_resolve:
        if existing is None:
            # A re-solve of something never scheduled: start the schedule rather
            # than discarding the evidence.
            if not attempt_solved(attempt):
                return RetentionOutcome(False, None, False, None)
            state = schedule_first_solve(attempt.submitted_at)
            session.add(_new_row(user.id, attempt.problem_id, state))
            await session.flush()
            return RetentionOutcome(True, None, False, state.due_at)

        performance = ResolvePerformance(
            solved=attempt_solved(attempt),
            used_help=attempt.resolution in HELPED_RESOLUTIONS,
            submit_count=attempt.submit_count,
            active_seconds=attempt.active_seconds,
            baseline_active_seconds=await _first_exposure_seconds(
                session, user.id, attempt.problem_id
            ),
        )
        state, grade = apply_review(_to_state(existing), performance, attempt.submitted_at)
        _write(existing, state)
        await session.flush()
        return RetentionOutcome(True, grade, is_lapsed(grade), state.due_at)

    # First exposure: start a retention schedule once it has actually been solved.
    if existing is not None or not attempt_solved(attempt):
        return RetentionOutcome(False, None, False, existing.due_at if existing else None)

    state = schedule_first_solve(attempt.submitted_at)
    session.add(_new_row(user.id, attempt.problem_id, state))
    await session.flush()
    return RetentionOutcome(True, None, False, state.due_at)


def _new_row(user_id: UUID, problem_id: UUID, state: ReviewState) -> ReviewSchedule:
    return ReviewSchedule(
        user_id=user_id,
        problem_id=problem_id,
        stability=state.stability,
        difficulty=state.difficulty,
        due_at=state.due_at,
        last_reviewed_at=state.last_reviewed_at,
        lapses=state.lapses,
        reps=state.reps,
        fsrs_state=state.fsrs_state,
        fsrs_step=state.fsrs_step,
        fsrs_version=state.fsrs_version,
    )


async def due_problem_ids(session: AsyncSession, user: User, at: datetime) -> list[UUID]:
    """Problems whose re-solve is due, most overdue first."""
    return list(
        (
            await session.execute(
                select(ReviewSchedule.problem_id)
                .where(ReviewSchedule.user_id == user.id, ReviewSchedule.due_at <= at)
                .order_by(ReviewSchedule.due_at.asc())
            )
        )
        .scalars()
        .all()
    )


@dataclass(frozen=True)
class RetentionSummary:
    tracked: int
    due: int
    lapses: int


async def summary(session: AsyncSession, user: User, at: datetime) -> RetentionSummary:
    rows = (
        (await session.execute(select(ReviewSchedule).where(ReviewSchedule.user_id == user.id)))
        .scalars()
        .all()
    )
    return RetentionSummary(
        tracked=len(rows),
        due=sum(1 for r in rows if r.due_at <= at),
        lapses=sum(r.lapses for r in rows),
    )
