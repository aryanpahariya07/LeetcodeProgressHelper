"""Teaching orchestration (spec §7.3).

Ties the hint ladder, code consent and complexity analysis to the runtime, and
records what happened. The recording matters more than it looks: the hint level
used is evidence, and an attempt solved at level 4 must not be scored as if it
were solved cold.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from dsa_coach.coach.runtime import CoachFailure
from dsa_coach.coach.teaching import (
    DiagnosisRequest,
    HintRequest,
    MockTurnRequest,
    PriorFailure,
    ReviewRequest,
    TeachingRuntime,
    build_teaching_runtime,
)
from dsa_coach.mechanism.complexity import ComplexityEstimate, estimate
from dsa_coach.mechanism.hints import check_hint, describe, next_level
from dsa_coach.models import (
    Attempt,
    Pattern,
    Problem,
    ProblemPattern,
    TeachingExchange,
    TeachingKind,
    User,
)
from dsa_coach.services import consent as consent_service


@dataclass(frozen=True)
class TeachingResult:
    ok: bool
    text: str
    kind: TeachingKind
    hint_level: int | None = None
    level_description: str | None = None
    degraded: bool = False
    runtime: str = ""
    cited_attempt_ids: tuple[UUID, ...] = ()
    reason: str = ""


async def _problem(session: AsyncSession, slug: str) -> Problem | None:
    return (await session.execute(select(Problem).where(Problem.slug == slug))).scalar_one_or_none()


async def _pattern_slugs(session: AsyncSession, problem_id: UUID) -> tuple[str, ...]:
    rows = (
        await session.execute(
            select(Pattern.slug)
            .join(ProblemPattern, ProblemPattern.pattern_id == Pattern.id)
            .where(ProblemPattern.problem_id == problem_id)
            .order_by(ProblemPattern.weight.desc())
        )
    ).scalars()
    return tuple(rows)


async def highest_hint_level(session: AsyncSession, user: User, problem_id: UUID) -> int:
    """The deepest hint already seen for this problem."""
    value = (
        await session.execute(
            select(TeachingExchange.hint_level)
            .where(
                TeachingExchange.user_id == user.id,
                TeachingExchange.problem_id == problem_id,
                TeachingExchange.kind == TeachingKind.HINT,
            )
            .order_by(TeachingExchange.hint_level.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return int(value or 0)


async def give_hint(
    session: AsyncSession,
    user: User,
    slug: str,
    requested_level: int | None = None,
    runtime: TeachingRuntime | None = None,
) -> TeachingResult:
    """One rung up the ladder — never two."""
    runtime = runtime or build_teaching_runtime()
    problem = await _problem(session, slug)
    if problem is None:
        return TeachingResult(
            ok=False,
            text="",
            kind=TeachingKind.HINT,
            reason=f"Unknown problem: {slug}",
        )

    seen = await highest_hint_level(session, user, problem.id)
    level = next_level(seen, requested_level)

    outcome = await runtime.hint(
        HintRequest(
            problem_slug=problem.slug,
            problem_title=problem.title,
            patterns=await _pattern_slugs(session, problem.id),
            level=level,
            already_seen=seen,
        )
    )

    if outcome.text is None:
        return TeachingResult(
            ok=False,
            text="",
            kind=TeachingKind.HINT,
            hint_level=int(level),
            runtime=runtime.name,
            reason=outcome.error_detail or "The coach could not produce a hint.",
        )

    # The model is not trusted to keep its own rule about code.
    checked = check_hint(level, outcome.text)
    if not checked.ok:
        return TeachingResult(
            ok=False,
            text="",
            kind=TeachingKind.HINT,
            hint_level=int(level),
            runtime=runtime.name,
            reason=checked.reason,
        )

    session.add(
        TeachingExchange(
            user_id=user.id,
            problem_id=problem.id,
            kind=TeachingKind.HINT,
            hint_level=int(level),
            content=checked.text,
        )
    )
    await session.flush()

    return TeachingResult(
        ok=True,
        text=checked.text,
        kind=TeachingKind.HINT,
        hint_level=int(level),
        level_description=describe(level),
        runtime=runtime.name,
    )


async def _attempt(session: AsyncSession, user: User, attempt_id: UUID) -> Attempt | None:
    return (
        await session.execute(
            select(Attempt)
            .where(Attempt.id == attempt_id, Attempt.user_id == user.id)
            .options(selectinload(Attempt.problem))
        )
    ).scalar_one_or_none()


async def _prior_failures(
    session: AsyncSession, user: User, attempt: Attempt, limit: int = 10
) -> tuple[PriorFailure, ...]:
    """Earlier attempts, so a repeating failure mode can be named.

    Recognising your *recurring* mistake is what moves the needle; a one-off bug
    rarely is.
    """
    rows = (
        (
            await session.execute(
                select(Attempt)
                .where(
                    Attempt.user_id == user.id,
                    Attempt.id != attempt.id,
                    Attempt.submitted_at <= attempt.submitted_at,
                )
                .options(selectinload(Attempt.problem))
                .order_by(Attempt.submitted_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return tuple(
        PriorFailure(
            attempt_id=row.id,
            problem_slug=row.problem.slug,
            resolution=row.resolution.value,
            blocker=row.blocker.value if row.blocker else None,
        )
        for row in rows
    )


async def _code_for(
    session: AsyncSession, user: User, attempt: Attempt, inline_code: str | None
) -> tuple[str | None, ComplexityEstimate | None, bool]:
    """The code to reason from, if consent allows it.

    Returns (code, complexity, degraded). `degraded` means the answer is being
    produced without code — which is a supported outcome, not a failure.
    """
    current = await consent_service.state(session, user)

    if inline_code and current.may_send:
        # Sent for this request. Stored only under a standing `always`.
        await consent_service.store_code(session, user, attempt, inline_code, attempt.language)
        return inline_code, estimate(inline_code), False

    stored = await consent_service.get_code(session, user, attempt.id)
    if stored is not None:
        return stored.code, estimate(stored.code), False

    return None, None, True


async def diagnose(
    session: AsyncSession,
    user: User,
    attempt_id: UUID,
    code: str | None = None,
    runtime: TeachingRuntime | None = None,
) -> TeachingResult:
    """Explain what went wrong — from the code where permitted, from history otherwise."""
    runtime = runtime or build_teaching_runtime()
    attempt = await _attempt(session, user, attempt_id)
    if attempt is None:
        return TeachingResult(
            ok=False, text="", kind=TeachingKind.DIAGNOSIS, reason="Unknown attempt."
        )

    resolved_code, complexity, degraded = await _code_for(session, user, attempt, code)
    priors = await _prior_failures(session, user, attempt)

    outcome = await runtime.diagnose(
        DiagnosisRequest(
            problem_slug=attempt.problem.slug,
            problem_title=attempt.problem.title,
            patterns=await _pattern_slugs(session, attempt.problem_id),
            resolution=attempt.resolution.value,
            blocker=attempt.blocker.value if attempt.blocker else None,
            code=resolved_code,
            language=attempt.language,
            complexity=complexity,
            prior_failures=priors,
        )
    )

    if outcome.text is None:
        return TeachingResult(
            ok=False,
            text="",
            kind=TeachingKind.DIAGNOSIS,
            runtime=runtime.name,
            degraded=degraded,
            reason=outcome.error_detail or "The coach could not produce a diagnosis.",
        )

    # Only attempts we actually supplied may be cited. Anything else would be
    # the model inventing history (invariant 5).
    permitted = {p.attempt_id for p in priors}
    cited = tuple(i for i in outcome.cited_attempt_ids if i in permitted)

    session.add(
        TeachingExchange(
            user_id=user.id,
            problem_id=attempt.problem_id,
            attempt_id=attempt.id,
            kind=TeachingKind.DIAGNOSIS,
            content=outcome.text,
            degraded=degraded,
        )
    )
    await session.flush()

    return TeachingResult(
        ok=True,
        text=outcome.text,
        kind=TeachingKind.DIAGNOSIS,
        degraded=degraded,
        runtime=runtime.name,
        cited_attempt_ids=cited,
    )


async def review(
    session: AsyncSession,
    user: User,
    attempt_id: UUID,
    code: str | None = None,
    runtime: TeachingRuntime | None = None,
) -> TeachingResult:
    """Review a solution that already passed. Accepted is not the same bar."""
    runtime = runtime or build_teaching_runtime()
    attempt = await _attempt(session, user, attempt_id)
    if attempt is None:
        return TeachingResult(
            ok=False, text="", kind=TeachingKind.REVIEW, reason="Unknown attempt."
        )

    resolved_code, complexity, degraded = await _code_for(session, user, attempt, code)

    outcome = await runtime.review(
        ReviewRequest(
            problem_slug=attempt.problem.slug,
            problem_title=attempt.problem.title,
            patterns=await _pattern_slugs(session, attempt.problem_id),
            code=resolved_code,
            language=attempt.language,
            complexity=complexity,
            active_seconds=attempt.active_seconds,
        )
    )

    if outcome.text is None:
        return TeachingResult(
            ok=False,
            text="",
            kind=TeachingKind.REVIEW,
            runtime=runtime.name,
            degraded=degraded,
            reason=outcome.error_detail or "The coach could not produce a review.",
        )

    session.add(
        TeachingExchange(
            user_id=user.id,
            problem_id=attempt.problem_id,
            attempt_id=attempt.id,
            kind=TeachingKind.REVIEW,
            content=outcome.text,
            degraded=degraded or outcome.degraded,
        )
    )
    await session.flush()

    return TeachingResult(
        ok=True,
        text=outcome.text,
        kind=TeachingKind.REVIEW,
        degraded=degraded or outcome.degraded,
        runtime=runtime.name,
    )


async def mock_turn(
    session: AsyncSession,
    user: User,
    slug: str,
    message: str,
    runtime: TeachingRuntime | None = None,
) -> TeachingResult:
    """One exchange in a mock interview."""
    runtime = runtime or build_teaching_runtime()
    problem = await _problem(session, slug)
    if problem is None:
        return TeachingResult(
            ok=False, text="", kind=TeachingKind.MOCK, reason=f"Unknown problem: {slug}"
        )

    history = (
        (
            await session.execute(
                select(TeachingExchange)
                .where(
                    TeachingExchange.user_id == user.id,
                    TeachingExchange.problem_id == problem.id,
                    TeachingExchange.kind == TeachingKind.MOCK,
                )
                .order_by(TeachingExchange.created_at.asc())
            )
        )
        .scalars()
        .all()
    )

    outcome = await runtime.mock_turn(
        MockTurnRequest(
            problem_slug=problem.slug,
            problem_title=problem.title,
            patterns=await _pattern_slugs(session, problem.id),
            transcript=tuple(("interviewer", h.content) for h in history),
            candidate_message=message,
        )
    )

    if outcome.text is None or outcome.failure is CoachFailure.UNAVAILABLE:
        return TeachingResult(
            ok=False,
            text=outcome.text or "",
            kind=TeachingKind.MOCK,
            runtime=runtime.name,
            reason=outcome.error_detail or "A mock interview needs the coach.",
        )

    session.add(
        TeachingExchange(
            user_id=user.id,
            problem_id=problem.id,
            kind=TeachingKind.MOCK,
            content=outcome.text,
        )
    )
    await session.flush()

    return TeachingResult(ok=True, text=outcome.text, kind=TeachingKind.MOCK, runtime=runtime.name)


async def history(session: AsyncSession, user: User, limit: int = 50) -> list[TeachingExchange]:
    return list(
        (
            await session.execute(
                select(TeachingExchange)
                .where(TeachingExchange.user_id == user.id)
                .order_by(TeachingExchange.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


def now() -> datetime:
    return datetime.now(UTC)
