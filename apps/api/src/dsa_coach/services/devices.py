"""Device pairing and revocation (spec §5).

The flow, and why each step is shaped this way:

1. The dashboard asks for a pairing code. It is short-lived, single-use, and
   capped at a few failed guesses.
2. The extension exchanges the code for a device token. That token is returned
   **once** and only its hash is kept.
3. Every extension request carries the token. It resolves to a device, and the
   device's scopes decide what it may do — an extension may write attempts and
   read its own config, and nothing else.
4. Revocation is immediate and per device. A revoked token stops working on the
   next request, with no cache to wait out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dsa_coach import security
from dsa_coach.models import Device, DeviceKind, PairingCode, User

#: How long a pairing code stays usable. Short, because it is a bearer secret
#: sitting on screen.
PAIRING_TTL_MINUTES = 10

#: Scopes granted to a browser extension. Deliberately minimal: an extension can
#: contribute evidence and read its own configuration. It cannot read the plan,
#: the coach, attempt history, or settings.
EXTENSION_SCOPES = ["extension:ingest"]


class PairingError(Exception):
    """A pairing attempt that failed, with a reason safe to show the user."""


@dataclass(frozen=True)
class IssuedCode:
    code: str
    expires_at: datetime


@dataclass(frozen=True)
class IssuedDevice:
    device: Device
    #: Plaintext, returned once. Never stored, never recoverable.
    token: str


async def issue_pairing_code(
    session: AsyncSession, user: User, *, now: datetime | None = None
) -> IssuedCode:
    """Mint a pairing code, invalidating any earlier unused one.

    Only one code is live at a time, so a code left on a forgotten screen cannot
    be used later.
    """
    now = now or datetime.now(UTC)

    outstanding = (
        (
            await session.execute(
                select(PairingCode).where(
                    PairingCode.user_id == user.id, PairingCode.consumed_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    for stale in outstanding:
        stale.consumed_at = now

    code, code_hash = security.generate_pairing_code()
    expires_at = now + timedelta(minutes=PAIRING_TTL_MINUTES)
    session.add(PairingCode(user_id=user.id, code_hash=code_hash, expires_at=expires_at))
    await session.flush()
    return IssuedCode(code=code, expires_at=expires_at)


async def redeem_pairing_code(
    session: AsyncSession,
    entered_code: str,
    *,
    device_name: str,
    kind: DeviceKind = DeviceKind.EXTENSION,
    now: datetime | None = None,
) -> IssuedDevice:
    """Exchange a pairing code for a device token."""
    now = now or datetime.now(UTC)
    normalized = security.normalize_pairing_code(entered_code)
    code_hash = security.hash_secret(normalized)

    row = (
        await session.execute(select(PairingCode).where(PairingCode.code_hash == code_hash))
    ).scalar_one_or_none()

    if row is None:
        # Charge the guess against every live code, so brute force burns through
        # the attempt cap rather than probing indefinitely.
        await _record_failed_attempt(session, now)
        raise PairingError("That pairing code is not valid.")

    if row.consumed_at is not None:
        raise PairingError("That pairing code has already been used.")
    if row.expires_at <= now:
        raise PairingError("That pairing code has expired. Generate a new one.")
    if row.failed_attempts >= security.MAX_PAIRING_ATTEMPTS:
        raise PairingError("Too many failed attempts. Generate a new code.")

    token, token_hash = security.generate_device_token()
    device = Device(
        user_id=row.user_id,
        name=device_name[:120] or "Browser extension",
        kind=kind,
        token_hash=token_hash,
        scopes=list(EXTENSION_SCOPES) if kind is DeviceKind.EXTENSION else [],
    )
    session.add(device)
    row.consumed_at = now
    await session.flush()

    return IssuedDevice(device=device, token=token)


async def _record_failed_attempt(session: AsyncSession, now: datetime) -> None:
    """Count a wrong guess — and commit it.

    The explicit commit matters. `redeem_pairing_code` raises straight after
    calling this, and the request-scoped session rolls back on any exception, so
    without committing here the counter resets on every failure and the
    brute-force cap silently does nothing. This is state about the *attempt*, not
    part of the transaction the attempt failed to complete.
    """
    live = (
        (
            await session.execute(
                select(PairingCode).where(
                    PairingCode.consumed_at.is_(None), PairingCode.expires_at > now
                )
            )
        )
        .scalars()
        .all()
    )
    for row in live:
        row.failed_attempts += 1
    await session.commit()


async def resolve_device(
    session: AsyncSession, token: str, *, now: datetime | None = None
) -> Device | None:
    """Look up an active device by its token. Revoked devices resolve to None."""
    now = now or datetime.now(UTC)
    device = (
        await session.execute(
            select(Device).where(Device.token_hash == security.hash_secret(token))
        )
    ).scalar_one_or_none()

    if device is None or not device.active:
        return None

    device.last_seen_at = now
    return device


async def list_devices(session: AsyncSession, user: User) -> list[Device]:
    return list(
        (
            await session.execute(
                select(Device).where(Device.user_id == user.id).order_by(Device.created_at.desc())
            )
        )
        .scalars()
        .all()
    )


async def revoke_device(
    session: AsyncSession, user: User, device_id: UUID, *, now: datetime | None = None
) -> Device | None:
    """Revoke immediately. Idempotent: revoking twice is not an error."""
    now = now or datetime.now(UTC)
    device = (
        await session.execute(
            select(Device).where(Device.id == device_id, Device.user_id == user.id)
        )
    ).scalar_one_or_none()

    if device is None:
        return None
    if device.revoked_at is None:
        device.revoked_at = now
        await session.flush()
    return device
