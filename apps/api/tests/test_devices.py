"""Device pairing, scope enforcement and revocation (spec §5).

The three claims worth testing: a token is never recoverable from the database, a
credential can only do what its scopes allow, and revocation is immediate.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dsa_coach import security
from dsa_coach.models import Device, PairingCode
from dsa_coach.services import devices as device_service


def attempt_event(slug: str = "two-sum") -> dict[str, object]:
    return {
        "event_uuid": str(uuid.uuid4()),
        "problem_slug": slug,
        "submitted_at": datetime.now(UTC).isoformat(),
        "resolution": "independent",
        "submission_outcome": "accepted",
    }


async def pair(client: AsyncClient) -> str:
    """Complete a pairing and return the device token."""
    code = (await client.post("/devices/pairing-code")).json()["code"]
    response = await client.post(
        "/extension/pair", json={"code": code, "device_name": "Test browser"}
    )
    assert response.status_code == 201, response.text
    return response.json()["token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestSecrets:
    def test_pairing_codes_avoid_ambiguous_characters(self) -> None:
        """The code gets read off a screen and retyped."""
        for _ in range(50):
            code, _ = security.generate_pairing_code()
            assert not set(code) & set("IlO01")

    def test_pairing_code_input_is_forgiving(self) -> None:
        assert security.normalize_pairing_code(" ab-cd efgh ") == "ABCDEFGH"

    def test_device_tokens_are_unique_and_long(self) -> None:
        tokens = {security.generate_device_token()[0] for _ in range(100)}

        assert len(tokens) == 100
        assert all(len(t) >= 40 for t in tokens)

    def test_comparison_is_constant_time(self) -> None:
        token, digest = security.generate_device_token()

        assert security.secrets_match(token, digest)
        assert not security.secrets_match(token + "x", digest)


class TestPairing:
    async def test_pairing_returns_a_token_once(self, onboarded: AsyncClient) -> None:
        token = await pair(onboarded)

        assert token

    async def test_the_token_is_not_recoverable_from_the_database(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        """Only the hash is stored — a database leak must not yield credentials."""
        token = await pair(onboarded)

        device = (await session.execute(select(Device))).scalar_one()
        assert token not in device.token_hash
        assert device.token_hash == security.hash_secret(token)

    async def test_the_token_is_never_returned_again(self, onboarded: AsyncClient) -> None:
        await pair(onboarded)

        listing = (await onboarded.get("/devices")).json()

        assert listing
        assert all("token" not in device for device in listing)

    async def test_a_code_cannot_be_used_twice(self, onboarded: AsyncClient) -> None:
        code = (await onboarded.post("/devices/pairing-code")).json()["code"]
        first = await onboarded.post("/extension/pair", json={"code": code})
        second = await onboarded.post("/extension/pair", json={"code": code})

        assert first.status_code == 201
        assert second.status_code == 400
        assert "already been used" in second.json()["detail"]

    async def test_issuing_a_new_code_invalidates_the_old_one(self, onboarded: AsyncClient) -> None:
        """A code left on a forgotten screen must not stay redeemable."""
        first = (await onboarded.post("/devices/pairing-code")).json()["code"]
        await onboarded.post("/devices/pairing-code")

        response = await onboarded.post("/extension/pair", json={"code": first})

        assert response.status_code == 400

    async def test_a_wrong_code_is_rejected(self, onboarded: AsyncClient) -> None:
        await onboarded.post("/devices/pairing-code")

        response = await onboarded.post("/extension/pair", json={"code": "AAAA-AAAA"})

        assert response.status_code == 400
        assert "not valid" in response.json()["detail"]

    async def test_repeated_wrong_guesses_burn_the_live_code(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        """Entropy is modest, so the attempt cap is what actually protects it."""
        code = (await onboarded.post("/devices/pairing-code")).json()["code"]

        for _ in range(security.MAX_PAIRING_ATTEMPTS):
            await onboarded.post("/extension/pair", json={"code": "ZZZZ-ZZZZ"})

        row = (await session.execute(select(PairingCode))).scalar_one()
        assert row.failed_attempts >= security.MAX_PAIRING_ATTEMPTS

        response = await onboarded.post("/extension/pair", json={"code": code})
        assert response.status_code == 400
        assert "Too many failed attempts" in response.json()["detail"]

    async def test_an_expired_code_is_rejected(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        code = (await onboarded.post("/devices/pairing-code")).json()["code"]

        row = (await session.execute(select(PairingCode))).scalar_one()
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

        response = await onboarded.post("/extension/pair", json={"code": code})

        assert response.status_code == 400
        assert "expired" in response.json()["detail"]

    async def test_pairing_is_case_and_hyphen_insensitive(self, onboarded: AsyncClient) -> None:
        code = (await onboarded.post("/devices/pairing-code")).json()["code"]

        response = await onboarded.post(
            "/extension/pair", json={"code": code.replace("-", "").lower()}
        )

        assert response.status_code == 201


class TestScopes:
    async def test_a_device_token_can_ingest_attempts(self, onboarded: AsyncClient) -> None:
        token = await pair(onboarded)

        response = await onboarded.post(
            "/extension/events/batch",
            json={"events": [attempt_event()]},
            headers=bearer(token),
        )

        assert response.status_code == 200
        assert response.json()["accepted"] == 1

    async def test_a_device_token_cannot_read_the_plan(self, onboarded: AsyncClient) -> None:
        """An extension contributes evidence. It has no business reading the plan."""
        token = await pair(onboarded)

        response = await onboarded.get("/plan/current", headers=bearer(token))

        assert response.status_code == 403
        assert "dashboard" in response.json()["detail"]

    async def test_a_device_token_cannot_read_progress(self, onboarded: AsyncClient) -> None:
        token = await pair(onboarded)

        response = await onboarded.get("/progress/readiness", headers=bearer(token))

        assert response.status_code == 403

    async def test_a_device_token_cannot_list_or_revoke_devices(
        self, onboarded: AsyncClient
    ) -> None:
        """A stolen extension token must not be able to unpair the real device."""
        token = await pair(onboarded)

        listing = await onboarded.get("/devices", headers=bearer(token))

        assert listing.status_code == 403

    async def test_the_extension_can_read_its_own_config(self, onboarded: AsyncClient) -> None:
        token = await pair(onboarded)

        response = await onboarded.get("/extension/config", headers=bearer(token))

        assert response.status_code == 200

    async def test_an_unknown_token_is_rejected(self, onboarded: AsyncClient) -> None:
        response = await onboarded.get("/extension/config", headers=bearer("not-a-real-token"))

        assert response.status_code == 401

    @pytest.mark.parametrize("header", ["", "Bearer", "Basic abc", "Bearer   "])
    async def test_malformed_authorization_headers_do_not_grant_access(
        self, onboarded: AsyncClient, header: str
    ) -> None:
        """A malformed header must not silently fall through to the local session."""
        response = await onboarded.get("/extension/config", headers={"Authorization": header})

        assert response.status_code == 403, "no device scope without a valid token"


class TestRevocation:
    async def test_revoking_blocks_the_next_request(self, onboarded: AsyncClient) -> None:
        """Immediate — there is no cache to wait out."""
        token = await pair(onboarded)
        device_id = (await onboarded.get("/devices")).json()[0]["id"]

        before = await onboarded.post(
            "/extension/events/batch",
            json={"events": [attempt_event()]},
            headers=bearer(token),
        )
        await onboarded.delete(f"/devices/{device_id}")
        after = await onboarded.post(
            "/extension/events/batch",
            json={"events": [attempt_event("3sum")]},
            headers=bearer(token),
        )

        assert before.status_code == 200
        assert after.status_code == 401

    async def test_a_revoked_device_is_still_listed(self, onboarded: AsyncClient) -> None:
        """Revocation is an audit record, not a deletion."""
        await pair(onboarded)
        device_id = (await onboarded.get("/devices")).json()[0]["id"]

        await onboarded.delete(f"/devices/{device_id}")
        listing = (await onboarded.get("/devices")).json()

        assert len(listing) == 1
        assert listing[0]["active"] is False
        assert listing[0]["revoked_at"] is not None

    async def test_revoking_twice_is_not_an_error(self, onboarded: AsyncClient) -> None:
        await pair(onboarded)
        device_id = (await onboarded.get("/devices")).json()[0]["id"]

        first = await onboarded.delete(f"/devices/{device_id}")
        second = await onboarded.delete(f"/devices/{device_id}")

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["revoked_at"] == second.json()["revoked_at"]

    async def test_revoking_an_unknown_device_is_a_404(self, onboarded: AsyncClient) -> None:
        response = await onboarded.delete(f"/devices/{uuid.uuid4()}")

        assert response.status_code == 404

    async def test_revoking_one_device_leaves_others_working(self, onboarded: AsyncClient) -> None:
        first_token = await pair(onboarded)
        second_token = await pair(onboarded)

        devices = (await onboarded.get("/devices")).json()
        # Listed newest first, so the second pairing is index 0.
        second_id = devices[0]["id"]

        await onboarded.delete(f"/devices/{second_id}")

        revoked = await onboarded.get("/extension/config", headers=bearer(second_token))
        survivor = await onboarded.get("/extension/config", headers=bearer(first_token))

        assert revoked.status_code == 401
        assert survivor.status_code == 200


class TestAttribution:
    async def test_ingested_events_record_the_device(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        """Evidence should say which credential contributed it."""
        token = await pair(onboarded)
        await onboarded.post(
            "/extension/events/batch",
            json={"events": [attempt_event()]},
            headers=bearer(token),
        )

        device = (await session.execute(select(Device))).scalar_one()
        from dsa_coach.models import AttemptEvent

        event = (await session.execute(select(AttemptEvent))).scalar_one()
        assert event.device_id == device.id

    async def test_last_seen_is_updated_on_use(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        token = await pair(onboarded)
        await onboarded.get("/extension/config", headers=bearer(token))

        device = (await session.execute(select(Device))).scalar_one()
        assert device.last_seen_at is not None


class TestServiceLayer:
    async def test_resolve_rejects_a_revoked_device(
        self, onboarded: AsyncClient, session: AsyncSession
    ) -> None:
        token = await pair(onboarded)
        device = (await session.execute(select(Device))).scalar_one()
        device.revoked_at = datetime.now(UTC)
        await session.commit()

        assert await device_service.resolve_device(session, token) is None
