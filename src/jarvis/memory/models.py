"""Validated records and access contexts for local, provenance-aware memory."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MemoryId = Annotated[str, Field(min_length=1, max_length=128, strict=True)]
ScopeName = Annotated[str, Field(min_length=1, max_length=128, strict=True)]


class MemoryRecordType(StrEnum):
    TASK_EPISODE = "task_episode"
    USER_PREFERENCE = "user_preference"
    PROJECT_FACT = "project_fact"
    ARTIFACT_REFERENCE = "artifact_reference"


class Provenance(StrEnum):
    USER_CONFIRMED = "user_confirmed"
    MODEL_INFERRED = "model_inferred"
    TOOL_OBSERVED = "tool_observed"
    LEGACY_UNKNOWN = "legacy_unknown"


class Sensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"
    RESTRICTED = "restricted"


class PermissionState(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    UNSPECIFIED = "unspecified"


class RecordState(StrEnum):
    CURRENT = "current"
    SUPERSEDED = "superseded"


class MemoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone.")
    return value.astimezone(UTC)


class MemoryRecord(MemoryModel):
    """A memory payload with explicit provenance and governance metadata."""

    id: MemoryId = Field(default_factory=lambda: uuid4().hex)
    record_type: MemoryRecordType
    logical_key: str | None = Field(default=None, min_length=1, max_length=256, strict=True)
    title: str = Field(min_length=1, max_length=500, strict=True)
    content: str = Field(min_length=1, max_length=100_000, strict=True)
    artifact_ref: str | None = Field(default=None, max_length=2048, strict=True)
    source: str = Field(min_length=1, max_length=512, strict=True)
    source_timestamp: datetime
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    confidence: float = Field(ge=0.0, le=1.0, strict=True)
    provenance: Provenance
    scope: ScopeName
    sensitivity: Sensitivity
    permission: PermissionState = PermissionState.UNSPECIFIED
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    expires_at: datetime | None = None
    version: int = Field(default=1, ge=1)
    supersedes_id: MemoryId | None = None
    state: RecordState = RecordState.CURRENT

    @field_validator("source_timestamp", "created_at", "updated_at", "valid_from", "valid_until", "expires_at")
    @classmethod
    def timestamps_are_aware_utc(cls, value: datetime | None) -> datetime | None:
        return _aware_utc(value) if value is not None else None

    @model_validator(mode="after")
    def validate_time_ranges(self) -> MemoryRecord:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at.")
        if self.valid_from and self.valid_until and self.valid_from >= self.valid_until:
            raise ValueError("valid_from must precede valid_until.")
        if self.expires_at and self.valid_from and self.expires_at <= self.valid_from:
            raise ValueError("expires_at must follow valid_from.")
        return self


class RetrievalContext(MemoryModel):
    """Caller permissions used by every content retrieval operation."""

    scopes: frozenset[ScopeName] = frozenset()
    max_sensitivity: Sensitivity = Sensitivity.PUBLIC
    include_inferred: bool = False


class ManagementContext(MemoryModel):
    """Separate authority to create, correct, and delete records."""

    scopes: frozenset[ScopeName] = frozenset()
    max_sensitivity: Sensitivity = Sensitivity.PUBLIC


class MemoryFilters(MemoryModel):
    """Optional current-record metadata filters."""

    record_type: MemoryRecordType | None = None
    logical_key: str | None = Field(default=None, min_length=1, max_length=256, strict=True)
    provenance: Provenance | None = None


SENSITIVITY_RANK: dict[Sensitivity, int] = {
    Sensitivity.PUBLIC: 0,
    Sensitivity.INTERNAL: 1,
    Sensitivity.SENSITIVE: 2,
    Sensitivity.RESTRICTED: 3,
}
