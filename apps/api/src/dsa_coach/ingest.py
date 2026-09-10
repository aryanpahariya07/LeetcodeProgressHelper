"""Attempt ingestion.

The single write path for attempt evidence, shared by manual logging and the
extension batch endpoint.

Invariant 7 — all ingestion is idempotent, deduplicated by client-generated
`event_uuid`. Every inbound event lands in `attempt_events` first, including
invalid ones, so that replaying a batch is always a no-op regardless of whether
the original event produced an attempt.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from dsa_coach import tuning
from dsa_coach.models import (
    Attempt,
    AttemptEvent,
    AttemptSource,
    CaptureConfidence,
    Problem,
    ProcessingStatus,
    RatingSource,
    User,
)
from dsa_coach.schemas import AttemptEventIn, EventResultOut
from dsa_coach.services import pipeline


async def ingest_events(
    session: AsyncSession,
    user: User,
    events: list[AttemptEventIn],
    source: AttemptSource,
    *,
    device_id: uuid.UUID | None = None,
) -> list[EventResultOut]:
    """Ingest a batch, returning one result per event in the order supplied.

    A failure on one event never affects the others — each is processed inside
    its own savepoint (spec §14, partial success).
    """
    results: list[EventResultOut] = []
    for event in events:
        result = await _ingest_one(session, user, event, source, device_id)
        results.append(result)

        # The evidence is recorded above. Deriving readiness, retention and the
        # trigger happens afterwards and can never undo it.
        if result.status == "accepted" and result.attempt_id is not None:
            attempt = await session.get(Attempt, result.attempt_id)
            if attempt is not None:
                await pipeline.process_safely(session, user, attempt)

    return results


async def _ingest_one(
    session: AsyncSession,
    user: User,
    event: AttemptEventIn,
    source: AttemptSource,
    device_id: uuid.UUID | None,
) -> EventResultOut:
    existing = await _find_event(session, event.event_uuid)
    if existing is not None:
        return EventResultOut(
            event_uuid=event.event_uuid,
            status="duplicate",
            attempt_id=existing.attempt_id,
        )

    problem = await _resolve_problem(session, event.provider, event.problem_slug)
    if problem is None:
        # Not in the catalogue — which is the normal case, not an error. The
        # seeded catalogue holds a few dozen problems; LeetCode has thousands.
        #
        # This used to be recorded `invalid` and the evidence discarded. That
        # silently threw away most real practice: a rejected event is dropped
        # from the extension's queue and surfaced nowhere, so attempts simply
        # vanished with no signal anywhere in the product.
        #
        # The attempt happened, so it is a fact and is stored. The rating and
        # difficulty did not become known, so they stay null rather than being
        # guessed (invariant 5) — which is exactly what keeps this honest. An
        # unrated problem is inactive, so the scheduler never offers it, and the
        # readiness models skip it for want of a reference rating.
        #
        # Enriching the catalogue later makes these count retroactively:
        # `recompute` replays every attempt, so evidence recorded today starts
        # contributing the moment its problem gains a rating.
        problem = await _create_unrated_problem(session, event.provider, event.problem_slug)

    try:
        async with session.begin_nested():
            attempt = _build_attempt(user, problem.id, event, source)
            session.add(attempt)
            await session.flush()

            await _record_event(
                session,
                user=user,
                event=event,
                device_id=device_id,
                status=ProcessingStatus.PROCESSED,
                attempt_id=attempt.id,
                error=None,
            )
            await session.flush()
    except IntegrityError:
        # Lost a race on the unique event_uuid index. The winner's row is
        # authoritative; report this one as the duplicate it is.
        duplicate = await _find_event(session, event.event_uuid)
        return EventResultOut(
            event_uuid=event.event_uuid,
            status="duplicate",
            attempt_id=duplicate.attempt_id if duplicate else None,
        )

    return EventResultOut(event_uuid=event.event_uuid, status="accepted", attempt_id=attempt.id)


async def _find_event(session: AsyncSession, event_uuid: uuid.UUID) -> AttemptEvent | None:
    return (
        await session.execute(select(AttemptEvent).where(AttemptEvent.event_uuid == event_uuid))
    ).scalar_one_or_none()


async def _create_unrated_problem(session: AsyncSession, provider: str, slug: str) -> Problem:
    """A placeholder for a problem seen in the wild but absent from the catalogue.

    Everything recorded here is *observed*: the provider, the slug, and the URL
    those two imply. Nothing is inferred. `rating` and `difficulty` stay null
    because they were never observed, and `is_active` is false so the scheduler
    cannot offer a problem it knows nothing about.

    The title falls back to the slug rather than being left blank — it is the
    only human-readable name available, and it is not a claim about anything.
    """
    problem = Problem(
        provider=provider,
        external_id=slug,
        slug=slug,
        title=slug.replace("-", " ").title(),
        url=f"https://leetcode.com/problems/{slug}/",
        difficulty=None,
        rating=None,
        rating_rd=tuning.UNRATED_RATING_RD,
        rating_source=RatingSource.MANUAL,
        catalogue_source_id=None,
        is_active=False,
    )
    session.add(problem)
    await session.flush()
    return problem


async def _resolve_problem(session: AsyncSession, provider: str, slug: str) -> Problem | None:
    return (
        await session.execute(
            select(Problem).where(Problem.provider == provider, Problem.slug == slug)
        )
    ).scalar_one_or_none()


#: Ceiling on how confident a capture may claim to be, by source (invariant 6).
#:
#: A hand-typed recollection is not `high`-confidence evidence no matter what the
#: client says, and public-profile sync reveals only that a problem was accepted —
#: never whether it was solved independently. The server knows the source, so the
#: server caps the claim.
_MAX_CONFIDENCE: dict[AttemptSource, CaptureConfidence] = {
    AttemptSource.EXTENSION: CaptureConfidence.HIGH,
    AttemptSource.MANUAL: CaptureConfidence.MEDIUM,
    AttemptSource.PUBLIC_SYNC: CaptureConfidence.LOW,
}

_CONFIDENCE_ORDER: dict[CaptureConfidence, int] = {
    CaptureConfidence.LOW: 0,
    CaptureConfidence.MEDIUM: 1,
    CaptureConfidence.HIGH: 2,
}


def _capped_confidence(claimed: CaptureConfidence, source: AttemptSource) -> CaptureConfidence:
    ceiling = _MAX_CONFIDENCE[source]
    return min(claimed, ceiling, key=lambda c: _CONFIDENCE_ORDER[c])


def _build_attempt(
    user: User, problem_id: uuid.UUID, event: AttemptEventIn, source: AttemptSource
) -> Attempt:
    return Attempt(
        user_id=user.id,
        problem_id=problem_id,
        resolution=event.resolution,
        blocker=event.blocker,
        confidence_cold_redo=event.confidence_cold_redo,
        hint_level_used=event.hint_level_used,
        language=event.language,
        started_at=event.started_at,
        submitted_at=event.submitted_at,
        active_seconds=event.active_seconds,
        excluded_seconds=event.excluded_seconds,
        run_count=event.run_count,
        submit_count=event.submit_count,
        submission_outcome=event.submission_outcome,
        is_resolve=event.is_resolve,
        timed=event.timed,
        # Set from the endpoint, never from the payload: a client cannot claim
        # its manual entry was captured telemetry.
        source=source,
        capture_confidence=_capped_confidence(event.capture_confidence, source),
        notes=event.notes,
        raw_metadata=event.raw_metadata,
    )


async def _record_event(
    session: AsyncSession,
    *,
    user: User,
    event: AttemptEventIn,
    device_id: uuid.UUID | None,
    status: ProcessingStatus,
    attempt_id: uuid.UUID | None,
    error: str | None,
) -> AttemptEvent:
    row = AttemptEvent(
        event_uuid=event.event_uuid,
        device_id=device_id,
        user_id=user.id,
        payload=event.model_dump(mode="json"),
        processing_status=status,
        attempt_id=attempt_id,
        error=error,
        processed_at=datetime.now(UTC),
    )
    session.add(row)
    return row
