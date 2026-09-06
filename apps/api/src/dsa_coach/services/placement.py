"""Placement service (spec §9).

Placement state is **derived, never stored**. It is a function of the attempts
already recorded and the readiness those attempts produced, so there is no flag
to get out of sync with the evidence — and re-running placement after deleting an
attempt gives the honest answer rather than a stale one.

A pattern only counts as a placement target if the catalogue actually has
problems for it. `complexity-basics` and `recursion-basics` are foundational
prerequisites with nothing tagged to them; requiring coverage of a pattern the
user cannot possibly attempt would mean placement never ends.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dsa_coach.mechanism.placement import PlacementProgress, evaluate_placement
from dsa_coach.mechanism.readiness.base import Prediction
from dsa_coach.models import Attempt, Pattern, ProblemPattern, User
from dsa_coach.services import readiness as readiness_service


async def foundational_pattern_ids(session: AsyncSession) -> list[UUID]:
    """Foundational patterns that have at least one active problem."""
    rows = (
        await session.execute(
            select(Pattern.id)
            .join(ProblemPattern, ProblemPattern.pattern_id == Pattern.id)
            .where(Pattern.foundational.is_(True))
            .group_by(Pattern.id)
            .order_by(Pattern.slug)
        )
    ).scalars()
    return list(rows)


async def first_exposure_count(session: AsyncSession, user: User) -> int:
    """Distinct problems seen for the first time.

    Counting distinct problems rather than attempts means grinding one problem
    does not advance placement — placement is asking about breadth.
    """
    return (
        await session.execute(
            select(func.count(func.distinct(Attempt.problem_id))).where(
                Attempt.user_id == user.id, Attempt.is_resolve.is_(False)
            )
        )
    ).scalar_one()


async def observed_pattern_ids(session: AsyncSession, user: User) -> set[UUID]:
    """Patterns the user has attempted at least one problem from."""
    rows = (
        await session.execute(
            select(ProblemPattern.pattern_id)
            .join(Attempt, Attempt.problem_id == ProblemPattern.problem_id)
            .where(Attempt.user_id == user.id, Attempt.is_resolve.is_(False))
            .distinct()
        )
    ).scalars()
    return set(rows)


async def progress(session: AsyncSession, user: User) -> PlacementProgress:
    """Where placement has got to."""
    foundational = await foundational_pattern_ids(session)
    predictions = await readiness_service.pattern_predictions(session, user)
    observed = await observed_pattern_ids(session, user)

    # A pattern with no evidence has no prediction, which is what marks it
    # uncovered. Present-but-uncalibrated is a different state, and the progress
    # evaluation distinguishes them.
    state: dict[UUID, Prediction | None] = {
        pattern_id: predictions.get(pattern_id) if pattern_id in observed else None
        for pattern_id in foundational
    }

    return evaluate_placement(
        attempts=await first_exposure_count(session, user),
        foundational=state,
    )


async def is_placing(session: AsyncSession, user: User) -> bool:
    return not (await progress(session, user)).complete
