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
from dsa_coach.mechanism.placement import PlacementProgress, select_placement_candidates
from dsa_coach.mechanism.prerequisites import PrerequisiteEdge, evaluate_unlocks
from dsa_coach.mechanism.prescription import Prescription
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
from dsa_coach.services import placement as placement_service
from dsa_coach.services import readiness as readiness_service
from dsa_coach.services import retention as retention_service


@dataclass(frozen=True)
class BlockPlan:
    plan: Plan
    block: Block
    focus_pattern_slugs: tuple[str, ...]
    locked_pattern_slugs: tuple[str, ...]
    #: Set while placement is still running (spec §9). The block is chosen for
    #: information rather than for targeting a known weakness.
    placement: PlacementProgress | None = None


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
    session: AsyncSession,
    user: User,
    *,
    now: datetime | None = None,
    seed: int = 0,
    prescription: Prescription | None = None,
) -> BlockPlan:
    """Assemble and persist the next block as a new active plan version.

    A `prescription` narrows the focus patterns, rating band, size and mix — but
    it has already been validated and clamped by the mechanism layer, and it
    still cannot name a single problem. Passing None runs the scheduler entirely
    on its own, which is what happens whenever the coach is unavailable.
    """
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
    # Provisionally unlocked patterns are schedulable but never a focus: their
    # prerequisites are unproven, so targeting them would be aiming at a guess.
    established = {pid for pid in unlocked if not unlocks[pid].provisional}
    focus = (
        [p.pattern_id for p in prescription.focus_patterns]
        if prescription is not None and prescription.focus_patterns
        else _focus_patterns(predictions, established)
    )

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
            minutes=estimate_minutes(problem.rating, score),
            predicted_score=score,
            pattern_ids=tuple(tags),
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

    size = prescription.size if prescription else _block_size(goal.minutes_per_day)
    mix = BlockMix(*prescription.mix) if prescription is not None else BlockMix()
    band = prescription.rating_band if prescription else None
    placement = await placement_service.progress(session, user)

    if placement.complete:
        if band is not None:
            low, high = band
            weakness = [c for c in weakness if low <= c.rating <= high]
            interleaved = [c for c in interleaved if low <= c.rating <= high]
        block = assemble_block(
            weakness=weakness,
            interleaved=interleaved,
            retention=retention,
            budget_minutes=goal.minutes_per_day,
            size=size,
            mix=mix,
            seed=seed,
        )
    else:
        # Still placing: there is no established weakness to target, so the block
        # is chosen for information instead — breadth across the foundations,
        # aimed where the outcome is least certain (spec §9).
        #
        # Due re-solves still come first. Retention does not pause for placement.
        observed = await placement_service.observed_pattern_ids(session, user)
        chosen = select_placement_candidates(
            weakness + interleaved,
            observed_patterns=observed,
            size=size,
            target_rating=tuning.PLACEMENT_START_RATING[goal.self_assessed_level],
        )
        block = assemble_block(
            weakness=[],
            interleaved=chosen,
            retention=retention,
            budget_minutes=goal.minutes_per_day,
            size=size,
            mix=BlockMix(),
            seed=seed,
        )

    plan = await _persist(
        session, user, goal, block, focus, slugs, predictions, placement, prescription
    )
    locked = tuple(sorted(slugs[pid] for pid, state in unlocks.items() if not state.unlocked))
    return BlockPlan(
        plan=plan,
        block=block,
        focus_pattern_slugs=tuple(slugs[p] for p in focus),
        locked_pattern_slugs=locked,
        placement=None if placement.complete else placement,
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
    placement: PlacementProgress,
    prescription: Prescription | None = None,
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
        # A plan built during placement is still provisional: it is chosen for
        # information, not from an established picture of where you stand.
        status=PlanStatus.PROVISIONAL if not placement.complete else PlanStatus.ACTIVE,
        summary=_summary(focus_names, block, placement),
        generation_context={
            "generator": _generator_name(placement, prescription),
            "generator_version": "phase3",
            "readiness_model": PRIMARY_MODEL,
            "focus_patterns": focus_names,
            # `None` where a focus pattern has no evidence yet. A prescribed
            # focus need not be one the user has already attempted — a pattern
            # with no history is a legitimate target, and often the most
            # informative one — so it simply has no readiness estimate. Writing
            # 0.0 here would record a fact nobody observed (invariant 5).
            "focus_readiness": {
                slugs[p]: (round(predictions[p].score, 3) if p in predictions else None)
                for p in focus
            },
            "mix": {
                "weakness": tuning.BLOCK_MIX_WEAKNESS,
                "interleaved": tuning.BLOCK_MIX_INTERLEAVED,
                "retention": tuning.BLOCK_MIX_RETENTION,
            },
            "placement": {
                "complete": placement.complete,
                "attempts": placement.attempts,
                "max_attempts": placement.max_attempts,
                "calibrated": placement.calibrated,
                "target": placement.target,
                "reason": placement.reason,
            },
            "budget_minutes": block.budget_minutes,
            "shortfalls": list(block.shortfalls),
            "prescribed": prescription is not None,
            "diagnosis": prescription.diagnosis if prescription else None,
            "note": (
                "The coach prescribed the shape; deterministic code selected the "
                "problems (invariant 3)."
                if prescription is not None
                else "No AI involved. Deterministic given the same evidence and seed."
            ),
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


def _generator_name(placement: PlacementProgress, prescription: Prescription | None) -> str:
    if not placement.complete:
        return "placement_block"
    return "coach_prescribed_block" if prescription else "deterministic_block_assembler"


def _summary(focus: list[str], block: Block, placement: PlacementProgress) -> str:
    if not block.items:
        return (
            "No block could be assembled — every candidate is either inside its "
            "cooldown window or behind a prerequisite that has not been demonstrated."
        )

    if not placement.complete:
        return (
            f"{len(block.items)} problems, {block.total_minutes} minutes. Still "
            f"working out where you stand ({placement.attempts} of up to "
            f"{placement.max_attempts} problems), so these are chosen to tell us "
            "the most rather than to target a weakness."
        )

    focus_text = ", ".join(focus) if focus else "a broad mix"
    return (
        f"{len(block.items)} problems, {block.total_minutes} minutes, focused on "
        f"{focus_text}. Assembled deterministically from your recorded attempts."
    )
