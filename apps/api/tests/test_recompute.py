"""Readiness recompute (spec §3.3).

The spec has promised since v3 that amending an attempt "recomputes the affected
ratings and schedules from the corrected evidence." Nothing implemented it:
`amended_at` was set and every derived number kept its pre-amendment value. This
is that recompute, and these are the tests that make the promise real.

The load-bearing test is `test_replay_reproduces_the_incremental_result`. Both
models are incremental, so replay is only a valid correction strategy if folding
the same evidence forward from scratch lands exactly where folding it forward one
attempt at a time did. If that ever stops holding, every recompute silently
changes numbers nobody asked it to change.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dsa_coach.models import (
    Attempt,
    PatternBaselineBucket,
    PatternRating,
    ReadinessPrediction,
    Resolution,
    User,
)
from dsa_coach.services import readiness as readiness_service

SLUGS = ["two-sum", "contains-duplicate", "valid-palindrome", "binary-search"]

# Relative to now, so every attempt lands inside the amendment window (§3.3).
# A fixed date would drift out of the window as the calendar moved on, and the
# amendment tests would start failing for a reason unrelated to the code.
BASE = datetime.now(UTC) - timedelta(days=len(SLUGS))


def event(slug: str, *, day: int = 0, resolution: str = "independent") -> dict[str, object]:
    return {
        "event_uuid": str(uuid.uuid4()),
        "problem_slug": slug,
        "submitted_at": (BASE + timedelta(days=day)).isoformat(),
        "resolution": resolution,
        "submission_outcome": "accepted" if resolution == "independent" else "wrong_answer",
        "blocker": None if resolution == "independent" else "pattern_known_impl_failed",
        "active_seconds": 900,
    }


async def practice(client: AsyncClient, slugs: list[str], **kwargs: object) -> None:
    for day, slug in enumerate(slugs):
        response = await client.post("/attempts", json=event(slug, day=day, **kwargs))  # type: ignore[arg-type]
        assert response.status_code == 201, response.text


async def the_user(session: AsyncSession) -> User:
    return (await session.execute(select(User))).scalar_one()


async def snapshot(session: AsyncSession, user: User) -> dict[str, object]:
    """Every derived readiness number, in a comparable shape."""
    buckets = (
        (
            await session.execute(
                select(PatternBaselineBucket)
                .where(PatternBaselineBucket.user_id == user.id)
                .order_by(PatternBaselineBucket.pattern_id, PatternBaselineBucket.bucket)
            )
        )
        .scalars()
        .all()
    )
    ratings = (
        (
            await session.execute(
                select(PatternRating)
                .where(PatternRating.user_id == user.id)
                .order_by(PatternRating.pattern_id)
            )
        )
        .scalars()
        .all()
    )
    return {
        "buckets": [
            (str(b.pattern_id), b.bucket, round(b.alpha, 9), round(b.beta, 9), b.evidence_count)
            for b in buckets
        ],
        "ratings": [
            (str(r.pattern_id), round(r.rating, 9), round(r.rd, 9), r.evidence_count)
            for r in ratings
        ],
    }


class TestReplayFidelity:
    async def test_replay_reproduces_the_incremental_result(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        """Recompute with nothing changed must be a no-op, to nine decimals.

        This is the assumption the whole strategy rests on. If replaying from
        scratch does not land where the incremental path landed, then every
        amendment silently perturbs unrelated numbers, and the "correction"
        introduces more error than it fixes.
        """
        await practice(onboarded, SLUGS)
        user = await the_user(session)
        before = await snapshot(session, user)

        await readiness_service.recompute(session, user)
        after = await snapshot(session, user)

        assert after == before

    async def test_repeated_recomputes_do_not_drift(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        # Idempotence. A recompute that moved the numbers a little each time
        # would be indistinguishable from real evidence arriving.
        await practice(onboarded, SLUGS)
        user = await the_user(session)

        await readiness_service.recompute(session, user)
        once = await snapshot(session, user)
        await readiness_service.recompute(session, user)
        twice = await snapshot(session, user)

        assert twice == once

    async def test_evidence_counts_are_not_inflated_by_replay(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        # Counts are shown to the user and gate calibration. Replaying without
        # clearing first would double every one of them.
        await practice(onboarded, SLUGS)
        user = await the_user(session)
        before = await session.scalar(
            select(func.sum(PatternBaselineBucket.evidence_count)).where(
                PatternBaselineBucket.user_id == user.id
            )
        )

        await readiness_service.recompute(session, user)
        after = await session.scalar(
            select(func.sum(PatternBaselineBucket.evidence_count)).where(
                PatternBaselineBucket.user_id == user.id
            )
        )

        assert after == before


class TestCorrection:
    async def test_a_corrected_attempt_changes_the_estimate(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        """The whole point: amended evidence reaches the readiness numbers."""
        await practice(onboarded, SLUGS)
        user = await the_user(session)
        before = await snapshot(session, user)

        # The correction §3.3 exists for: solved it, but only after the editorial.
        attempt = (
            await session.execute(
                select(Attempt)
                .where(Attempt.user_id == user.id)
                .order_by(Attempt.submitted_at)
                .limit(1)
            )
        ).scalar_one()
        attempt.resolution = Resolution.AFTER_EDITORIAL
        attempt.amended_at = datetime.now(UTC)
        await session.flush()

        await readiness_service.recompute(session, user)
        after = await snapshot(session, user)

        assert after != before

    async def test_predictions_are_rebuilt_not_appended(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        # Calibration (spec §6.3) compares forecasts against outcomes. Leaving
        # the old rows behind would mix in forecasts made against evidence that
        # has since been corrected, quietly poisoning the bake-off.
        await practice(onboarded, SLUGS)
        user = await the_user(session)
        before = await session.scalar(
            select(func.count())
            .select_from(ReadinessPrediction)
            .where(ReadinessPrediction.user_id == user.id)
        )

        await readiness_service.recompute(session, user)
        after = await session.scalar(
            select(func.count())
            .select_from(ReadinessPrediction)
            .where(ReadinessPrediction.user_id == user.id)
        )

        assert after == before

    async def test_evidence_itself_is_never_touched(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        # Recompute drops derived state only. An attempt is a fact (invariant 1);
        # if recompute could delete one, a correction would destroy history.
        await practice(onboarded, SLUGS)
        user = await the_user(session)
        before = await session.scalar(
            select(func.count()).select_from(Attempt).where(Attempt.user_id == user.id)
        )

        await readiness_service.recompute(session, user)
        after = await session.scalar(
            select(func.count()).select_from(Attempt).where(Attempt.user_id == user.id)
        )

        assert after == before == len(SLUGS)


class TestEdgeCases:
    async def test_a_user_with_no_attempts_recomputes_to_nothing(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        user = await the_user(session)

        result = await readiness_service.recompute(session, user)

        assert result.attempts_replayed == 0
        assert result.attempts_counted == 0

    async def test_it_reports_what_it_replayed(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        await practice(onboarded, SLUGS)
        user = await the_user(session)

        result = await readiness_service.recompute(session, user)

        assert result.attempts_replayed == len(SLUGS)
        # Not every attempt necessarily produces evidence — an untagged problem
        # or a discarded resolution yields none — so counted may be lower.
        assert result.attempts_counted <= result.attempts_replayed

    @pytest.mark.parametrize("resolution", ["independent", "failed", "after_hint"])
    async def test_replay_is_faithful_for_every_resolution(
        self, onboarded: AsyncClient, session: AsyncSession, resolution: str
    ) -> None:
        # Each resolution maps to a different evidence score; a replay that were
        # faithful only for successes would be worse than none at all.
        await practice(onboarded, SLUGS, resolution=resolution)
        user = await the_user(session)
        before = await snapshot(session, user)

        await readiness_service.recompute(session, user)

        assert await snapshot(session, user) == before


class TestAmendmentEndpoint:
    """Spec §3.3's actual user-facing promise."""

    async def test_amending_records_the_correction_and_replays(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        await practice(onboarded, SLUGS, resolution="failed")
        attempts = (await onboarded.get("/attempts")).json()
        target = attempts[0]

        response = await onboarded.patch(
            f"/attempts/{target['id']}",
            json={"resolution": "after_editorial"},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["attempt"]["resolution"] == "after_editorial"
        assert body["attempt"]["amended_at"] is not None
        assert body["changed_fields"] == ["resolution"]
        # The correction reached the models rather than only the row.
        assert body["attempts_replayed"] == len(SLUGS)

    async def test_the_previous_value_is_kept_not_overwritten(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        await practice(onboarded, SLUGS, resolution="failed")
        target = (await onboarded.get("/attempts")).json()[0]

        await onboarded.patch(f"/attempts/{target['id']}", json={"resolution": "after_editorial"})
        await onboarded.patch(f"/attempts/{target['id']}", json={"resolution": "after_hint"})

        row = (
            await session.execute(select(Attempt).where(Attempt.id == uuid.UUID(target["id"])))
        ).scalar_one()
        await session.refresh(row)

        # Both corrections survive: the history is appended to, not replaced.
        assert len(row.prior_values or []) == 2
        assert row.prior_values[0]["was"]["resolution"] == "failed"
        assert row.prior_values[1]["was"]["resolution"] == "after_editorial"

    async def test_amending_changes_the_readiness_estimate(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        # The end-to-end version of the promise: a correction moves the numbers.
        await practice(onboarded, SLUGS, resolution="failed")
        user = await the_user(session)
        before = await snapshot(session, user)
        target = (await onboarded.get("/attempts")).json()[0]

        await onboarded.patch(f"/attempts/{target['id']}", json={"resolution": "independent"})
        session.expunge_all()

        assert await snapshot(session, user) != before

    async def test_a_no_op_amendment_is_not_recorded_as_one(self, onboarded: AsyncClient) -> None:
        # Stamping `amended_at` for a correction that changed nothing would
        # claim history that did not happen.
        await practice(onboarded, SLUGS, resolution="failed")
        target = (await onboarded.get("/attempts")).json()[0]

        response = await onboarded.patch(f"/attempts/{target['id']}", json={"resolution": "failed"})

        body = response.json()
        assert body["changed_fields"] == []
        assert body["attempts_replayed"] == 0
        assert body["attempt"]["amended_at"] is None

    async def test_a_blocker_can_be_cleared(self, onboarded: AsyncClient) -> None:
        # None means "not supplied", so removing a wrongly-reported blocker
        # needs its own flag rather than being unreachable.
        await practice(onboarded, SLUGS, resolution="failed")
        target = (await onboarded.get("/attempts")).json()[0]
        assert target["blocker"] is not None

        response = await onboarded.patch(f"/attempts/{target['id']}", json={"clear_blocker": True})

        assert response.json()["attempt"]["blocker"] is None

    async def test_telemetry_is_not_amendable(self, onboarded: AsyncClient) -> None:
        # Timings and verdicts are observed fact. Editing them by hand would be
        # fabrication (invariant 5), so the schema has nowhere to put them.
        await practice(onboarded, SLUGS)
        target = (await onboarded.get("/attempts")).json()[0]

        response = await onboarded.patch(
            f"/attempts/{target['id']}",
            json={"active_seconds": 1, "submission_outcome": "accepted", "run_count": 99},
        )

        body = response.json()
        assert body["changed_fields"] == []
        assert body["attempt"]["active_seconds"] == 900

    async def test_an_attempt_outside_the_window_is_refused(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        await practice(onboarded, SLUGS)
        user = await the_user(session)
        stale = (
            await session.execute(select(Attempt).where(Attempt.user_id == user.id).limit(1))
        ).scalar_one()
        stale.submitted_at = datetime.now(UTC) - timedelta(days=30)
        await session.commit()

        response = await onboarded.patch(f"/attempts/{stale.id}", json={"resolution": "after_hint"})

        assert response.status_code == 409
        assert "amendable" in response.json()["detail"]

    async def test_another_users_attempt_is_not_found(self, onboarded: AsyncClient) -> None:
        response = await onboarded.patch(
            f"/attempts/{uuid.uuid4()}", json={"resolution": "after_hint"}
        )

        assert response.status_code == 404
