"""Identity and authorisation boundary.

Invariant 10: identity comes from server-side context. Never from a client-supplied
ID, never from a model-generated tool argument. A device token identifies a
*credential*; the user it maps to is looked up here, not asserted by the caller.

Two callers exist:

- **The extension** presents a device token (spec §5). It is resolved to a device,
  and the device's scopes decide what it may do. Revocation takes effect on the
  very next request.
- **The local dashboard** presents nothing. This build is single-user and binds to
  localhost, so the dashboard is trusted by locality; `AuthContext` already carries
  the shape a real session token will slot into.

Whichever path is used, routes see the same `AuthContext`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dsa_coach.db import get_session
from dsa_coach.models import Device, User
from dsa_coach.services import devices as device_service


class Scope(StrEnum):
    """Token scopes (spec §5)."""

    DASHBOARD = "dashboard"
    #: An extension device may write attempt events and read its own config.
    #: It may NOT read the plan, the coach, attempt history, or settings.
    EXTENSION_INGEST = "extension:ingest"


@dataclass(frozen=True)
class AuthContext:
    user: User
    device: Device | None
    scopes: frozenset[str]

    def allows(self, scope: Scope) -> bool:
        return scope.value in self.scopes


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


async def get_auth_context(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AuthContext:
    """Resolve the caller. Server-side only."""
    token = _bearer_token(request)

    if token is not None:
        device = await device_service.resolve_device(session, token)
        if device is None:
            # Covers unknown, malformed and revoked tokens alike — saying which
            # would tell an attacker whether a token was ever real.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or revoked device token.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        user = (await session.execute(select(User).where(User.id == device.user_id))).scalar_one()
        return AuthContext(user=user, device=device, scopes=frozenset(device.scopes))

    local_user = (
        await session.execute(select(User).order_by(User.created_at).limit(1))
    ).scalar_one_or_none()
    if local_user is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No user exists yet. Complete onboarding first.",
        )
    return AuthContext(user=local_user, device=None, scopes=frozenset({Scope.DASHBOARD.value}))


async def get_current_user(
    context: Annotated[AuthContext, Depends(get_auth_context)],
) -> User:
    """The acting user, however they authenticated."""
    return context.user


def require_scope(scope: Scope):  # type: ignore[no-untyped-def]
    """Declare — and enforce — the scope a route requires."""

    async def _dependency(
        context: Annotated[AuthContext, Depends(get_auth_context)],
    ) -> AuthContext:
        if not context.allows(scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This credential does not have the {scope.value} scope.",
            )
        return context

    return _dependency


CurrentUser = Annotated[User, Depends(get_current_user)]
DbSession = Annotated[AsyncSession, Depends(get_session)]
Auth = Annotated[AuthContext, Depends(get_auth_context)]
