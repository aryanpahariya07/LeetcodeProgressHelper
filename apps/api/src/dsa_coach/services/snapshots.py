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

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dsa_coach import tuning
from dsa_coach.coach.conclusion_runtime import (
    ConclusionOutcome,
    ConclusionRequest,
    SnapshotView,
    build_conclusion_runtime,
)
from dsa_coach.coach.runtime import CoachFailure
from dsa_coach.mechanism import defects as defect_vocab
from dsa_coach.models import (
    Attempt,
    AttemptConclusion,
    AttemptSnapshot,
    Blocker,
    EpisodeOutcome,
    Pattern,
    Problem,
    ProblemPattern,
    SnapshotKind,
    SubmissionOutcome,
    User,
)
from dsa_coach.services import consent as consent_service

logger = logging.getLogger(__name__)


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
                .order_by(
                    AttemptSnapshot.captured_at,
                    # Insertion order breaks a tie. Two runs can share a
                    # millisecond, and falling through to a random UUID would
                    # reorder the sequence — which is the one thing the
                    # conclusion actually reads.
                    AttemptSnapshot.created_at,
                    AttemptSnapshot.id,
                )
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


class ConclusionRuntime(Protocol):
    """Anything that can turn a run sequence into a conclusion.

    Exists so tests can inject a scripted double: a real call costs a ChatGPT
    quota and takes tens of seconds, and neither belongs in a suite that runs on
    every change.
    """

    name: str

    async def conclude(self, request: ConclusionRequest) -> ConclusionOutcome: ...


async def conclude(
    session: AsyncSession,
    user: User,
    conclusion_row: AttemptConclusion,
    runtime: ConclusionRuntime | None = None,
) -> ConclusionOutcome:
    """Ask the coach what the run sequence shows, and record the answer.

    The episode is already closed by the time this runs — `abandon` and the
    solve path both attach the snapshots first. That ordering is deliberate:
    closing must not depend on a model, so giving up works with the provider
    down and a failed conclusion leaves the episode closed and empty rather
    than reopening it (invariant 4).

    Everything written here is judgment. It lands on `attempt_conclusions` and
    never on `attempts`, and `confidence` is stored alongside so downstream can
    weight it rather than take it at face value (invariant 1).
    """
    snapshots = (
        (
            await session.execute(
                select(AttemptSnapshot)
                .where(AttemptSnapshot.conclusion_id == conclusion_row.id)
                .order_by(
                    AttemptSnapshot.captured_at,
                    AttemptSnapshot.created_at,
                    AttemptSnapshot.id,
                )
            )
        )
        .scalars()
        .all()
    )
    if not snapshots:
        return ConclusionOutcome(
            failure=CoachFailure.INVALID_OUTPUT, error_detail="No snapshots attached."
        )

    problem = (
        await session.execute(select(Problem).where(Problem.id == conclusion_row.problem_id))
    ).scalar_one()

    patterns = (
        await session.execute(
            select(ProblemPattern.pattern_id, Pattern.slug)
            .join(Pattern, Pattern.id == ProblemPattern.pattern_id)
            .where(ProblemPattern.problem_id == problem.id)
        )
    ).all()

    runtime = runtime or build_conclusion_runtime()
    outcome = await runtime.conclude(
        ConclusionRequest(
            problem_slug=problem.slug,
            problem_title=problem.title,
            language=snapshots[-1].language,
            candidate_patterns=tuple((str(pid), slug) for pid, slug in patterns),
            outcome=conclusion_row.outcome.value,
            snapshots=tuple(
                SnapshotView(ordinal=index + 1, kind=snap.kind.value, code=snap.code)
                for index, snap in enumerate(snapshots)
            ),
        )
    )

    conclusion_row.runtime = runtime.name
    conclusion_row.model = outcome.model
    conclusion_row.vocabulary_version = defect_vocab.VOCABULARY_VERSION

    if outcome.conclusion is not None:
        result = outcome.conclusion
        conclusion_row.patterns_used = result.patterns_used
        conclusion_row.blocker_observed = (
            Blocker(result.blocker_observed) if result.blocker_observed else None
        )
        conclusion_row.final_complexity = result.final_complexity
        conclusion_row.runs_before_pass = result.runs_before_pass
        conclusion_row.approach_changed = result.approach_changed
        conclusion_row.converged_at_run = result.converged_at_run
        conclusion_row.defects = result.defects
        conclusion_row.confidence = result.confidence
        conclusion_row.notes = result.notes

    await session.flush()
    return outcome


async def close_solved(
    session: AsyncSession, user: User, attempt: Attempt
) -> AttemptConclusion | None:
    """Close the episode an accepted submission ends (spec §3.6).

    Deterministic and fast, exactly like `abandon`. It creates the conclusion
    row and attaches the snapshots; what the sequence *shows* is filled in
    afterwards by `conclude`, which may be slow and may fail.

    Splitting it this way is what keeps invariant 4: a submission is recorded
    and its episode closed whether or not the coach is reachable, and a failed
    conclusion leaves an empty row rather than an episode that reopens itself.

    Returns None when there is nothing open — solving a problem with no captured
    runs, which is the normal case without code-capture consent.
    """
    if attempt.submission_outcome is not SubmissionOutcome.ACCEPTED:
        return None

    snapshots = await open_episode(session, user, attempt.problem_id)
    if not snapshots:
        return None

    conclusion = AttemptConclusion(
        user_id=user.id,
        problem_id=attempt.problem_id,
        attempt_id=attempt.id,
        outcome=EpisodeOutcome.SOLVED,
        runs_before_pass=sum(1 for s in snapshots if s.kind is SnapshotKind.RUN),
    )
    session.add(conclusion)
    await session.flush()

    for snapshot in snapshots:
        snapshot.conclusion_id = conclusion.id
    await session.flush()
    return conclusion


async def conclude_later(user_id: uuid.UUID, conclusion_id: uuid.UUID) -> None:
    """Run the conclusion in the background, in its own session.

    Closing an episode is deterministic and fast; concluding is a Codex call
    that can take tens of seconds. Doing them in one request would make
    submitting a problem wait on a model, which is exactly what storing runs raw
    was meant to avoid (spec §3.6).

    Its own session, because the request's has already been committed and
    returned by the time this runs. Failures are logged and go no further: the
    episode is already closed, the evidence is already recorded, and a missing
    conclusion is a gap rather than a fault (invariant 4).
    """
    from dsa_coach.db import get_session_factory

    try:
        async with get_session_factory()() as session:
            row = await session.get(AttemptConclusion, conclusion_id)
            if row is None or row.user_id != user_id:
                return
            user = await session.get(User, user_id)
            if user is None:
                return

            outcome = await conclude(session, user, row)
            await session.commit()

            if outcome.conclusion is None:
                logger.info("No conclusion for episode %s: %s", conclusion_id, outcome.error_detail)
    except Exception:
        logger.exception("Conclusion failed for episode %s", conclusion_id)


def schedule_conclusion(conclusion: AttemptConclusion) -> None:
    """Kick off `conclude_later` without waiting for it.

    Fire-and-forget, with a reference held so the task is not garbage-collected
    mid-flight — a detail asyncio does not protect you from.
    """
    task = asyncio.create_task(conclude_later(conclusion.user_id, conclusion.id))
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


#: Strong references to in-flight conclusions. Without this, asyncio may collect
#: a running task and the conclusion silently never happens.
_BACKGROUND: set[asyncio.Task[None]] = set()
