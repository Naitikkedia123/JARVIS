"""Typed records for paired devices and their self-reported capabilities."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

DeviceId = Annotated[str, StringConstraints(strict=True, min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")]
CapabilityName = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9._:-]*$",
    ),
]


class DeviceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DevicePlatform(StrEnum):
    ANDROID = "android"
    WINDOWS = "windows"
    WEB = "web"
    OTHER = "other"


class DeviceRole(StrEnum):
    OWNER = "owner"
    COMPANION = "companion"


class CapabilityReport(DeviceModel):
    """Untrusted descriptive capability claims from a device."""

    reported_capabilities: frozenset[CapabilityName] = Field(max_length=32)


class PairingMetadata(DeviceModel):
    """Validated self-reported registration fields for one device."""

    name: str = Field(min_length=1, max_length=80, strict=True)
    platform: DevicePlatform
    reported_capabilities: frozenset[CapabilityName] = Field(default_factory=frozenset, max_length=32)

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            if any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError("Device name contains unsupported control characters.")
        return value


class Device(DeviceModel):
    """A paired device; reported capabilities are descriptive, never grants."""

    id: DeviceId
    name: str = Field(min_length=1, max_length=80, strict=True)
    platform: DevicePlatform
    role: DeviceRole
    reported_capabilities: frozenset[CapabilityName] = Field(max_length=32)
    paired_at: datetime
    last_seen_at: datetime
    revoked_at: datetime | None = None
    credential_generation: int = Field(default=0, ge=0, strict=True)

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            if any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError("Device name contains unsupported control characters.")
        return value

    @field_validator("paired_at", "last_seen_at", "revoked_at")
    @classmethod
    def timestamps_are_aware_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Device timestamps must include a timezone.")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_device_lifecycle(self) -> Device:
        if self.last_seen_at < self.paired_at:
            raise ValueError("last_seen_at cannot precede paired_at.")
        if self.revoked_at is not None and self.revoked_at < self.paired_at:
            raise ValueError("revoked_at cannot precede paired_at.")
        return self


class PairingReceipt(DeviceModel):
    """Pairing response. The access token is shown only in this receipt."""

    device: Device
    access_token: SecretStr


class OwnerCredentialReceipt(DeviceModel):
    """One-time owner credential replacement and its finite expiry."""

    device: Device
    access_token: SecretStr
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def expiry_is_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Owner credential expiry must include a timezone.")
        return value.astimezone(UTC)


class DeviceCredentialAuditEvent(DeviceModel):
    """Credential lifecycle event without bearer tokens or credential digests."""

    event_id: Annotated[str, StringConstraints(strict=True, min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")]
    occurred_at: datetime
    event_type: Literal["owner_credential_rotated", "owner_credential_recovered"]
    device_id: DeviceId
    actor: Literal["authenticated_owner", "host_local_operator"]
    origin: Literal["owner_rotation_api", "host_local_recovery_cli"]
    outcome: Literal["success"]

    @field_validator("occurred_at")
    @classmethod
    def timestamp_is_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Credential audit timestamp must include a timezone.")
        return value.astimezone(UTC)
