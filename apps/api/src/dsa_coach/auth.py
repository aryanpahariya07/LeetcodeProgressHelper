"""Identity boundary.

Invariant 10: identity comes from server-side context. Never from a client-supplied
ID, never from a model-generated tool argument.

Phase 0 is local single-user, so `get_current_user()` resolves *the* user row. The
signature is the one Phase 2 (device tokens, spec §5) and any later multi-user auth
must satisfy, so replacing the body is the only change required.
"""

from enum import StrEnum
from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dsa_coach.db import get_session
from dsa_coach.models import User


class Scope(StrEnum):
    """Token scopes (spec §5). Enforced from Phase 2; defined now so routes can
    declare their requirement immediately and never have to be revisited."""

    DASHBOARD = "dashboard"
    #  An extension device may write attempt events and read its own config.
    #  It may NOT read the plan, the coach, or user settings.
    EXTENSION_INGEST = "extension:ingest"


async def get_current_user(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    """Resolve the acting user from server-side context only."""
    result = await session.execute(select(User).order_by(User.created_at).limit(1))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No user exists yet. Complete onboarding first.",
        )
    return user


def require_scope(scope: Scope):  # type: ignore[no-untyped-def]
    """Declare the scope a route requires.

    Phase 0 has no tokens to check, so this is a pass-through that records intent.
    Phase 2 fills in the body; routes do not change.
    """

    async def _dependency() -> Scope:
        return scope

    return _dependency


CurrentUser = Annotated[User, Depends(get_current_user)]
DbSession = Annotated[AsyncSession, Depends(get_session)]
