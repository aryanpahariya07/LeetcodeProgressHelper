"""Run snapshots and unfinished episodes (spec §3.6).

Deterministic throughout. Storing a run, working out which problems are still
open, and closing an episode are all plain queries — the coach is only involved
in producing the conclusion itself, and that happens elsewhere.

The organising idea: **snapshots with no `conclusion_id` are the open episode**
for that problem. There is no episode table. Runs arrive before any attempt
exists, and a problem picked up across several sittings has runs that belong
together with no submission to hang them on, so the open set is the only
grouping that survives both cases.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dsa_coach import tuning
from dsa_coach.models import (
    Attempt,
    AttemptConclusion,
    AttemptSnapshot,
    EpisodeOutcome,
    Problem,
    SnapshotKind,
    SubmissionOutcome,
    User,
)
from dsa_coach.services import consent as consent_service


@dataclass(frozen=True)
class UnfinishedProblem:
    """A problem worked on but not solved — one row of the web app's list."""

    problem_id: uuid.UUID
    slug: str
    title: str
    url: str
    run_count: int
    submit_count: int
    first_seen_at: datetime
    last_seen_at: datetime


async def store(
    session: AsyncSession,
    user: User,
    *,
    snapshot_uuid: uuid.UUID,
    problem: Problem,
    kind: SnapshotKind,
    code: str,
    language: str | None,
    captured_at: datetime,
) -> AttemptSnapshot | None:
    """Record one Run or Submit's source.

    Returns None when consent does not allow storing code (invariant 9) — the
    check is here rather than at the endpoint so no caller can skip it — and
    also on a replay, since `snapshot_uuid` is the idempotency key (invariant 7).
    """
    current = await consent_service.state(session, user)
    if not current.may_store:
        return None

    existing = (
        await session.execute(
            select(AttemptSnapshot).where(AttemptSnapshot.snapshot_uuid == snapshot_uuid)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    snapshot = AttemptSnapshot(
        snapshot_uuid=snapshot_uuid,
        user_id=user.id,
        problem_id=problem.id,
        kind=kind,
        language=language,
        code=code,
        captured_at=captured_at,
        retention_until=datetime.now(UTC) + timedelta(days=tuning.SNAPSHOT_RETENTION_DAYS),
    )
    session.add(snapshot)
    await session.flush()
    return snapshot


async def open_episode(
    session: AsyncSession, user: User, problem_id: uuid.UUID
) -> list[AttemptSnapshot]:
    """Every snapshot for this problem not yet consumed by a conclusion.

    Chronological, because the sequence is the whole point: what distinguishes a
    clean solve from four passes at the same off-by-one is the order they came
    in, not the final source.
    """
    rows = (
        (
            await session.execute(
                select(AttemptSnapshot)
                .where(
                    AttemptSnapshot.user_id == user.id,
                    AttemptSnapshot.problem_id == problem_id,
                    AttemptSnapshot.conclusion_id.is_(None),
                )
                .order_by(AttemptSnapshot.captured_at, AttemptSnapshot.id)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def unfinished(session: AsyncSession, user: User) -> list[UnfinishedProblem]:
    """Problems with an open episode and no accepted submission.

    Surfaced in the web app rather than swept up by a timer, because only the
    user knows the difference between "gave up" and "coming back tomorrow", and
    those produce opposite conclusions from identical data (spec §3.6).
    """
    solved = (
        select(Attempt.problem_id)
        .where(
            Attempt.user_id == user.id,
            Attempt.submission_outcome == SubmissionOutcome.ACCEPTED,
        )
        .scalar_subquery()
    )

    rows = (
        await session.execute(
            select(
                Problem.id,
                Problem.slug,
                Problem.title,
                Problem.url,
                func.sum(case((AttemptSnapshot.kind == SnapshotKind.RUN, 1), else_=0)).label(
                    "runs"
                ),
                func.sum(case((AttemptSnapshot.kind == SnapshotKind.SUBMIT, 1), else_=0)).label(
                    "submits"
                ),
                func.min(AttemptSnapshot.captured_at).label("first_seen"),
                func.max(AttemptSnapshot.captured_at).label("last_seen"),
            )
            .join(Problem, Problem.id == AttemptSnapshot.problem_id)
            .where(
                AttemptSnapshot.user_id == user.id,
                AttemptSnapshot.conclusion_id.is_(None),
                AttemptSnapshot.problem_id.not_in(solved),
            )
            .group_by(Problem.id, Problem.slug, Problem.title, Problem.url)
            .order_by(func.max(AttemptSnapshot.captured_at).desc())
        )
    ).all()

    return [
        UnfinishedProblem(
            problem_id=row[0],
            slug=row[1],
            title=row[2],
            url=row[3],
            run_count=int(row[4] or 0),
            submit_count=int(row[5] or 0),
            first_seen_at=row[6],
            last_seen_at=row[7],
        )
        for row in rows
    ]


async def purge_expired(session: AsyncSession, now: datetime | None = None) -> int:
    """Delete snapshots past their retention window (spec §3.6, §8).

    The conclusion drawn from them is unaffected — that is the point of keeping
    the two apart. Raw code has a 30-day life; what was learned from it does not.
    """
    now = now or datetime.now(UTC)
    rows = (
        (
            await session.execute(
                select(AttemptSnapshot).where(AttemptSnapshot.retention_until <= now)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        await session.delete(row)
    await session.flush()
    return len(rows)


async def abandon(
    session: AsyncSession, user: User, problem_id: uuid.UUID
) -> AttemptConclusion | None:
    """Close an open episode the user has given up on (spec §3.6).

    Returns None when there is nothing open — abandoning a problem you never
    started, or one already closed, is a no-op rather than an error.

    A conclusion row is created and the snapshots are attached to it, which is
    what takes the problem off the unfinished list and stops the next episode
    inheriting these runs. The *contents* of the conclusion are filled in
    separately by the coach; this only closes the episode, so giving up never
    waits on a model and works with the provider down (invariant 4).
    """
    snapshots = await open_episode(session, user, problem_id)
    if not snapshots:
        return None

    conclusion = AttemptConclusion(
        user_id=user.id,
        problem_id=problem_id,
        attempt_id=None,
        outcome=EpisodeOutcome.ABANDONED,
        runs_before_pass=sum(1 for s in snapshots if s.kind is SnapshotKind.RUN),
    )
    session.add(conclusion)
    await session.flush()

    for snapshot in snapshots:
        snapshot.conclusion_id = conclusion.id
    await session.flush()
    return conclusion
