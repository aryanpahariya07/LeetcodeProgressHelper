"""Attempts on problems the catalogue does not know (spec §3.1, §11).

The catalogue holds a few dozen problems; LeetCode has thousands. Practising
anything outside the seeded set used to produce `Unknown problem: <slug>`, mark
the event `invalid`, and discard the evidence — silently, because the extension
drops a rejected event from its queue and nothing in the product surfaced it.
Nine real attempts vanished that way before anyone noticed.

The attempt is a fact and is now stored. The rating is genuinely unknown and
stays null rather than being invented (invariant 5), which is what keeps the
readiness estimate honest: an unrated problem contributes no evidence at all
until its rating is known.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dsa_coach.models import (
    Attempt,
    AttemptEvent,
    PatternBaselineBucket,
    Problem,
    ProcessingStatus,
    User,
)
from dsa_coach.services import readiness as readiness_service

UNKNOWN = "remove-element"  # genuinely absent from the seeded catalogue


def event(slug: str, **overrides: object) -> dict[str, object]:
    return {
        "event_uuid": str(uuid.uuid4()),
        "problem_slug": slug,
        "submitted_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
        "resolution": "independent",
        "submission_outcome": "accepted",
        "active_seconds": 600,
        **overrides,
    }


class TestUncataloguedIsRecorded:
    async def test_the_attempt_is_accepted_not_rejected(self, onboarded: AsyncClient) -> None:
        response = await onboarded.post("/attempts", json=event(UNKNOWN))

        assert response.status_code == 201, response.text
        assert response.json()["status"] == "accepted"

    async def test_the_evidence_survives(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        await onboarded.post("/attempts", json=event(UNKNOWN))

        attempts = (await session.execute(select(Attempt))).scalars().all()

        assert len(attempts) == 1

    async def test_the_placeholder_problem_claims_nothing_it_does_not_know(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        # The heart of it: recording the attempt must not mean inventing a
        # difficulty or a rating for a problem nobody has catalogued.
        await onboarded.post("/attempts", json=event(UNKNOWN))

        problem = (
            await session.execute(select(Problem).where(Problem.slug == UNKNOWN))
        ).scalar_one()

        assert problem.rating is None
        assert problem.difficulty is None
        assert problem.is_active is False
        assert problem.catalogue_source_id is None

    async def test_the_event_is_not_marked_invalid(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        # `invalid` is what silently lost the evidence. The event must now be
        # recorded as processed, with the attempt it produced attached.
        await onboarded.post("/attempts", json=event(UNKNOWN))

        stored = (await session.execute(select(AttemptEvent))).scalars().all()

        assert len(stored) == 1
        assert stored[0].processing_status is ProcessingStatus.PROCESSED
        assert stored[0].error is None
        assert stored[0].attempt_id is not None

    async def test_a_second_attempt_reuses_the_same_placeholder(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        # Otherwise the unique (provider, slug) index would reject the second
        # attempt on a problem you are working through across several sessions.
        await onboarded.post("/attempts", json=event(UNKNOWN))
        await onboarded.post("/attempts", json=event(UNKNOWN))

        problems = (
            (await session.execute(select(Problem).where(Problem.slug == UNKNOWN))).scalars().all()
        )

        assert len(problems) == 1

    async def test_ingestion_stays_idempotent(self, onboarded: AsyncClient) -> None:
        # Invariant 7 holds regardless of which path created the problem.
        payload = event(UNKNOWN)
        await onboarded.post("/attempts", json=payload)

        again = await onboarded.post("/attempts", json=payload)

        assert again.json()["status"] == "duplicate"


class TestUnratedContributesNoEstimate:
    async def test_an_unrated_attempt_moves_no_readiness(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        # The honesty guarantee. Without a rating there is no "how hard was
        # this", so there is nothing to conclude about skill — and a made-up
        # difficulty would corrupt every band derived from it.
        await onboarded.post("/attempts", json=event(UNKNOWN))

        buckets = await session.scalar(select(func.count()).select_from(PatternBaselineBucket))

        assert buckets == 0

    async def test_a_catalogued_attempt_still_does(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        # The control: the pipeline is not simply broken for everything.
        await onboarded.post("/attempts", json=event("two-sum"))

        buckets = await session.scalar(select(func.count()).select_from(PatternBaselineBucket))

        assert buckets and buckets > 0

    async def test_build_facts_declines_an_unrated_problem(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        await onboarded.post("/attempts", json=event(UNKNOWN))
        user = (await session.execute(select(User))).scalar_one()
        attempt = (await session.execute(select(Attempt))).scalar_one()

        assert await readiness_service.build_facts(session, user, attempt) is None

    async def test_recompute_skips_unrated_without_failing(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        # Replay walks every attempt, including ones that cannot contribute.
        # It must skip them rather than raise partway through the rebuild.
        await onboarded.post("/attempts", json=event(UNKNOWN))
        await onboarded.post("/attempts", json=event("two-sum"))
        user = (await session.execute(select(User))).scalar_one()

        result = await readiness_service.recompute(session, user)

        assert result.attempts_replayed == 2
        assert result.attempts_counted == 1
