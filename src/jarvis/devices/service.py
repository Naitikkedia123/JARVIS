"""Trusted service operations for one-time device pairing and bearer authentication."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from pydantic import SecretStr

from jarvis.devices.models import (
    CapabilityReport,
    Device,
    DevicePlatform,
    DeviceRole,
    PairingMetadata,
    PairingReceipt,
)
from jarvis.devices.store import (
    DeviceNotFoundError,
    DevicePairingUnavailableError,
    DeviceStore,
)

PAIRING_CODE_TTL = timedelta(minutes=5)
MIN_BOOTSTRAP_CODE_LENGTH = 32


class DeviceAuthenticationError(RuntimeError):
    """Authentication failed without disclosing whether a device exists."""


class DeviceAuthorizationError(RuntimeError):
    """An active device lacks authority for the requested registry operation."""


class DeviceService:
    """Pairing/authentication logic; caller-provided capabilities never grant policy."""

    def __init__(
        self,
        store: DeviceStore,
        *,
        bootstrap_code: SecretStr | None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))
        if bootstrap_code is not None:
            raw = bootstrap_code.get_secret_value()
            if len(raw) < MIN_BOOTSTRAP_CODE_LENGTH:
                raise ValueError("Bootstrap pairing code must be at least 32 characters.")
            self._store.add_bootstrap_code(_sha256(raw))

    def pair(
        self,
        code: SecretStr,
        *,
        name: str,
        platform: DevicePlatform,
        reported_capabilities: frozenset[str] = frozenset(),
    ) -> PairingReceipt:
        metadata = PairingMetadata(
            name=name,
            platform=platform,
            reported_capabilities=reported_capabilities,
        )
        raw_code = code.get_secret_value()
        if not raw_code or len(raw_code) > 128:
            raise DevicePairingUnavailableError("Pairing code is invalid or unavailable.")
        code_digest = _sha256(raw_code)
        token = secrets.token_urlsafe(32)
        device = self._store.pair(
            code_digest=code_digest,
            token_digest=_sha256(token),
            name=metadata.name,
            platform=metadata.platform,
            capabilities=metadata.reported_capabilities,
        )
        return PairingReceipt(device=device, access_token=SecretStr(token))

    def authenticate(self, access_token: SecretStr) -> Device:
        token = access_token.get_secret_value()
        if not token or len(token) > 256:
            raise DeviceAuthenticationError("Device authentication failed.")
        try:
            return self._store.authenticate(_sha256(token))
        except DeviceNotFoundError as exc:
            raise DeviceAuthenticationError("Device authentication failed.") from exc

    def issue_pairing_code(self, owner_token: SecretStr) -> SecretStr:
        owner = self.authenticate(owner_token)
        if owner.role is not DeviceRole.OWNER:
            raise DeviceAuthorizationError("Device is not authorized to pair companions.")
        code = secrets.token_urlsafe(32)
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Clock must return a timezone-aware timestamp.")
        self._store.issue_pairing_code(
            owner.id,
            _sha256(code),
            now.astimezone(UTC) + PAIRING_CODE_TTL,
        )
        return SecretStr(code)

    def update_reported_capabilities(
        self, access_token: SecretStr, capabilities: frozenset[str]
    ) -> Device:
        device = self.authenticate(access_token)
        report = CapabilityReport(reported_capabilities=capabilities)
        return self._store.update_capabilities(device.id, report.reported_capabilities)

    def list_devices(self, owner_token: SecretStr) -> tuple[Device, ...]:
        owner = self.authenticate(owner_token)
        if owner.role is not DeviceRole.OWNER:
            raise DeviceAuthorizationError("Device is not authorized to manage the registry.")
        return self._store.list_devices()

    def revoke_device(self, owner_token: SecretStr, device_id: str) -> Device:
        owner = self.authenticate(owner_token)
        if owner.role is not DeviceRole.OWNER:
            raise DeviceAuthorizationError("Device is not authorized to manage the registry.")
        try:
            return self._store.revoke(device_id)
        except DevicePairingUnavailableError as exc:
            raise DeviceAuthorizationError("The owner device cannot be revoked here.") from exc


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
