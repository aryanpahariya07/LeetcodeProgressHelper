"""Readiness service — the database glue around the pure models.

Both models run on every attempt and log a prediction each, so the Phase 6
bake-off (spec §6.3) has real data to decide on. Only the primary model's
estimates reach the scheduler.

Each model persists its own state, because their shapes genuinely differ: the
Beta baseline keeps a posterior per rating bucket, Glicko-2 keeps a single
rating/RD/volatility triple. Deleting the losing model in Phase 6 means deleting
its model file, its repository and its table together.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from dsa_coach.mechanism.evidence import (
    AttemptFacts,
    Evidence,
    ProblemRef,
    split_across_patterns,
    to_evidence,
)
from dsa_coach.mechanism.readiness import MODELS, PRIMARY_MODEL, Prediction, bucket_index
from dsa_coach.mechanism.readiness.baseline import BaselineState, Bucket
from dsa_coach.mechanism.readiness.glicko import GlickoState
from dsa_coach.models import (
    Attempt,
    Level,
    Pattern,
    PatternBaselineBucket,
    PatternRating,
    Problem,
    ProblemPattern,
    ReadinessPrediction,
    User,
    UserGoal,
)


class StateRepository(Protocol):
    """Loads and stores one model's per-pattern state."""

    async def load_all(self, session: AsyncSession, user_id: UUID) -> dict[UUID, Any]: ...

    async def save(
        self,
        session: AsyncSession,
        user_id: UUID,
        pattern_id: UUID,
        state: Any,
        evidence: Evidence,
    ) -> None: ...

    async def clear(self, session: AsyncSession, user_id: UUID) -> None:
        """Drop every stored state for this user.

        Only `recompute` uses this. Both models are incremental — a Beta
        posterior and a Glicko rating are folded forward one attempt at a time —
        so a corrected attempt cannot be edited in place. The state is dropped
        and rebuilt by replaying the evidence (spec §3.3).
        """
        ...


