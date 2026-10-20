"""Trusted service operations for one-time device pairing and bearer authentication."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from pydantic import SecretStr

from jarvis.devices.models import (
    CapabilityReport,
    Device,
    DevicePlatform,
    DeviceRole,
    OwnerCredentialReceipt,
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


def generate_bootstrap_code() -> SecretStr:
    """Generate a high-entropy bootstrap code for trusted out-of-band provisioning.

    The caller must securely deliver the returned secret to the intended owner and
    supply it to ``DeviceService``. The service stores only its digest.
    """
    return SecretStr(secrets.token_urlsafe(32))


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

    @contextmanager
    def reserve_authenticated_owner_credential(
        self, access_token: SecretStr
    ) -> Iterator[Device]:
        """Keep a validated owner credential stable through a TaskStore commit."""
        if not isinstance(access_token, SecretStr):
            raise DeviceAuthenticationError("Device authentication failed.")
        token = access_token.get_secret_value()
        if not token or len(token) > 256:
            raise DeviceAuthenticationError("Device authentication failed.")
        owner = self.authenticate(access_token)
        if owner.role is not DeviceRole.OWNER:
            raise DeviceAuthenticationError("Device authentication failed.")
        try:
            with self._store.reserve_owner_credential(
                owner_id=owner.id,
                expected_generation=owner.credential_generation,
                token_digest=_sha256(token),
            ) as reserved_owner:
                yield reserved_owner
        except DeviceNotFoundError as exc:
            raise DeviceAuthenticationError("Device authentication failed.") from exc

    @contextmanager
    def reserve_owner_generation(
        self, owner_id: str, expected_generation: int
    ) -> Iterator[Device]:
        """Serialize a task resume against rotation or host-local recovery."""
        try:
            with self._store.reserve_owner_credential(
                owner_id=owner_id,
                expected_generation=expected_generation,
            ) as owner:
                yield owner
        except DeviceNotFoundError as exc:
            raise DeviceAuthenticationError("Device authentication failed.") from exc

    @contextmanager
    def reserve_approval_resume(
        self,
        access_token: SecretStr,
        *,
        approval_owner_id: str,
        approval_owner_generation: int,
        task_owner_id: str,
    ) -> Iterator[tuple[Device, Device]]:
        """Reserve the approval owner and authenticated task owner together."""
        if not isinstance(access_token, SecretStr):
            raise DeviceAuthenticationError("Device authentication failed.")
        token = access_token.get_secret_value()
        if not token or len(token) > 256:
            raise DeviceAuthenticationError("Device authentication failed.")
        try:
            with self._store.reserve_approval_resume(
                approval_owner_id=approval_owner_id,
                approval_owner_generation=approval_owner_generation,
                task_owner_id=task_owner_id,
                task_owner_token_digest=_sha256(token),
            ) as devices:
                yield devices
        except DeviceNotFoundError as exc:
            raise DeviceAuthenticationError("Device authentication failed.") from exc

    @contextmanager
    def reserve_task_device(
        self,
        access_token: SecretStr,
        *,
        expected_device_id: str,
    ) -> Iterator[Device]:
        """Keep a task owner's current credential valid through a task commit."""
        if not isinstance(access_token, SecretStr):
            raise DeviceAuthenticationError("Device authentication failed.")
        token = access_token.get_secret_value()
        if not token or len(token) > 256:
            raise DeviceAuthenticationError("Device authentication failed.")
        try:
            with self._store.reserve_task_device(
                device_id=expected_device_id,
                token_digest=_sha256(token),
            ) as device:
                yield device
        except DeviceNotFoundError as exc:
            raise DeviceAuthenticationError("Device authentication failed.") from exc

    def validate_reserved_task_device(
        self,
        access_token: SecretStr,
        *,
        expected_device_id: str,
    ) -> Device:
        """Revalidate the presented task-owner credential inside its reservation."""
        if not isinstance(access_token, SecretStr):
            raise DeviceAuthenticationError("Device authentication failed.")
        token = access_token.get_secret_value()
        if not token or len(token) > 256:
            raise DeviceAuthenticationError("Device authentication failed.")
        try:
            return self._store.validate_reserved_task_device(
                device_id=expected_device_id,
                token_digest=_sha256(token),
            )
        except DeviceNotFoundError as exc:
            raise DeviceAuthenticationError("Device authentication failed.") from exc

    def validate_reserved_approval_resume(
        self,
        access_token: SecretStr,
        *,
        approval_owner_id: str,
        approval_owner_generation: int,
        task_owner_id: str,
    ) -> tuple[Device, Device]:
        """Revalidate both identities and the task-owner bearer under reservation."""
        if not isinstance(access_token, SecretStr):
            raise DeviceAuthenticationError("Device authentication failed.")
        token = access_token.get_secret_value()
        if not token or len(token) > 256:
            raise DeviceAuthenticationError("Device authentication failed.")
        try:
            approval_owner = self._store.validate_reserved_owner_credential(
                owner_id=approval_owner_id,
                expected_generation=approval_owner_generation,
            )
            task_owner = self._store.validate_reserved_task_device(
                device_id=task_owner_id,
                token_digest=_sha256(token),
            )
            return approval_owner, task_owner
        except DeviceNotFoundError as exc:
            raise DeviceAuthenticationError("Device authentication failed.") from exc

    def validate_reserved_owner_generation(
        self,
        owner_id: str,
        expected_generation: int,
        *,
        access_token: SecretStr | None = None,
    ) -> Device:
        """Recheck expiry/generation immediately before a TaskStore commit."""
        token_digest: str | None = None
        if access_token is not None:
            if not isinstance(access_token, SecretStr):
                raise DeviceAuthenticationError("Device authentication failed.")
            token = access_token.get_secret_value()
            if not token or len(token) > 256:
                raise DeviceAuthenticationError("Device authentication failed.")
            token_digest = _sha256(token)
        try:
            return self._store.validate_reserved_owner_credential(
                owner_id=owner_id,
                expected_generation=expected_generation,
                token_digest=token_digest,
            )
        except DeviceNotFoundError as exc:
            raise DeviceAuthenticationError("Device authentication failed.") from exc

    def get_device(self, device_id: str) -> Device:
        """Read current device lifecycle metadata for an already trusted binding."""
        try:
            return self._store.get_device(device_id)
        except DeviceNotFoundError:
            raise DeviceAuthenticationError("Device authentication failed.") from None

    def get_active_owner(self) -> Device:
        """Return the unique active owner identity and current credential generation."""
        owners = tuple(
            device
            for device in self._store.list_devices()
            if device.role is DeviceRole.OWNER and device.revoked_at is None
        )
        if len(owners) != 1:
            raise DeviceAuthenticationError("Device authentication failed.")
        return owners[0]

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

    def rotate_owner_credential(self, access_token: SecretStr) -> OwnerCredentialReceipt:
        """Replace the current owner's bearer only after fresh active-owner auth."""
        owner = self.authenticate(access_token)
        if owner.role is not DeviceRole.OWNER:
            raise DeviceAuthorizationError("Device is not authorized to rotate the owner credential.")
        replacement = secrets.token_urlsafe(32)
        try:
            expires_at = self._store.rotate_owner_credential(
                owner.id,
                _sha256(access_token.get_secret_value()),
                _sha256(replacement),
            )
        except DeviceNotFoundError as exc:
            # A concurrent rotation or expiry between authentication and the
            # transactional replacement invalidates this attempt.
            raise DeviceAuthenticationError("Device authentication failed.") from exc
        return OwnerCredentialReceipt(
            device=self._store.get_device(owner.id),
            access_token=SecretStr(replacement),
            expires_at=expires_at,
        )

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
