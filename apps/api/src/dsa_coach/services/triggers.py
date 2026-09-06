"""Three-attempt trigger with material-change gating (spec §6.6).

Runs after every three *relevant* attempts. Most of the time three attempts do
not justify changing anything, and the honest result is a recorded, explained
`no_change` rather than silence.

Phase 1 has no agent. A material change is recorded as `pending_agent` so Phase 4
can act on it without re-deriving history — and so the deterministic scheduler
can still rebuild the block on its own in the meantime.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dsa_coach import tuning
from dsa_coach.mechanism.prerequisites import PrerequisiteEdge, evaluate_unlocks
from dsa_coach.mechanism.readiness.base import Prediction
from dsa_coach.mechanism.triggers import (
    BlockerObservation,
    MaterialChange,
    RelevanceFacts,
    detect_material_change,
    is_relevant,
)
from dsa_coach.models import (
    Attempt,
    Pattern,
    PatternPrerequisite,
    ProblemPattern,
    TriggerBatch,
    TriggerOutcome,
    User,
)
from dsa_coach.services import readiness as readiness_service


@dataclass(frozen=True)
class Snapshot:
    readiness: dict[str, float]
    uncertainty: dict[str, float]
    unlocked: list[str]

    def to_json(self) -> dict[str, object]:
        return {
            "readiness": self.readiness,
            "uncertainty": self.uncertainty,
            "unlocked": self.unlocked,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any] | None) -> Snapshot | None:
        if not raw:
            return None
        return cls(
            readiness={str(k): float(v) for k, v in raw.get("readiness", {}).items()},
            uncertainty={str(k): float(v) for k, v in raw.get("uncertainty", {}).items()},
            unlocked=[str(item) for item in raw.get("unlocked", [])],
        )


def mark_relevance(attempt: Attempt, *, resolve_lapsed: bool, already_counted: bool) -> bool:
    """Decide once, at ingestion, whether this attempt advances the counter."""
    relevant = is_relevant(
        RelevanceFacts(
            is_resolve=attempt.is_resolve,
            resolution=attempt.resolution,
            resolve_lapsed=resolve_lapsed,
            already_counted_this_sitting=already_counted,
        )
    )
    attempt.counts_toward_trigger = relevant
    return relevant


async def _last_batch(session: AsyncSession, user: User) -> TriggerBatch | None:
    return (
        await session.execute(
            select(TriggerBatch)
            .where(TriggerBatch.user_id == user.id)
            .order_by(TriggerBatch.evaluated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _pending_attempts(session: AsyncSession, user: User) -> list[Attempt]:
    """Relevant attempts since the last evaluation.

    Bounded by the last batch's timestamp, so a retry cannot double-count.
    """
    last = await _last_batch(session, user)
    query = select(Attempt).where(
        Attempt.user_id == user.id, Attempt.counts_toward_trigger.is_(True)
    )
    if last is not None:
        query = query.where(Attempt.created_at > last.evaluated_at)
    return list((await session.execute(query.order_by(Attempt.created_at.asc()))).scalars().all())


async def _snapshot(session: AsyncSession, user: User) -> tuple[Snapshot, dict[UUID, Prediction]]:
    predictions = await readiness_service.pattern_predictions(session, user)
    patterns = (await session.execute(select(Pattern))).scalars().all()
    slugs = {p.id: p.slug for p in patterns}

    edges = [
        PrerequisiteEdge(r.pattern_id, r.requires_pattern_id, r.strength)
        for r in (await session.execute(select(PatternPrerequisite))).scalars().all()
    ]
    unlocks = evaluate_unlocks({p.id for p in patterns}, edges, predictions)

    snapshot = Snapshot(
        readiness={slugs[pid]: round(p.score, 4) for pid, p in predictions.items()},
        uncertainty={slugs[pid]: round(p.uncertainty, 4) for pid, p in predictions.items()},
        unlocked=sorted(slugs[pid] for pid, s in unlocks.items() if s.unlocked),
    )
    return snapshot, predictions


async def _blocker_observations(
    session: AsyncSession, attempts: list[Attempt]
) -> list[BlockerObservation]:
    """Blockers from the batch, attributed to each attempt's dominant pattern."""
    observations: list[BlockerObservation] = []
    for attempt in attempts:
        if attempt.blocker is None:
            continue
        rows = (
            await session.execute(
                select(ProblemPattern.pattern_id, ProblemPattern.weight)
                .where(ProblemPattern.problem_id == attempt.problem_id)
                .order_by(ProblemPattern.weight.desc())
                .limit(1)
            )
        ).first()
        if rows is None:
            continue
        observations.append(
            BlockerObservation(
                pattern_id=rows[0], blocker=attempt.blocker, resolution=attempt.resolution
            )
        )
    return observations


async def evaluate_if_due(
    session: AsyncSession, user: User, *, now: datetime | None = None
) -> TriggerBatch | None:
    """Evaluate the plan if three relevant attempts have accumulated."""
    now = now or datetime.now(UTC)
    pending = await _pending_attempts(session, user)
    if len(pending) < tuning.TRIGGER_ATTEMPT_COUNT:
        return None

    batch = pending[: tuning.TRIGGER_ATTEMPT_COUNT]
    previous = await _last_batch(session, user)
    before = Snapshot.from_json(previous.readiness_snapshot if previous else None)
    after, predictions = await _snapshot(session, user)

    patterns = (await session.execute(select(Pattern))).scalars().all()
    by_slug = {p.slug: p.id for p in patterns}
    names = {p.id: p.slug for p in patterns}

    before_predictions: dict[UUID, Prediction] = {}
    if before is not None:
        for slug, score in before.readiness.items():
            pattern_id = by_slug.get(slug)
            if pattern_id is not None:
                before_predictions[pattern_id] = Prediction(
                    score=score, uncertainty=before.uncertainty.get(slug, 1.0)
                )

    if before is None:
        # The first evaluation has nothing to compare against. Without this,
        # every root pattern reads as "newly unlocked" and the user is told a
        # dozen things changed on day one — noise dressed up as insight.
        change = MaterialChange(
            material=False,
            reasons=(),
            explanation=(
                "Reviewed after 3 attempts — no change. This is the first "
                "evaluation, so there is no earlier baseline to compare against."
            ),
        )
    else:
        change = detect_material_change(
            before=before_predictions,
            after=predictions,
            unlocked_before={by_slug[s] for s in before.unlocked if s in by_slug},
            unlocked_after={by_slug[s] for s in after.unlocked if s in by_slug},
            recent_blockers=await _blocker_observations(session, batch),
            pattern_names=names,
        )

    row = TriggerBatch(
        user_id=user.id,
        attempt_ids=[str(a.id) for a in batch],
        relevant_count=len(batch),
        material=change.material,
        reasons=list(change.reasons),
        outcome=TriggerOutcome.PENDING_AGENT if change.material else TriggerOutcome.NO_CHANGE,
        explanation=change.explanation,
        readiness_snapshot=after.to_json(),
        evaluated_at=now,
    )
    session.add(row)
    await session.flush()
    return row


async def recent_batches(session: AsyncSession, user: User, limit: int = 20) -> list[TriggerBatch]:
    return list(
        (
            await session.execute(
                select(TriggerBatch)
                .where(TriggerBatch.user_id == user.id)
                .order_by(TriggerBatch.evaluated_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
