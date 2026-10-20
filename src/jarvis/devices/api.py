"""Authenticated HTTP routes for the existing persistent device service."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from jarvis.devices.models import (
    CapabilityName,
    CapabilityReport,
    Device,
    DeviceId,
    DevicePlatform,
    DeviceRole,
    PairingMetadata,
)
from jarvis.devices.service import DeviceAuthenticationError, DeviceService

MAX_DEVICE_REQUEST_BYTES = 16 * 1024
_bearer = HTTPBearer(auto_error=False)
router = APIRouter(prefix="/devices", tags=["devices"])


class DeviceRequestSizeLimitMiddleware:
    """Limit device API bodies, including streamed bodies without Content-Length."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_DEVICE_REQUEST_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or (path != "/devices" and not path.startswith("/devices/")):
            await self.app(scope, receive, send)
            return

        content_length = next(
            (value for name, value in scope.get("headers", []) if name.lower() == b"content-length"),
            None,
        )
        if content_length is not None:
            try:
                if int(content_length) > self.max_bytes:
                    await _send_too_large(send)
                    return
            except ValueError:
                pass

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            await _send_too_large(send)


class _RequestBodyTooLarge(Exception):
    pass


async def _send_too_large(send: Send) -> None:
    body = json.dumps(
        {"error": {"code": "request_too_large", "message": "Request body exceeds the allowed size."}},
        separators=(",", ":"),
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


class _ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PairDeviceRequest(PairingMetadata):
    pairing_code: SecretStr = Field(min_length=1, max_length=128)


class DeviceResponse(_ApiModel):
    id: DeviceId
    name: str
    platform: DevicePlatform
    role: DeviceRole
    reported_capabilities: frozenset[CapabilityName]
    paired_at: datetime
    last_seen_at: datetime
    revoked_at: datetime | None

    @classmethod
    def from_device(cls, device: Device) -> DeviceResponse:
        return cls(
            id=device.id,
            name=device.name,
            platform=device.platform,
            role=device.role,
            reported_capabilities=device.reported_capabilities,
            paired_at=device.paired_at,
            last_seen_at=device.last_seen_at,
            revoked_at=device.revoked_at,
        )


class PairingResponse(_ApiModel):
    device: DeviceResponse
    access_token: str = Field(min_length=1, max_length=256)


class PairingCodeResponse(_ApiModel):
    pairing_code: str = Field(min_length=1, max_length=128)
    expires_in_seconds: int = 300


class OwnerCredentialRotationResponse(_ApiModel):
    device: DeviceResponse
    access_token: str = Field(min_length=1, max_length=256)
    expires_at: datetime


class DeviceListResponse(_ApiModel):
    devices: tuple[DeviceResponse, ...]


def device_service(request: Request) -> DeviceService:
    service = getattr(request.app.state, "device_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Device API is not configured.")
    return service


async def authenticated_device(
    service: Annotated[DeviceService, Depends(device_service)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> tuple[SecretStr, Device]:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise DeviceAuthenticationError("Device authentication failed.")
    token = SecretStr(credentials.credentials)
    return token, service.authenticate(token)


@router.post("/pair", response_model=PairingResponse, status_code=201)
async def pair_device(
    payload: PairDeviceRequest,
    service: Annotated[DeviceService, Depends(device_service)],
    response: Response,
) -> PairingResponse:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    receipt = service.pair(
        payload.pairing_code,
        name=payload.name,
        platform=payload.platform,
        reported_capabilities=payload.reported_capabilities,
    )
    return PairingResponse(
        device=DeviceResponse.from_device(receipt.device),
        access_token=receipt.access_token.get_secret_value(),
    )


@router.get("/me", response_model=DeviceResponse)
async def get_current_device(
    authenticated: Annotated[tuple[SecretStr, Device], Depends(authenticated_device)],
) -> DeviceResponse:
    return DeviceResponse.from_device(authenticated[1])


@router.patch("/me/capabilities", response_model=DeviceResponse)
async def update_capabilities(
    payload: CapabilityReport,
    authenticated: Annotated[tuple[SecretStr, Device], Depends(authenticated_device)],
    service: Annotated[DeviceService, Depends(device_service)],
) -> DeviceResponse:
    device = service.update_reported_capabilities(authenticated[0], payload.reported_capabilities)
    return DeviceResponse.from_device(device)


@router.post("/pairing-codes", response_model=PairingCodeResponse, status_code=201)
async def issue_pairing_code(
    authenticated: Annotated[tuple[SecretStr, Device], Depends(authenticated_device)],
    service: Annotated[DeviceService, Depends(device_service)],
    response: Response,
) -> PairingCodeResponse:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    code = service.issue_pairing_code(authenticated[0])
    return PairingCodeResponse(pairing_code=code.get_secret_value())


@router.post(
    "/owner-credential/rotate",
    response_model=OwnerCredentialRotationResponse,
    status_code=200,
)
async def rotate_owner_credential(
    authenticated: Annotated[tuple[SecretStr, Device], Depends(authenticated_device)],
    service: Annotated[DeviceService, Depends(device_service)],
    response: Response,
) -> OwnerCredentialRotationResponse:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    receipt = service.rotate_owner_credential(authenticated[0])
    return OwnerCredentialRotationResponse(
        device=DeviceResponse.from_device(receipt.device),
        access_token=receipt.access_token.get_secret_value(),
        expires_at=receipt.expires_at,
    )


@router.get("", response_model=DeviceListResponse)
async def list_devices(
    authenticated: Annotated[tuple[SecretStr, Device], Depends(authenticated_device)],
    service: Annotated[DeviceService, Depends(device_service)],
) -> DeviceListResponse:
    devices = service.list_devices(authenticated[0])
    return DeviceListResponse(devices=tuple(DeviceResponse.from_device(device) for device in devices))


@router.delete("/{device_id}", response_model=DeviceResponse)
async def revoke_device(
    device_id: DeviceId,
    authenticated: Annotated[tuple[SecretStr, Device], Depends(authenticated_device)],
    service: Annotated[DeviceService, Depends(device_service)],
) -> DeviceResponse:
    device = service.revoke_device(authenticated[0], device_id)
    return DeviceResponse.from_device(device)
