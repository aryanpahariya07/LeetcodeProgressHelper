"""Device pairing and revocation endpoints (spec §5)."""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from dsa_coach.auth import CurrentUser, DbSession, Scope, require_scope
from dsa_coach.models import DeviceKind
from dsa_coach.schemas import (
    DeviceOut,
    PairingCodeOut,
    PairingRequest,
    PairingResult,
)
from dsa_coach.services import devices as device_service

router = APIRouter(tags=["devices"])


@router.post(
    "/devices/pairing-code",
    response_model=PairingCodeOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scope(Scope.DASHBOARD))],
)
async def create_pairing_code(user: CurrentUser, session: DbSession) -> PairingCodeOut:
    """Mint a short-lived code for the user to type into the extension.

    Issuing a new code invalidates any earlier unused one, so a code left on a
    forgotten screen cannot be redeemed later.
    """
    issued = await device_service.issue_pairing_code(session, user)
    remaining = int((issued.expires_at - datetime.now(UTC)).total_seconds())
    return PairingCodeOut(
        code=issued.code,
        expires_at=issued.expires_at,
        expires_in_seconds=max(0, remaining),
    )


@router.post("/extension/pair", response_model=PairingResult, status_code=status.HTTP_201_CREATED)
async def pair_extension(payload: PairingRequest, session: DbSession) -> PairingResult:
    """Exchange a pairing code for a device token.

    Deliberately unauthenticated — the code *is* the credential. It is single-use,
    expires in minutes, and dies after a handful of wrong guesses.
    """
    try:
        issued = await device_service.redeem_pairing_code(
            session,
            payload.code,
            device_name=payload.device_name,
            kind=DeviceKind.EXTENSION,
        )
    except device_service.PairingError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error

    return PairingResult(
        device_id=issued.device.id,
        device_name=issued.device.name,
        token=issued.token,
        scopes=list(issued.device.scopes),
    )


@router.get(
    "/devices",
    response_model=list[DeviceOut],
    dependencies=[Depends(require_scope(Scope.DASHBOARD))],
)
async def list_devices(user: CurrentUser, session: DbSession) -> list[DeviceOut]:
    devices = await device_service.list_devices(session, user)
    return [DeviceOut.model_validate(d) for d in devices]


@router.delete(
    "/devices/{device_id}",
    response_model=DeviceOut,
    dependencies=[Depends(require_scope(Scope.DASHBOARD))],
)
async def revoke_device(device_id: UUID, user: CurrentUser, session: DbSession) -> DeviceOut:
    """Revoke a device. Takes effect on its very next request."""
    device = await device_service.revoke_device(session, user, device_id)
    if device is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown device.")
    return DeviceOut.model_validate(device)
