"""Block assembly service — turns readiness into an actual plan (spec §6.7).

This is the deterministic scheduler. It produces a defensible next block with no
AI involved, which is what makes invariant 4 true: the product stays fully usable
when the coach is unavailable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from dsa_coach import tuning
from dsa_coach.mechanism.blocks import (
    Block,
    BlockMix,
    Candidate,
    assemble_block,
    estimate_minutes,
)
from dsa_coach.mechanism.evidence import ProblemRef
from dsa_coach.mechanism.prerequisites import PrerequisiteEdge, evaluate_unlocks
from dsa_coach.mechanism.readiness import MODELS, PRIMARY_MODEL, Prediction
from dsa_coach.models import (
    Attempt,
    ItemRole,
    ItemStatus,
    ItemType,
    Pattern,
    PatternPrerequisite,
    Plan,
    PlanItem,
    PlanStatus,
    Problem,
    ProblemPattern,
    User,
    UserGoal,
)
from dsa_coach.services import readiness as readiness_service
from dsa_coach.services import retention as retention_service


@dataclass(frozen=True)
class BlockPlan:
    plan: Plan
    block: Block
    focus_pattern_slugs: tuple[str, ...]
    locked_pattern_slugs: tuple[str, ...]


async def _prerequisite_edges(session: AsyncSession) -> list[PrerequisiteEdge]:
    rows = (await session.execute(select(PatternPrerequisite))).scalars().all()
    return [PrerequisiteEdge(r.pattern_id, r.requires_pattern_id, r.strength) for r in rows]


async def _recent_problem_ids(session: AsyncSession, user: User, now: datetime) -> set[UUID]:
    """Problems attempted inside the cooldown window."""
    cutoff = now - timedelta(days=tuning.COOLDOWN_DAYS)
    return set(
        (
            await session.execute(
                select(Attempt.problem_id).where(
                    Attempt.user_id == user.id, Attempt.submitted_at >= cutoff
                )
            )
        )
        .scalars()
        .all()
    )


async def _problem_pattern_map(session: AsyncSession) -> dict[UUID, dict[UUID, float]]:
    rows = (await session.execute(select(ProblemPattern))).scalars().all()
    mapping: dict[UUID, dict[UUID, float]] = {}
    for row in rows:
        mapping.setdefault(row.problem_id, {})[row.pattern_id] = row.weight
    return mapping


def _focus_patterns(
    predictions: dict[UUID, Prediction], unlocked: set[UUID], limit: int = 3
) -> list[UUID]:
    """The weakest unlocked patterns that have some evidence behind them.

    Patterns with no evidence at all are not "weak" — they are unknown, and are
    better served by the interleaved slot until something is known about them.
    """
    known = [p for p in unlocked if p in predictions]
    return sorted(known, key=lambda p: predictions[p].score)[:limit]


async def build_next_block(
    session: AsyncSession, user: User, *, now: datetime | None = None, seed: int = 0
) -> BlockPlan:
    """Assemble and persist the next block as a new active plan version."""
    now = now or datetime.now(UTC)
    model = MODELS[PRIMARY_MODEL]

    goal = (
        await session.execute(
            select(UserGoal).where(UserGoal.user_id == user.id, UserGoal.active.is_(True))
        )
    ).scalar_one()

    predictions = await readiness_service.pattern_predictions(session, user)
    patterns = (await session.execute(select(Pattern))).scalars().all()
    pattern_ids = {p.id for p in patterns}
    slugs = {p.id: p.slug for p in patterns}

    unlocks = evaluate_unlocks(pattern_ids, await _prerequisite_edges(session), predictions)
    unlocked = {pid for pid, state in unlocks.items() if state.unlocked}
    focus = _focus_patterns(predictions, unlocked)

    excluded = await _recent_problem_ids(session, user, now)
    due = await retention_service.due_problem_ids(session, user, now)
    problem_patterns = await _problem_pattern_map(session)

    problems = (
        (await session.execute(select(Problem).where(Problem.is_active.is_(True)))).scalars().all()
    )

    weakness: list[Candidate] = []
    interleaved: list[Candidate] = []
    retention: list[Candidate] = []

    states = await readiness_service.REPOSITORIES[PRIMARY_MODEL].load_all(session, user.id)

    for problem in problems:
        tags = problem_patterns.get(problem.id, {})
        if not tags:
            continue

        reference = ProblemRef(problem.rating, problem.rating_rd)
        score = _predicted(model, states, tags, reference, goal)
        candidate = Candidate(
            problem_id=problem.id,
            rating=problem.rating,
            minutes=estimate_minutes(score),
            predicted_score=score,
        )

        # Due re-solves bypass the cooldown: that is the entire point of them.
        if problem.id in due:
            retention.append(candidate)
            continue
        if problem.id in excluded:
            continue
        if not any(pid in unlocked for pid in tags):
            continue
        if any(pid in focus for pid in tags):
            weakness.append(candidate)
        else:
            interleaved.append(candidate)

    size = _block_size(goal.minutes_per_day)
    block = assemble_block(
        weakness=weakness,
        interleaved=interleaved,
        retention=retention,
        budget_minutes=goal.minutes_per_day,
        size=size,
        mix=BlockMix(),
        seed=seed,
    )

    plan = await _persist(session, user, goal, block, focus, slugs, predictions)
    locked = tuple(sorted(slugs[pid] for pid, state in unlocks.items() if not state.unlocked))
    return BlockPlan(
        plan=plan,
        block=block,
        focus_pattern_slugs=tuple(slugs[p] for p in focus),
        locked_pattern_slugs=locked,
    )


def _predicted(
    model: object,
    states: dict[UUID, object],
    tags: dict[UUID, float],
    reference: ProblemRef,
    goal: UserGoal,
) -> float:
    total = sum(tags.values()) or 1.0
    score = 0.0
    for pattern_id, weight in tags.items():
        state = states.get(pattern_id) or model.blank(goal.self_assessed_level)  # type: ignore[attr-defined]
        score += model.predict(state, reference).score * weight  # type: ignore[attr-defined]
    return score / total


def _block_size(minutes_per_day: int) -> int:
    """How many problems a day's budget can hold, at the base estimate."""
    usable = minutes_per_day * tuning.BLOCK_BUDGET_FILL
    return max(1, int(usable // tuning.BASE_PROBLEM_MINUTES) + 1)


async def _persist(
    session: AsyncSession,
    user: User,
    goal: UserGoal,
    block: Block,
    focus: list[UUID],
    slugs: dict[UUID, str],
    predictions: dict[UUID, Prediction],
) -> Plan:
    current = (
        (
            await session.execute(
                select(Plan)
                .where(
                    Plan.user_id == user.id,
                    Plan.status.in_([PlanStatus.PROVISIONAL, PlanStatus.ACTIVE]),
                )
                .order_by(Plan.version.desc())
            )
        )
        .scalars()
        .all()
    )

    for old in current:
        old.status = PlanStatus.SUPERSEDED

    version = (current[0].version + 1) if current else 1
    focus_names = [slugs[p] for p in focus]

    plan = Plan(
        user_id=user.id,
        goal_id=goal.id,
        version=version,
        status=PlanStatus.ACTIVE,
        summary=_summary(focus_names, block),
        generation_context={
            "generator": "deterministic_block_assembler",
            "generator_version": "phase1",
            "readiness_model": PRIMARY_MODEL,
            "focus_patterns": focus_names,
            "focus_readiness": {slugs[p]: round(predictions[p].score, 3) for p in focus},
            "mix": {
                "weakness": tuning.BLOCK_MIX_WEAKNESS,
                "interleaved": tuning.BLOCK_MIX_INTERLEAVED,
                "retention": tuning.BLOCK_MIX_RETENTION,
            },
            "budget_minutes": block.budget_minutes,
            "shortfalls": list(block.shortfalls),
            "note": "No AI involved. Deterministic given the same evidence and seed.",
        },
    )
    session.add(plan)
    await session.flush()

    block_id = uuid.uuid4()
    for sequence, item in enumerate(block.items):
        session.add(
            PlanItem(
                plan_id=plan.id,
                block_id=block_id,
                sequence=sequence,
                problem_id=item.candidate.problem_id,
                item_type=(
                    ItemType.RESOLVE if item.role is ItemRole.RETENTION else ItemType.PRACTICE
                ),
                role=item.role,
                target_minutes=item.candidate.minutes,
                status=ItemStatus.PENDING,
            )
        )

    await session.flush()
    return (
        await session.execute(
            select(Plan)
            .where(Plan.id == plan.id)
            .options(selectinload(Plan.items).selectinload(PlanItem.problem))
        )
    ).scalar_one()


def _summary(focus: list[str], block: Block) -> str:
    if not block.items:
        return (
            "No block could be assembled — every candidate is either inside its "
            "cooldown window or behind a prerequisite that has not been demonstrated."
        )
    focus_text = ", ".join(focus) if focus else "a broad mix"
    return (
        f"{len(block.items)} problems, {block.total_minutes} minutes, focused on "
        f"{focus_text}. Assembled deterministically from your recorded attempts."
    )
