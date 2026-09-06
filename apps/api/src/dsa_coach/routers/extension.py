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

from dsa_coach.auth import CurrentUser, DbSession, Scope, require_scope
from dsa_coach.config import Settings, get_settings
from dsa_coach.ingest import ingest_events
from dsa_coach.models import AttemptSource
from dsa_coach.schemas import AttemptBatchIn, BatchResultOut

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
    settings: Annotated[Settings, Depends(get_settings)],
) -> BatchResultOut:
    if len(payload.events) > settings.max_batch_size:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Batch exceeds maximum of {settings.max_batch_size} events.",
        )

    results = await ingest_events(session, user, payload.events, AttemptSource.EXTENSION)
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
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    """Runtime config the content script needs. No secrets are ever returned here."""
    return {
        "max_batch_size": settings.max_batch_size,
        # Phase 2 wires these to real consent + pause state.
        "monitoring_enabled": False,
        "code_capture_enabled": False,
        "idle_threshold_seconds": 300,
        "host_permissions": ["https://leetcode.com/problems/*"],
    }
