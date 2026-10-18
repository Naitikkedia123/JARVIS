"""Validated, model-independent tool and authorization contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ToolName = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")]
CapabilityName = Annotated[
    str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.:-]*$")
]
MAX_TOOL_RESULT_BYTES = 1_048_576


class ToolAccess(StrEnum):
    READ = "read"
    WRITE = "write"


class ToolAuditOutcome(StrEnum):
    AUTHORIZATION_DENIED = "authorization_denied"
    AUTHORIZATION_GRANTED = "authorization_granted"
    EXECUTION_STARTED = "execution_started"
    EXECUTION_SUCCEEDED = "execution_succeeded"
    VALIDATION_FAILED = "validation_failed"
    EXECUTION_TIMED_OUT = "execution_timed_out"
    EXECUTION_FAILED = "execution_failed"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_FAILED = "approval_failed"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ToolDefinition(BaseModel):
    """Static contract registered by trusted application code."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: ToolName
    description: str = Field(min_length=1, max_length=500)
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    required_capabilities: frozenset[CapabilityName] = Field(default_factory=frozenset)
    access: ToolAccess
    timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    max_result_bytes: int = Field(default=65_536, ge=1, le=MAX_TOOL_RESULT_BYTES)
    audit_fields: frozenset[str] = Field(default_factory=frozenset)
    requires_approval: bool = False
    approval_summary: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def audit_fields_are_declared(self) -> ToolDefinition:
        available = set(self.input_schema.model_fields) | set(self.output_schema.model_fields)
        if not self.audit_fields <= available:
            raise ValueError("Audit fields must name declared input or output fields.")
        if self.requires_approval and self.approval_summary is None:
            raise ValueError("Approval-required tools must declare a safe approval summary.")
        return self


class ToolRequest(BaseModel):
    """Untrusted proposal; identity and permissions are deliberately absent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: ToolName
    arguments: dict[str, object] = Field(default_factory=dict)


class AuthorizationContext(BaseModel):
    """Trusted caller-supplied identity and capabilities, separate from a request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal_id: str = Field(min_length=1, max_length=128)
    granted_capabilities: frozenset[CapabilityName] = Field(default_factory=frozenset)


class AuthorizationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    reason: str = Field(min_length=1, max_length=64)
    missing_capabilities: frozenset[CapabilityName] = Field(default_factory=frozenset)


class ToolAuditEvent(BaseModel):
    """Safe structured dispatch audit record; deliberately excludes request data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    occurred_at: datetime = Field(default_factory=_utc_now)
    tool_name: ToolName
    principal_id: str | None = Field(default=None, min_length=1, max_length=128)
    access: ToolAccess
    outcome: ToolAuditOutcome
    reason_code: str = Field(min_length=1, max_length=64)
    duration_ms: int | None = Field(default=None, ge=0)

    @field_validator("occurred_at")
    @classmethod
    def timestamp_is_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Audit timestamp must include a timezone.")
        return value.astimezone(UTC)
