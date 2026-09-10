"""Extension ingestion endpoints.

Phase 0 provides the batch contract the Phase 2 extension will speak: idempotent
by `event_uuid`, size-capped, per-event validation, partial success, server-assigned
receipt timestamps (spec §14).

Device authentication (pairing, hashed revocable device tokens, scope enforcement)
arrives with the extension in Phase 2. The scope is already declared here so the
route does not change when it does.
"""

from collections import Counter
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from dsa_coach.auth import Auth, CurrentUser, DbSession, Scope, require_scope
from dsa_coach.config import Settings, get_settings
from dsa_coach.ingest import ingest_events, resolve_or_create_problem
from dsa_coach.models import AttemptSource, SnapshotKind
from dsa_coach.schemas import (
    AttemptBatchIn,
    BatchResultOut,
    SnapshotBatchIn,
    SnapshotResultOut,
)
from dsa_coach.services import consent as consent_service
from dsa_coach.services import snapshots as snapshot_service

router = APIRouter(prefix="/extension", tags=["extension"])


@router.post(
    "/events/batch",
    response_model=BatchResultOut,
    dependencies=[Depends(require_scope(Scope.EXTENSION_INGEST))],
)
async def ingest_batch(
    payload: AttemptBatchIn,
    user: CurrentUser,
    session: DbSession,
    auth: Auth,
    settings: Annotated[Settings, Depends(get_settings)],
) -> BatchResultOut:
    if len(payload.events) > settings.max_batch_size:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Batch exceeds maximum of {settings.max_batch_size} events.",
        )

    results = await ingest_events(
        session,
        user,
        payload.events,
        AttemptSource.EXTENSION,
        # Evidence records which credential contributed it.
        device_id=auth.device.id if auth.device else None,
    )
    counts = Counter(r.status for r in results)

    return BatchResultOut(
        received=len(results),
        accepted=counts["accepted"],
        duplicates=counts["duplicate"],
        invalid=counts["invalid"],
        results=results,
        server_received_at=datetime.now(UTC),
    )


@router.get("/config", dependencies=[Depends(require_scope(Scope.EXTENSION_INGEST))])
async def extension_config(
    user: CurrentUser,
    session: DbSession,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    """Runtime config the content script needs. No secrets are ever returned here.

    `code_capture_enabled` is the server's answer, not the extension's opinion.
    Invariant 9 puts consent behind an explicit, versioned decision, so the
    extension must ask rather than decide — and revoking consent in the
    dashboard has to stop capture at the source, not merely stop storage.
    """
    consent = await consent_service.state(session, user)
    return {
        "max_batch_size": settings.max_batch_size,
        "monitoring_enabled": True,
        "code_capture_enabled": consent.may_send,
        "host_permissions": ["https://leetcode.com/problems/*"],
    }


@router.post(
    "/snapshots",
    response_model=SnapshotResultOut,
    dependencies=[Depends(require_scope(Scope.EXTENSION_INGEST))],
)
async def store_snapshots(
    payload: SnapshotBatchIn,
    user: CurrentUser,
    session: DbSession,
) -> SnapshotResultOut:
    """Record the source from one or more Runs/Submits (spec §3.6).

    Deliberately separate from attempt ingestion: runs happen *before* any
    attempt exists, and the sequence of them is the thing worth keeping.

    Nothing is analysed here. Snapshots are stored raw and left alone until the
    problem is solved or abandoned, so a practice session never waits on a model
    and a provider outage costs nothing but a delayed conclusion.
    """
    stored = 0
    refused = 0
    for item in payload.snapshots:
        problem = await resolve_or_create_problem(session, item.provider, item.problem_slug)
        snapshot = await snapshot_service.store(
            session,
            user,
            snapshot_uuid=item.snapshot_uuid,
            problem=problem,
            kind=SnapshotKind(item.kind),
            code=item.code,
            language=item.language,
            captured_at=item.captured_at,
        )
        if snapshot is None:
            refused += 1
        else:
            stored += 1

    return SnapshotResultOut(stored=stored, refused_no_consent=refused)
