"""Ingestion, deduplication and partial-success behaviour.

Invariant 7: all ingestion is idempotent, deduplicated by client-generated
`event_uuid`.
"""

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dsa_coach.models import Attempt, AttemptEvent, AttemptSource, Problem, ProcessingStatus


def _event(slug: str = "two-sum", **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "event_uuid": str(uuid.uuid4()),
        "problem_slug": slug,
        "submitted_at": datetime.now(UTC).isoformat(),
        "resolution": "independent",
        "submission_outcome": "accepted",
        "active_seconds": 900,
        "language": "python",
    }
    payload.update(overrides)
    return payload


async def _attempt_count(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(Attempt))).scalar_one()


class TestManualLogging:
    async def test_accepts_a_valid_attempt(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        response = await onboarded.post("/attempts", json=_event())

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == "accepted"
        assert body["attempt_id"] is not None
        assert await _attempt_count(session) == 1

    async def test_source_is_server_assigned_not_client_supplied(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        """A client must not be able to claim manual entry was captured telemetry."""
        await onboarded.post("/attempts", json=_event(source="extension"))

        attempt = (await session.execute(select(Attempt))).scalar_one()
        assert attempt.source is AttemptSource.MANUAL

    async def test_unknown_problem_is_recorded_without_inventing_metadata(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        """Invariant 5, without discarding the evidence to satisfy it.

        This used to reject the attempt outright, which honoured invariant 5 by
        throwing away a fact. The attempt happened; only the rating is unknown.
        So the attempt is stored and the rating stays null — see
        tests/test_uncatalogued.py for what that then does to the estimate.
        """
        response = await onboarded.post("/attempts", json=_event(slug="not-a-real-problem"))

        assert response.json()["status"] == "accepted"
        assert await _attempt_count(session) == 1

        problem = (
            await session.execute(select(Problem).where(Problem.slug == "not-a-real-problem"))
        ).scalar_one()
        assert problem.rating is None
        assert problem.is_active is False

    async def test_dismissed_questionnaire_is_recorded_as_unknown(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        """Dismissal is a valid outcome, not a failure (spec §3.2)."""
        await onboarded.post("/attempts", json=_event(resolution="unknown"))

        attempt = (await session.execute(select(Attempt))).scalar_one()
        assert attempt.resolution.value == "unknown"


class TestIdempotency:
    async def test_replaying_the_same_event_creates_no_second_attempt(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        event = _event()

        first = await onboarded.post("/attempts", json=event)
        second = await onboarded.post("/attempts", json=event)

        assert first.json()["status"] == "accepted"
        assert second.json()["status"] == "duplicate"
        assert second.json()["attempt_id"] == first.json()["attempt_id"]
        assert await _attempt_count(session) == 1

    async def test_replaying_an_uncatalogued_event_stays_deduplicated(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        """Every inbound event is recorded, so a replay is still a no-op.

        Uncatalogued problems are the path that used to produce `invalid`. They
        are accepted now, and the idempotency guarantee has to survive that:
        creating the placeholder problem must not make a replayed event look new.
        """
        event = _event(slug="does-not-exist")

        first = await onboarded.post("/attempts", json=event)
        second = await onboarded.post("/attempts", json=event)

        assert first.json()["status"] == "accepted"
        assert second.json()["status"] == "duplicate"

        events = (await session.execute(select(AttemptEvent))).scalars().all()
        assert len(events) == 1
        assert events[0].processing_status is ProcessingStatus.PROCESSED

    async def test_same_problem_different_event_uuid_is_a_new_attempt(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        """Dedup is on event identity, not on problem identity — you may
        legitimately attempt the same problem twice."""
        await onboarded.post("/attempts", json=_event())
        await onboarded.post("/attempts", json=_event())

        assert await _attempt_count(session) == 2


class TestExtensionBatch:
    async def test_partial_success_reports_each_event_independently(
        self,
        onboarded: AsyncClient,
        session: AsyncSession,
        extension_auth: dict[str, str],
    ) -> None:
        duplicate = _event("valid-parentheses")
        await onboarded.post("/attempts", json=duplicate)

        batch = {
            "events": [
                _event("two-sum"),
                duplicate,
                _event("no-such-slug"),
                _event("climbing-stairs"),
            ]
        }
        response = await onboarded.post(
            "/extension/events/batch", json=batch, headers=extension_auth
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["received"] == 4
        # An uncatalogued slug is accepted now rather than rejected, so the
        # only non-accepted result left is the genuine replay. Per-event
        # independence is what this asserts, not the specific mix.
        assert body["accepted"] == 3
        assert body["duplicates"] == 1
        assert [r["status"] for r in body["results"]] == [
            "accepted",
            "duplicate",
            "accepted",
            "accepted",
        ]

    async def test_one_bad_event_does_not_roll_back_the_good_ones(
        self,
        onboarded: AsyncClient,
        session: AsyncSession,
        extension_auth: dict[str, str],
    ) -> None:
        # A malformed payload rather than an unknown slug: unknown slugs are a
        # normal, accepted case now, so they no longer exercise savepoint
        # isolation. A duplicate does — it fails partway and must not take the
        # rest of the batch down with it.
        shared = _event("valid-parentheses")
        await onboarded.post("/attempts", json=shared)

        batch = {"events": [_event("two-sum"), shared, _event("3sum")]}

        await onboarded.post("/extension/events/batch", json=batch, headers=extension_auth)

        # The two new ones landed; the replay did not roll them back.
        assert await _attempt_count(session) == 3

    async def test_batch_events_are_recorded_as_extension_source(
        self,
        onboarded: AsyncClient,
        session: AsyncSession,
        extension_auth: dict[str, str],
    ) -> None:
        await onboarded.post(
            "/extension/events/batch", json={"events": [_event()]}, headers=extension_auth
        )

        attempt = (await session.execute(select(Attempt))).scalar_one()
        assert attempt.source is AttemptSource.EXTENSION

    async def test_replaying_a_whole_batch_is_a_no_op(
        self,
        onboarded: AsyncClient,
        session: AsyncSession,
        extension_auth: dict[str, str],
    ) -> None:
        batch = {"events": [_event("two-sum"), _event("3sum"), _event("subsets")]}

        first = await onboarded.post("/extension/events/batch", json=batch, headers=extension_auth)
        second = await onboarded.post("/extension/events/batch", json=batch, headers=extension_auth)

        assert first.json()["accepted"] == 3
        assert second.json()["accepted"] == 0
        assert second.json()["duplicates"] == 3
        assert await _attempt_count(session) == 3

    async def test_oversized_batch_is_rejected(
        self,
        onboarded: AsyncClient,
        extension_auth: dict[str, str],
    ) -> None:
        batch = {"events": [_event() for _ in range(101)]}

        response = await onboarded.post(
            "/extension/events/batch", json=batch, headers=extension_auth
        )

        assert response.status_code == 413

    async def test_empty_batch_is_a_validation_error(
        self,
        onboarded: AsyncClient,
        extension_auth: dict[str, str],
    ) -> None:
        response = await onboarded.post(
            "/extension/events/batch", json={"events": []}, headers=extension_auth
        )

        assert response.status_code == 422


class TestUncertainCapture:
    """Invariant 6: uncertain telemetry is recorded as uncertain, never guessed."""

    async def test_low_confidence_and_missing_fields_are_preserved(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        event = _event(capture_confidence="low", active_seconds=None, language=None)

        await onboarded.post("/attempts", json=event)

        attempt = (await session.execute(select(Attempt))).scalar_one()
        assert attempt.capture_confidence.value == "low"
        assert attempt.active_seconds is None
        assert attempt.language is None

    @pytest.mark.parametrize("value", [0, 6, -1])
    async def test_confidence_outside_one_to_five_is_rejected(
        self, onboarded: AsyncClient, value: int
    ) -> None:
        response = await onboarded.post("/attempts", json=_event(confidence_cold_redo=value))

        assert response.status_code == 422


class TestConfidenceCeiling:
    """Invariant 6: the server knows the source, so the server caps the claim."""

    async def test_manual_entry_cannot_claim_high_confidence(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        await onboarded.post("/attempts", json=_event(capture_confidence="high"))

        attempt = (await session.execute(select(Attempt))).scalar_one()
        assert attempt.capture_confidence.value == "medium"

    async def test_extension_capture_may_claim_high_confidence(
        self,
        onboarded: AsyncClient,
        session: AsyncSession,
        extension_auth: dict[str, str],
    ) -> None:
        await onboarded.post(
            "/extension/events/batch",
            json={"events": [_event(capture_confidence="high")]},
            headers=extension_auth,
        )

        attempt = (await session.execute(select(Attempt))).scalar_one()
        assert attempt.capture_confidence.value == "high"

    async def test_a_lower_claim_is_never_raised(
        self,
        onboarded: AsyncClient,
        session: AsyncSession,
        extension_auth: dict[str, str],
    ) -> None:
        await onboarded.post(
            "/extension/events/batch",
            json={"events": [_event(capture_confidence="low")]},
            headers=extension_auth,
        )

        attempt = (await session.execute(select(Attempt))).scalar_one()
        assert attempt.capture_confidence.value == "low"
