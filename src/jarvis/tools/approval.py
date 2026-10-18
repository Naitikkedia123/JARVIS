"""In-memory approval requests; this module does not decide or execute approvals."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from jarvis.tools.errors import ApprovalRequestStoreError, UnknownApprovalRequestError
from jarvis.tools.models import ToolName


class ApprovalRequestState(StrEnum):
    PENDING = "pending"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ApprovalRequest(BaseModel):
    """Validated safe metadata for a request awaiting a future decision channel."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID = Field(default_factory=uuid4)
    tool_name: ToolName
    requesting_principal: str = Field(min_length=1, max_length=128)
    created_at: datetime = Field(default_factory=_utc_now)
    state: ApprovalRequestState = ApprovalRequestState.PENDING
    summary: str = Field(min_length=1, max_length=500)

    @field_validator("created_at")
    @classmethod
    def timestamp_is_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Approval request timestamp must include a timezone.")
        return value.astimezone(UTC)


class ApprovalRequestService(Protocol):
    """Small interface for recording and inspecting pending approval requests."""

    def create_pending_request(
        self, *, tool_name: str, principal_id: str, summary: str
    ) -> ApprovalRequest: ...

    def get_request(self, request_id: UUID | str) -> ApprovalRequest: ...


class InMemoryApprovalRequestService:
    """Non-durable approval request store; all requests disappear on process exit."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._clock = clock
        self._id_factory = id_factory
        self._requests: dict[UUID, ApprovalRequest] = {}

    def create_pending_request(
        self, *, tool_name: str, principal_id: str, summary: str
    ) -> ApprovalRequest:
        try:
            request = ApprovalRequest(
                request_id=self._id_factory(),
                tool_name=tool_name,
                requesting_principal=principal_id,
                created_at=self._clock(),
                state=ApprovalRequestState.PENDING,
                summary=summary,
            )
        except (ValidationError, TypeError, ValueError):
            raise ApprovalRequestStoreError() from None
        if request.request_id in self._requests:
            raise ApprovalRequestStoreError() from None
        self._requests[request.request_id] = request
        return request

    def get_request(self, request_id: UUID | str) -> ApprovalRequest:
        try:
            parsed_id = request_id if isinstance(request_id, UUID) else UUID(request_id)
        except (AttributeError, TypeError, ValueError):
            raise ApprovalRequestStoreError() from None
        try:
            return self._requests[parsed_id]
        except KeyError:
            raise UnknownApprovalRequestError() from None
