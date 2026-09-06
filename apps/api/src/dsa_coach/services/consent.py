"""Consent and code storage (spec §8).

Code capture is **off by default**. Nothing here has a default that stores code;
storing requires a positive, recorded, revocable decision.

Three things this module is careful about:

- **The disclosure is versioned by its own text.** If the wording of what the
  user agreed to changes materially, its hash changes and consent must be asked
  for again. Consent to a weaker disclosure is not consent to this one.
- **"Once" really means once.** The code is used for the request and never
  written. That is a distinct decision from "always", not a softer version of it.
- **Revoking deletes.** Not "stops collecting" — deletes what is already there.
  A revocation that leaves the data behind is not a revocation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from dsa_coach.models import (
    Attempt,
    AttemptCode,
    Consent,
    ConsentDecision,
    ConsentScope,
    User,
)

CONSENT_VERSION = "1"

#: The exact words the user agrees to. Changing them changes the hash, which
#: invalidates every consent granted against the old wording.
CODE_CAPTURE_DISCLOSURE = (
    "To diagnose a failed attempt or review a solution, DSA Coach sends the code "
    "you wrote to OpenAI. Choosing 'always' also stores that code locally in this "
    "app's database for 90 days, so past attempts can be re-examined. Choosing "
    "'once' analyses this submission and stores nothing. You can delete all stored "
    "code, or withdraw this permission entirely, at any time in Settings — "
    "withdrawing deletes what has already been stored."
)

#: How long stored code lives before a scheduled job removes it (spec §8).
RETENTION_DAYS = 90


def disclosure_hash(text: str | None = None) -> str:
    """Hash the disclosure text as it stands *now*.

    Read at call time, not bound as a default argument: a default is evaluated
    once at import, so editing the disclosure would have left the hash frozen
    and stale consent would have kept passing. The whole point of hashing the
    wording is that changing the wording invalidates agreement to it.
    """
    return hashlib.sha256((text or CODE_CAPTURE_DISCLOSURE).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ConsentState:
    scope: ConsentScope
    decision: ConsentDecision | None
    #: True when the user has never been asked, or was asked under older wording.
    needs_prompt: bool
    disclosure: str
    version: str

    @property
    def may_store(self) -> bool:
        return self.decision is ConsentDecision.ALWAYS

    @property
    def may_send(self) -> bool:
        """Whether code may be sent to the provider for this request."""
        return self.decision in {ConsentDecision.ALWAYS, ConsentDecision.ONCE}


async def _active(session: AsyncSession, user: User, scope: ConsentScope) -> Consent | None:
    return (
        await session.execute(
            select(Consent)
            .where(
                Consent.user_id == user.id,
                Consent.scope == scope,
                Consent.revoked_at.is_(None),
            )
            .order_by(Consent.granted_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def state(
    session: AsyncSession, user: User, scope: ConsentScope = ConsentScope.CODE_CAPTURE
) -> ConsentState:
    """The current decision, and whether the user must be asked again."""
    current = await _active(session, user, scope)
    expected = disclosure_hash()

    if current is None:
        return ConsentState(
            scope=scope,
            decision=None,
            needs_prompt=True,
            disclosure=CODE_CAPTURE_DISCLOSURE,
            version=CONSENT_VERSION,
        )

    # A stale disclosure means they agreed to different words. Ask again.
    stale = current.disclosure_text_hash != expected
    # "Once" is spent as soon as it is used, so it is never a standing answer.
    spent = current.decision is ConsentDecision.ONCE

    return ConsentState(
        scope=scope,
        decision=current.decision,
        needs_prompt=stale or spent,
        disclosure=CODE_CAPTURE_DISCLOSURE,
        version=CONSENT_VERSION,
    )


async def record(
    session: AsyncSession,
    user: User,
    decision: ConsentDecision,
    scope: ConsentScope = ConsentScope.CODE_CAPTURE,
) -> Consent:
    """Record a decision, superseding any earlier one.

    Choosing `never` also deletes anything already stored: declining to continue
    is not the same as being content with what was kept.
    """
    now = datetime.now(UTC)
    previous = await _active(session, user, scope)
    if previous is not None:
        previous.revoked_at = now

    consent = Consent(
        user_id=user.id,
        scope=scope,
        decision=decision,
        consent_version=CONSENT_VERSION,
        disclosure_text_hash=disclosure_hash(),
        granted_at=now,
    )
    session.add(consent)

    if decision is ConsentDecision.NEVER:
        await delete_all_code(session, user)

    await session.flush()
    return consent


async def revoke(
    session: AsyncSession, user: User, scope: ConsentScope = ConsentScope.CODE_CAPTURE
) -> int:
    """Withdraw consent and delete what was stored under it.

    Returns how many stored snippets were removed, so the UI can say so rather
    than claiming a vague success.
    """
    now = datetime.now(UTC)
    current = await _active(session, user, scope)
    if current is not None:
        current.revoked_at = now
    removed = await delete_all_code(session, user)
    await session.flush()
    return removed


async def store_code(
    session: AsyncSession,
    user: User,
    attempt: Attempt,
    code: str,
    language: str | None = None,
) -> AttemptCode | None:
    """Persist code — but only under a standing `always` consent.

    Returns None when consent does not permit storage. The caller may still have
    been allowed to *use* the code for one request; that is a different question,
    answered by `ConsentState.may_send`.
    """
    current = await state(session, user)
    if not current.may_store:
        return None

    existing = (
        await session.execute(select(AttemptCode).where(AttemptCode.attempt_id == attempt.id))
    ).scalar_one_or_none()

    now = datetime.now(UTC)
    retention_until = now + timedelta(days=RETENTION_DAYS)

    if existing is not None:
        existing.code = code
        existing.language = language
        existing.retention_until = retention_until
        existing.deleted_at = None
        await session.flush()
        return existing

    row = AttemptCode(
        attempt_id=attempt.id,
        user_id=user.id,
        language=language,
        code=code,
        retention_until=retention_until,
    )
    session.add(row)
    await session.flush()
    return row


async def get_code(session: AsyncSession, user: User, attempt_id: UUID) -> AttemptCode | None:
    return (
        await session.execute(
            select(AttemptCode).where(
                AttemptCode.attempt_id == attempt_id,
                AttemptCode.user_id == user.id,
                AttemptCode.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def delete_all_code(session: AsyncSession, user: User) -> int:
    """Hard-delete every stored snippet for this user.

    A real DELETE, not a flag. `attempt_code` is a separate table precisely so
    this can happen without touching a single piece of practice evidence.
    """
    rows = (
        (await session.execute(select(AttemptCode).where(AttemptCode.user_id == user.id)))
        .scalars()
        .all()
    )
    count = len(rows)
    if count:
        await session.execute(delete(AttemptCode).where(AttemptCode.user_id == user.id))
        await session.flush()
    return count


async def purge_expired(session: AsyncSession, now: datetime | None = None) -> int:
    """Delete stored code past its retention window (spec §8).

    Meant to be run on a schedule. Idempotent.
    """
    now = now or datetime.now(UTC)
    rows = (
        (await session.execute(select(AttemptCode.id).where(AttemptCode.retention_until <= now)))
        .scalars()
        .all()
    )
    if rows:
        await session.execute(delete(AttemptCode).where(AttemptCode.id.in_(rows)))
        await session.flush()
    return len(rows)