class BaselineRepository:
    """Beta posteriors, one row per (pattern, rating bucket)."""

    async def clear(self, session: AsyncSession, user_id: UUID) -> None:
        await session.execute(
            delete(PatternBaselineBucket).where(PatternBaselineBucket.user_id == user_id)
        )

    async def load_all(self, session: AsyncSession, user_id: UUID) -> dict[UUID, Any]:
        rows = (
            (
                await session.execute(
                    select(PatternBaselineBucket).where(PatternBaselineBucket.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )
        grouped: dict[UUID, dict[int, Bucket]] = {}
        for row in rows:
            grouped.setdefault(row.pattern_id, {})[row.bucket] = Bucket(row.alpha, row.beta)

        states: dict[UUID, Any] = {}
        for pattern_id, buckets in grouped.items():
            if len(buckets) != 3:
                continue
            states[pattern_id] = BaselineState(
                buckets=(buckets[0], buckets[1], buckets[2]),
                prior_mean=buckets[0].mean,
            )
        return states

    async def save(
        self,
        session: AsyncSession,
        user_id: UUID,
        pattern_id: UUID,
        state: Any,
        evidence: Evidence,
    ) -> None:
        practiced_at = evidence.at
        observed_bucket = bucket_index(evidence.problem.rating)
        existing = {
            row.bucket: row
            for row in (
                (
                    await session.execute(
                        select(PatternBaselineBucket).where(
                            PatternBaselineBucket.user_id == user_id,
                            PatternBaselineBucket.pattern_id == pattern_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        }
        for index, bucket in enumerate(state.buckets):
            row = existing.get(index)
            if row is None:
                touched = index == observed_bucket
                session.add(
                    PatternBaselineBucket(
                        user_id=user_id,
                        pattern_id=pattern_id,
                        bucket=index,
                        alpha=bucket.alpha,
                        beta=bucket.beta,
                        evidence_count=1 if touched else 0,
                        last_practiced_at=practiced_at if touched else None,
                    )
                )
            else:
                # Only the bucket that actually received this observation counts.
                # Incrementing all three would report three times the real
                # evidence, which is exactly the kind of inflated number this
                # project is not allowed to show.
                changed = row.alpha != bucket.alpha or row.beta != bucket.beta
                row.alpha = bucket.alpha
                row.beta = bucket.beta
                if changed:
                    row.evidence_count += 1
                    row.last_practiced_at = practiced_at


class GlickoRepository:
    """Glicko-2 state, one row per pattern."""

    async def clear(self, session: AsyncSession, user_id: UUID) -> None:
        await session.execute(delete(PatternRating).where(PatternRating.user_id == user_id))

    async def load_all(self, session: AsyncSession, user_id: UUID) -> dict[UUID, Any]:
        rows = (
            (await session.execute(select(PatternRating).where(PatternRating.user_id == user_id)))
            .scalars()
            .all()
        )
        return {row.pattern_id: GlickoState(row.rating, row.rd, row.volatility) for row in rows}

    async def save(
        self,
        session: AsyncSession,
        user_id: UUID,
        pattern_id: UUID,
        state: Any,
        evidence: Evidence,
    ) -> None:
        practiced_at = evidence.at
        row = (
            await session.execute(
                select(PatternRating).where(
                    PatternRating.user_id == user_id, PatternRating.pattern_id == pattern_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            session.add(
                PatternRating(
                    user_id=user_id,
                    pattern_id=pattern_id,
                    rating=state.rating,
                    rd=state.rd,
                    volatility=state.volatility,
                    evidence_count=1,
                    last_practiced_at=practiced_at,
                )
            )
        else:
            row.rating = state.rating
            row.rd = state.rd
            row.volatility = state.volatility
            row.evidence_count += 1
            row.last_practiced_at = practiced_at


REPOSITORIES: dict[str, StateRepository] = {
    "baseline-beta-v1": BaselineRepository(),
    "glicko2-v1": GlickoRepository(),
}


@dataclass(frozen=True)
class PatternReadiness:
    pattern_id: UUID
    slug: str
    name: str
    prediction: Prediction
    evidence_count: int


async def _level(session: AsyncSession, user: User) -> Level:
    goal = (
        await session.execute(
            select(UserGoal).where(UserGoal.user_id == user.id, UserGoal.active.is_(True))
        )
    ).scalar_one_or_none()
    return goal.self_assessed_level if goal else Level.BEGINNER


async def reference_ratings(session: AsyncSession) -> dict[UUID, ProblemRef]:
    """Median rating of each pattern's active problems.

    Readiness is only meaningful relative to a difficulty, so every summary is
    expressed against the pattern's own typical problem.
    """
    rows = (
        await session.execute(
            select(ProblemPattern.pattern_id, Problem.rating, Problem.rating_rd)
            .join(Problem, Problem.id == ProblemPattern.problem_id)
            .where(Problem.is_active.is_(True))
        )
    ).all()

    grouped: dict[UUID, list[tuple[int, int]]] = {}
    for pattern_id, rating, rd in rows:
        grouped.setdefault(pattern_id, []).append((rating, rd))

    return {
        pattern_id: ProblemRef(
            rating=int(statistics.median(r for r, _ in values)),
            rating_rd=int(statistics.median(d for _, d in values)),
        )
        for pattern_id, values in grouped.items()
    }


async def _problem_patterns(session: AsyncSession, problem_id: UUID) -> dict[UUID, float]:
    rows = (
        await session.execute(
            select(ProblemPattern.pattern_id, ProblemPattern.weight).where(
                ProblemPattern.problem_id == problem_id
            )
        )
    ).all()
    return dict(rows)  # type: ignore[arg-type]


async def _prior_attempt_times(
    session: AsyncSession, user_id: UUID, problem_id: UUID, before: datetime
) -> tuple[datetime, ...]:
    rows = (
        (
            await session.execute(
                select(Attempt.submitted_at).where(
                    Attempt.user_id == user_id,
                    Attempt.problem_id == problem_id,
                    Attempt.submitted_at < before,
                )
            )
        )
        .scalars()
        .all()
    )
    return tuple(rows)


async def build_facts(session: AsyncSession, user: User, attempt: Attempt) -> AttemptFacts:
    problem = (
        await session.execute(select(Problem).where(Problem.id == attempt.problem_id))
    ).scalar_one()
    return AttemptFacts(
        attempt_id=attempt.id,
        problem_id=attempt.problem_id,
        problem=ProblemRef(rating=problem.rating, rating_rd=problem.rating_rd),
        resolution=attempt.resolution,
        submission_outcome=attempt.submission_outcome,
        capture_confidence=attempt.capture_confidence,
        is_resolve=attempt.is_resolve,
        submitted_at=attempt.submitted_at,
        prior_attempt_times=await _prior_attempt_times(
            session, user.id, attempt.problem_id, attempt.submitted_at
        ),
    )


async def apply_attempt(session: AsyncSession, user: User, attempt: Attempt) -> Evidence | None:
    """Fold one attempt into every readiness model.

    Logs a prediction per model *before* applying the evidence, so calibration
    compares a genuine forecast against the outcome rather than a hindsight fit.
    Returns the evidence used, or None if the attempt did not count.
    """
    facts = await build_facts(session, user, attempt)
    evidence = to_evidence(facts)
    if evidence is None:
        return None

    pattern_weights = await _problem_patterns(session, attempt.problem_id)
    if not pattern_weights:
        return None

    level = await _level(session, user)
    per_pattern = split_across_patterns(evidence, pattern_weights)

    for version, model in MODELS.items():
        repository = REPOSITORIES[version]
        states = await repository.load_all(session, user.id)

        # Prediction before the update, weighted across the problem's patterns.
        predicted = _weighted_prediction(model, states, level, pattern_weights, facts.problem)
        session.add(
            ReadinessPrediction(
                user_id=user.id,
                problem_id=attempt.problem_id,
                attempt_id=attempt.id,
                model_version=version,
                predicted_score=predicted.score,
                uncertainty=predicted.uncertainty,
                actual_outcome=evidence.score,
                resolved_at=attempt.submitted_at,
            )
        )

        for pattern_id, pattern_evidence in per_pattern.items():
            state = states.get(pattern_id) or model.blank(level)
            state = model.update(state, pattern_evidence)
            await repository.save(session, user.id, pattern_id, state, pattern_evidence)

    await session.flush()
    return evidence


@dataclass(frozen=True)
class RecomputeResult:
    attempts_replayed: int
    attempts_counted: int


async def recompute(session: AsyncSession, user: User) -> RecomputeResult:
    """Rebuild every readiness model from the user's attempts (spec §3.3).

    Amending an attempt changes evidence that has already been folded in, and
    both models are incremental — a Beta posterior and a Glicko rating are
    folded forward one attempt at a time — so there is no way to edit a single
    past observation in place. The only correct answer is to drop the derived
    state and replay.

    Everything dropped here is *derived*: posteriors, ratings, and the
    prediction log. No attempt, no plan and no piece of evidence is touched.
    That is what makes this safe to run whenever a correction lands.

    Predictions are re-logged as the replay proceeds, which keeps the
    calibration data (spec §6.3) consistent with the corrected history rather
    than mixing forecasts made against evidence that has since changed.
    """
    for repository in REPOSITORIES.values():
        await repository.clear(session, user.id)
    await session.execute(delete(ReadinessPrediction).where(ReadinessPrediction.user_id == user.id))
    # The clears must land before the replay re-reads state, or `load_all` would
    # hand back rows this transaction has already deleted.
    await session.flush()

    attempts = (
        (
            await session.execute(
                select(Attempt)
                .where(Attempt.user_id == user.id)
                # Chronological, because each update depends on the state left by
                # the one before it. Replaying out of order silently produces a
                # different answer rather than an error.
                .order_by(Attempt.submitted_at, Attempt.id)
            )
        )
        .scalars()
        .all()
    )

    counted = 0
    for attempt in attempts:
        if await apply_attempt(session, user, attempt) is not None:
            counted += 1

    return RecomputeResult(attempts_replayed=len(attempts), attempts_counted=counted)


def _weighted_prediction(
    model: Any,
    states: dict[UUID, Any],
    level: Level,
    pattern_weights: dict[UUID, float],
    problem: ProblemRef,
) -> Prediction:
    """Combine per-pattern predictions into one for a multi-pattern problem."""
    total = sum(pattern_weights.values()) or 1.0
    score = 0.0
    uncertainty = 0.0
    for pattern_id, weight in pattern_weights.items():
        state = states.get(pattern_id) or model.blank(level)
        prediction = model.predict(state, problem)
        score += prediction.score * weight
        uncertainty += prediction.uncertainty * weight
    return Prediction(score=score / total, uncertainty=uncertainty / total)


async def pattern_predictions(
    session: AsyncSession, user: User, version: str = PRIMARY_MODEL
) -> dict[UUID, Prediction]:
    """Current readiness per pattern, from the given model."""
    model = MODELS[version]
    states = await REPOSITORIES[version].load_all(session, user.id)
    references = await reference_ratings(session)

    return {
        pattern_id: model.summary(state, references[pattern_id])
        for pattern_id, state in states.items()
        if pattern_id in references
    }


async def readiness_report(
    session: AsyncSession, user: User, version: str = PRIMARY_MODEL
) -> list[PatternReadiness]:
    """Readiness for every catalogue pattern, including untouched ones."""
    model = MODELS[version]
    states = await REPOSITORIES[version].load_all(session, user.id)
    references = await reference_ratings(session)
    level = await _level(session, user)
    counts = await _evidence_counts(session, user.id, version)

    patterns: Sequence[Pattern] = (
        (await session.execute(select(Pattern).order_by(Pattern.slug))).scalars().all()
    )

    report = []
    for pattern in patterns:
        reference = references.get(pattern.id)
        if reference is None:
            continue
        state = states.get(pattern.id) or model.blank(level)
        report.append(
            PatternReadiness(
                pattern_id=pattern.id,
                slug=pattern.slug,
                name=pattern.name,
                prediction=model.summary(state, reference),
                evidence_count=counts.get(pattern.id, 0),
            )
        )
    return report


async def _evidence_counts(session: AsyncSession, user_id: UUID, version: str) -> dict[UUID, int]:
    if version == "glicko2-v1":
        rows = (
            await session.execute(
                select(PatternRating.pattern_id, PatternRating.evidence_count).where(
                    PatternRating.user_id == user_id
                )
            )
        ).all()
        return dict(rows)  # type: ignore[arg-type]

    rows = (
        await session.execute(
            select(PatternBaselineBucket.pattern_id, PatternBaselineBucket.evidence_count).where(
                PatternBaselineBucket.user_id == user_id
            )
        )
    ).all()
    totals: dict[UUID, int] = {}
    for pattern_id, count in rows:
        totals[pattern_id] = totals.get(pattern_id, 0) + count
    return totals
