"""Run snapshots and unfinished episodes (spec §3.6).

The deterministic half of the conclusion pipeline: capturing each Run/Submit's
source, grouping the open ones into an episode, and listing what was worked on
but never solved. No model is involved in any of it.

Two properties are load-bearing:

- **Nothing is stored without consent** (invariant 9). The gate lives in the
  service rather than the endpoint, so no future caller can route around it.
- **An episode is the open set.** There is no episode table; snapshots with no
  `conclusion_id` are the current attempt at a problem. If that ever stops
  holding, a new episode silently inherits the previous one's runs and the
  conclusion is drawn from a sequence that never happened.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dsa_coach.models import (
    AttemptConclusion,
    AttemptSnapshot,
    EpisodeOutcome,
    Problem,
    User,
)
from dsa_coach.services import snapshots as snapshot_service

NOW = datetime.now(UTC)


def snap(slug: str = "two-sum", kind: str = "run", code: str = "class Solution {}") -> dict:
    return {
        "snapshot_uuid": str(uuid.uuid4()),
        "problem_slug": slug,
        "kind": kind,
        "language": "cpp",
        "code": code,
        "captured_at": NOW.isoformat(),
    }


async def allow_code(client: AsyncClient) -> None:
    await client.post("/consents/code-capture", json={"decision": "always"})


async def the_user(session: AsyncSession) -> User:
    return (await session.execute(select(User))).scalar_one()


async def count_snapshots(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(AttemptSnapshot)) or 0


class TestConsentGate:
    async def test_nothing_is_stored_without_consent(
        self, onboarded: AsyncClient, extension_auth: dict[str, str], session: AsyncSession
    ) -> None:
        response = await onboarded.post(
            "/extension/snapshots", json={"snapshots": [snap()]}, headers=extension_auth
        )

        assert response.status_code == 200, response.text
        assert response.json() == {"stored": 0, "refused_no_consent": 1}
        assert await count_snapshots(session) == 0

    async def test_refusal_is_reported_not_swallowed(
        self, onboarded: AsyncClient, extension_auth: dict[str, str]
    ) -> None:
        # An extension sending code that is never kept should be able to tell.
        body = (
            await onboarded.post(
                "/extension/snapshots",
                json={"snapshots": [snap(), snap(kind="submit")]},
                headers=extension_auth,
            )
        ).json()

        assert body["refused_no_consent"] == 2

    async def test_stored_once_consented(
        self, onboarded: AsyncClient, extension_auth: dict[str, str], session: AsyncSession
    ) -> None:
        await allow_code(onboarded)

        response = await onboarded.post(
            "/extension/snapshots", json={"snapshots": [snap()]}, headers=extension_auth
        )

        assert response.json()["stored"] == 1
        assert await count_snapshots(session) == 1


class TestCapture:
    async def test_the_source_survives_verbatim(
        self, onboarded: AsyncClient, extension_auth: dict[str, str], session: AsyncSession
    ) -> None:
        await allow_code(onboarded)
        code = "class Solution:\n    def twoSum(self):\n        pass  # éè"

        await onboarded.post(
            "/extension/snapshots",
            json={"snapshots": [snap(code=code)]},
            headers=extension_auth,
        )

        stored = (await session.execute(select(AttemptSnapshot))).scalar_one()
        assert stored.code == code

    async def test_replaying_a_snapshot_stores_it_once(
        self, onboarded: AsyncClient, extension_auth: dict[str, str], session: AsyncSession
    ) -> None:
        # Invariant 7. The extension retries; a retried run is not a second run.
        await allow_code(onboarded)
        payload = snap()

        await onboarded.post(
            "/extension/snapshots", json={"snapshots": [payload]}, headers=extension_auth
        )
        await onboarded.post(
            "/extension/snapshots", json={"snapshots": [payload]}, headers=extension_auth
        )

        assert await count_snapshots(session) == 1

    async def test_an_uncatalogued_problem_is_accepted(
        self, onboarded: AsyncClient, extension_auth: dict[str, str], session: AsyncSession
    ) -> None:
        # Same reasoning as attempts (§3.1): most practice is outside the seeded
        # catalogue, and refusing it would throw the sequence away.
        await allow_code(onboarded)

        await onboarded.post(
            "/extension/snapshots",
            json={"snapshots": [snap(slug="some-unseen-problem")]},
            headers=extension_auth,
        )

        problem = (
            await session.execute(select(Problem).where(Problem.slug == "some-unseen-problem"))
        ).scalar_one()
        assert problem.rating is None
        assert await count_snapshots(session) == 1

    async def test_retention_is_thirty_days_out(
        self, onboarded: AsyncClient, extension_auth: dict[str, str], session: AsyncSession
    ) -> None:
        await allow_code(onboarded)

        await onboarded.post(
            "/extension/snapshots", json={"snapshots": [snap()]}, headers=extension_auth
        )

        stored = (await session.execute(select(AttemptSnapshot))).scalar_one()
        assert timedelta(days=29) < stored.retention_until - datetime.now(UTC) <= timedelta(days=30)


class TestOpenEpisode:
    async def test_the_sequence_comes_back_in_order(
        self, onboarded: AsyncClient, extension_auth: dict[str, str], session: AsyncSession
    ) -> None:
        # The order is the information. A clean solve and four passes at the same
        # off-by-one differ only in the sequence, not the final source.
        await allow_code(onboarded)
        for index in range(3):
            payload = snap(code=f"attempt {index}")
            payload["captured_at"] = (NOW + timedelta(minutes=index)).isoformat()
            await onboarded.post(
                "/extension/snapshots", json={"snapshots": [payload]}, headers=extension_auth
            )

        user = await the_user(session)
        problem = (
            await session.execute(select(Problem).where(Problem.slug == "two-sum"))
        ).scalar_one()
        episode = await snapshot_service.open_episode(session, user, problem.id)

        assert [s.code for s in episode] == ["attempt 0", "attempt 1", "attempt 2"]

    async def test_a_consumed_snapshot_leaves_the_episode(
        self, onboarded: AsyncClient, extension_auth: dict[str, str], session: AsyncSession
    ) -> None:
        # What stops the next episode inheriting the previous one's runs, and
        # concluding from a sequence that never happened.
        await allow_code(onboarded)
        await onboarded.post(
            "/extension/snapshots", json={"snapshots": [snap()]}, headers=extension_auth
        )
        user = await the_user(session)
        problem = (
            await session.execute(select(Problem).where(Problem.slug == "two-sum"))
        ).scalar_one()

        conclusion = AttemptConclusion(
            user_id=user.id, problem_id=problem.id, outcome=EpisodeOutcome.ABANDONED
        )
        session.add(conclusion)
        await session.flush()
        stored = (await session.execute(select(AttemptSnapshot))).scalar_one()
        stored.conclusion_id = conclusion.id
        await session.flush()

        assert await snapshot_service.open_episode(session, user, problem.id) == []

    async def test_episodes_do_not_bleed_between_problems(
        self, onboarded: AsyncClient, extension_auth: dict[str, str], session: AsyncSession
    ) -> None:
        await allow_code(onboarded)
        await onboarded.post(
            "/extension/snapshots",
            json={"snapshots": [snap("two-sum"), snap("3sum")]},
            headers=extension_auth,
        )
        user = await the_user(session)
        problem = (
            await session.execute(select(Problem).where(Problem.slug == "two-sum"))
        ).scalar_one()

        assert len(await snapshot_service.open_episode(session, user, problem.id)) == 1


class TestUnfinishedList:
    async def test_a_problem_with_runs_and_no_pass_is_listed(
        self, onboarded: AsyncClient, extension_auth: dict[str, str]
    ) -> None:
        await allow_code(onboarded)
        await onboarded.post(
            "/extension/snapshots",
            json={"snapshots": [snap(), snap(), snap(kind="submit")]},
            headers=extension_auth,
        )

        rows = (await onboarded.get("/progress/unfinished")).json()

        assert len(rows) == 1
        assert rows[0]["slug"] == "two-sum"
        assert rows[0]["run_count"] == 2
        assert rows[0]["submit_count"] == 1

    async def test_a_solved_problem_drops_off_the_list(
        self, onboarded: AsyncClient, extension_auth: dict[str, str]
    ) -> None:
        await allow_code(onboarded)
        await onboarded.post(
            "/extension/snapshots", json={"snapshots": [snap()]}, headers=extension_auth
        )

        await onboarded.post(
            "/attempts",
            json={
                "event_uuid": str(uuid.uuid4()),
                "problem_slug": "two-sum",
                "submitted_at": NOW.isoformat(),
                "resolution": "independent",
                "submission_outcome": "accepted",
            },
        )

        assert (await onboarded.get("/progress/unfinished")).json() == []

    async def test_a_failed_submission_keeps_it_listed(
        self, onboarded: AsyncClient, extension_auth: dict[str, str]
    ) -> None:
        # Failing is not finishing. This is exactly the case the list exists for.
        await allow_code(onboarded)
        await onboarded.post(
            "/extension/snapshots", json={"snapshots": [snap()]}, headers=extension_auth
        )
        await onboarded.post(
            "/attempts",
            json={
                "event_uuid": str(uuid.uuid4()),
                "problem_slug": "two-sum",
                "submitted_at": NOW.isoformat(),
                "resolution": "failed",
                "submission_outcome": "wrong_answer",
            },
        )

        rows = (await onboarded.get("/progress/unfinished")).json()

        assert [r["slug"] for r in rows] == ["two-sum"]

    async def test_nothing_practised_means_an_empty_list(self, onboarded: AsyncClient) -> None:
        assert (await onboarded.get("/progress/unfinished")).json() == []


class TestRetentionPurge:
    async def test_expired_snapshots_are_deleted(
        self, onboarded: AsyncClient, extension_auth: dict[str, str], session: AsyncSession
    ) -> None:
        await allow_code(onboarded)
        await onboarded.post(
            "/extension/snapshots", json={"snapshots": [snap()]}, headers=extension_auth
        )
        stored = (await session.execute(select(AttemptSnapshot))).scalar_one()
        stored.retention_until = datetime.now(UTC) - timedelta(days=1)
        await session.flush()

        removed = await snapshot_service.purge_expired(session)

        assert removed == 1
        assert await count_snapshots(session) == 0

    async def test_live_snapshots_survive_a_purge(
        self, onboarded: AsyncClient, extension_auth: dict[str, str], session: AsyncSession
    ) -> None:
        await allow_code(onboarded)
        await onboarded.post(
            "/extension/snapshots", json={"snapshots": [snap()]}, headers=extension_auth
        )

        assert await snapshot_service.purge_expired(session) == 0
        assert await count_snapshots(session) == 1


class TestAbandon:
    async def test_abandoning_closes_the_episode(
        self, onboarded: AsyncClient, extension_auth: dict[str, str]
    ) -> None:
        await allow_code(onboarded)
        await onboarded.post(
            "/extension/snapshots",
            json={"snapshots": [snap(), snap(), snap(kind="submit")]},
            headers=extension_auth,
        )
        listed = (await onboarded.get("/progress/unfinished")).json()

        response = await onboarded.post(f"/progress/unfinished/{listed[0]['problem_id']}/abandon")

        assert response.status_code == 200, response.text
        assert response.json()["runs_recorded"] == 2
        assert (await onboarded.get("/progress/unfinished")).json() == []

    async def test_the_snapshots_are_kept_not_deleted(
        self, onboarded: AsyncClient, extension_auth: dict[str, str], session: AsyncSession
    ) -> None:
        # Giving up is an outcome worth learning from — the runs are what the
        # conclusion will be drawn from, so abandoning must not discard them.
        await allow_code(onboarded)
        await onboarded.post(
            "/extension/snapshots", json={"snapshots": [snap()]}, headers=extension_auth
        )
        listed = (await onboarded.get("/progress/unfinished")).json()

        await onboarded.post(f"/progress/unfinished/{listed[0]['problem_id']}/abandon")

        assert await count_snapshots(session) == 1

    async def test_returning_later_starts_a_fresh_episode(
        self, onboarded: AsyncClient, extension_auth: dict[str, str]
    ) -> None:
        # Abandoning is not permanent (spec §3.6). The new runs must not be
        # mixed with the ones already concluded on.
        await allow_code(onboarded)
        await onboarded.post(
            "/extension/snapshots", json={"snapshots": [snap()]}, headers=extension_auth
        )
        listed = (await onboarded.get("/progress/unfinished")).json()
        await onboarded.post(f"/progress/unfinished/{listed[0]['problem_id']}/abandon")

        await onboarded.post(
            "/extension/snapshots", json={"snapshots": [snap()]}, headers=extension_auth
        )

        again = (await onboarded.get("/progress/unfinished")).json()
        assert len(again) == 1
        assert again[0]["run_count"] == 1

    async def test_abandoning_nothing_is_a_404_not_a_crash(self, onboarded: AsyncClient) -> None:
        response = await onboarded.post(f"/progress/unfinished/{uuid.uuid4()}/abandon")

        assert response.status_code == 404

    async def test_abandoning_twice_is_refused_not_duplicated(
        self, onboarded: AsyncClient, extension_auth: dict[str, str]
    ) -> None:
        await allow_code(onboarded)
        await onboarded.post(
            "/extension/snapshots", json={"snapshots": [snap()]}, headers=extension_auth
        )
        listed = (await onboarded.get("/progress/unfinished")).json()
        problem_id = listed[0]["problem_id"]

        await onboarded.post(f"/progress/unfinished/{problem_id}/abandon")
        second = await onboarded.post(f"/progress/unfinished/{problem_id}/abandon")

        assert second.status_code == 404
