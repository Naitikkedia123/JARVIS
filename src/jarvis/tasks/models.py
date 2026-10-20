"""Validated domain models for goals, tasks, steps, and durable records."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jarvis.devices.models import DeviceId
from jarvis.tasks.action import AuthorizedTaskAction

OpaqueId = Annotated[str, Field(min_length=1, max_length=128)]


class GoalState(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TaskState(StrEnum):
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_APPROVAL = "waiting_approval"
    CANCELLING = "cancelling"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    PAUSED = "paused"
    WAITING_APPROVAL = "waiting_approval"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ReplaySafety(StrEnum):
    PURE = "pure"
    IDEMPOTENT = "idempotent"
    NON_IDEMPOTENT = "non_idempotent"


class AttemptOutcome(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    RETRYABLE_FAILURE = "retryable_failure"
    PERMANENT_FAILURE = "permanent_failure"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ReconciliationOutcome(StrEnum):
    CONFIRMED_SUCCEEDED = "confirmed_succeeded"
    CONFIRMED_NOT_APPLIED = "confirmed_not_applied"
    CONFIRMED_FAILED = "confirmed_failed"


class SafeErrorCode(StrEnum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    OUTCOME_UNKNOWN = "outcome_unknown"


class TaskEventType(StrEnum):
    GOAL_CREATED = "goal_created"
    TASK_CREATED = "task_created"
    STEP_CREATED = "step_created"
    STATE_CHANGED = "state_changed"
    ATTEMPT_STARTED = "attempt_started"
    ATTEMPT_FINISHED = "attempt_finished"
    RECONCILED = "reconciled"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    REVOKED = "revoked"
    CONSUMED = "consumed"


class ApprovalAuditEventType(StrEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    REVOKED = "revoked"
    CONSUMED = "consumed"
    DISPATCH_LINKED = "dispatch_linked"


class EventMetadata(BaseModel):
    """Allowlisted audit fields; arbitrary text and exception messages are excluded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_number: int | None = Field(default=None, ge=1)
    replay_safety: ReplaySafety | None = None
    outcome: AttemptOutcome | ReconciliationOutcome | None = None
    error_code: SafeErrorCode | None = None
    confirmation_id: UUID | None = None
    attempt_id: OpaqueId | None = None
    action_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    device_id: DeviceId | None = None
    authorization_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ApprovalRequestRecord(BaseModel):
    """Secret-free durable binding for one exact task action awaiting approval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: UUID = Field(default_factory=uuid4)
    task_id: OpaqueId
    step_id: OpaqueId
    requesting_device_id: DeviceId
    owner_device_id: DeviceId
    owner_credential_generation: int = Field(ge=1)
    action_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_definition_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime
    decided_at: datetime | None = None
    decided_by_device_id: DeviceId | None = None
    expired_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_by_device_id: DeviceId | None = None
    consumed_at: datetime | None = None
    consumed_by_device_id: DeviceId | None = None

    @field_validator(
        "created_at", "expires_at", "decided_at", "expired_at", "revoked_at", "consumed_at"
    )
    @classmethod
    def timestamps_are_aware_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_aware_utc(value)

    @model_validator(mode="after")
    def lifecycle_fields_match_status(self) -> ApprovalRequestRecord:
        if self.expires_at <= self.created_at:
            raise ValueError("Approval expiry must follow creation.")
        if (self.decided_at is None) != (self.decided_by_device_id is None):
            raise ValueError("Approval decision timestamp and actor must be paired.")
        if (self.revoked_at is None) != (self.revoked_by_device_id is None):
            raise ValueError("Approval revocation timestamp and actor must be paired.")
        if (self.consumed_at is None) != (self.consumed_by_device_id is None):
            raise ValueError("Approval consumption timestamp and actor must be paired.")
        if self.status is ApprovalStatus.PENDING and any(
            value is not None
            for value in (self.decided_at, self.expired_at, self.revoked_at, self.consumed_at)
        ):
            raise ValueError("Pending approval cannot have lifecycle timestamps.")
        if self.status in (ApprovalStatus.APPROVED, ApprovalStatus.DENIED) and (
            self.decided_at is None
        ):
            raise ValueError("Decided approval requires decision provenance.")
        if self.status is ApprovalStatus.EXPIRED and self.expired_at is None:
            raise ValueError("Expired approval requires an expiry event timestamp.")
        if self.status is ApprovalStatus.REVOKED and self.revoked_at is None:
            raise ValueError("Revoked approval requires revocation provenance.")
        if self.status is ApprovalStatus.CONSUMED and (
            self.decided_at is None or self.consumed_at is None
        ):
            raise ValueError("Consumed approval must first have been approved.")
        return self


class ApprovalAuditEvent(BaseModel):
    """Append-only, allowlisted approval provenance without action or token data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID = Field(default_factory=uuid4)
    request_id: UUID
    task_id: OpaqueId
    step_id: OpaqueId
    event_type: ApprovalAuditEventType
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    actor_device_id: DeviceId | None = None
    owner_credential_generation: int = Field(ge=1)
    action_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_definition_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_id: OpaqueId | None = None
    reason_code: str | None = Field(default=None, pattern=r"^[a-z0-9_.:-]{1,64}$")

    @field_validator("occurred_at")
    @classmethod
    def timestamp_is_aware_utc(cls, value: datetime) -> datetime:
        return _require_aware_utc(value)


class AttemptAuthorization(BaseModel):
    """Authorization facts supplied to the store before an owned dispatch claim."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    device_id: DeviceId
    action_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class AttemptDenial(BaseModel):
    """Authenticated, pre-dispatch denial recorded without creating a grant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    device_id: DeviceId
    action_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization_fingerprint: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )


class AttemptAuthorizationBinding(AttemptAuthorization):
    """Durable per-attempt binding; generated transactionally by TaskStore."""

    task_id: OpaqueId
    step_id: OpaqueId
    attempt_id: OpaqueId
    attempt_number: int = Field(ge=1, le=10)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _require_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone.")
    return value.astimezone(UTC)


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Goal(DomainModel):
    id: OpaqueId = Field(default_factory=lambda: uuid4().hex)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    state: GoalState = GoalState.ACTIVE
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamps_are_aware_utc(cls, value: datetime) -> datetime:
        return _require_aware_utc(value)


class Task(DomainModel):
    id: OpaqueId = Field(default_factory=lambda: uuid4().hex)
    goal_id: OpaqueId
    title: str = Field(min_length=1, max_length=200)
    state: TaskState = TaskState.READY
    planner_version: str = Field(default="deterministic-v1", min_length=1, max_length=64)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamps_are_aware_utc(cls, value: datetime) -> datetime:
        return _require_aware_utc(value)


class Step(DomainModel):
    id: OpaqueId = Field(default_factory=lambda: uuid4().hex)
    task_id: OpaqueId
    title: str = Field(min_length=1, max_length=200)
    action: AuthorizedTaskAction | None = None
    dependencies: list[OpaqueId] = Field(default_factory=list, max_length=100)
    state: StepState = StepState.PENDING
    replay_safety: ReplaySafety = ReplaySafety.NON_IDEMPOTENT
    idempotency_key: OpaqueId | None = None
    attempt_limit: int = Field(default=3, ge=1, le=10)
    attempt_count: int = Field(default=0, ge=0, le=10)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamps_are_aware_utc(cls, value: datetime) -> datetime:
        return _require_aware_utc(value)

    @model_validator(mode="after")
    def validate_step_invariants(self) -> Step:
        if self.attempt_count > self.attempt_limit:
            raise ValueError("Attempt count cannot exceed the attempt limit.")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("Step dependencies must be unique.")
        if self.id in self.dependencies:
            raise ValueError("A step cannot depend on itself.")
        if self.replay_safety is ReplaySafety.IDEMPOTENT and not self.idempotency_key:
            raise ValueError("Idempotent steps require a stable idempotency key.")
        if self.state is StepState.RUNNING and self.attempt_count == 0:
            raise ValueError("A running step must have a persisted attempt.")
        if self.state is StepState.RECONCILIATION_REQUIRED and self.attempt_count == 0:
            raise ValueError("A step requiring reconciliation must have an attempt.")
        return self


class TaskPlan(DomainModel):
    task: Task
    steps: list[Step] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_plan(self) -> TaskPlan:
        step_ids: set[str] = set()
        by_id: dict[str, Step] = {}
        for step in self.steps:
            if step.task_id != self.task.id:
                raise ValueError("Every plan step must belong to its task.")
            if step.id in step_ids:
                raise ValueError("Step identifiers in a plan must be unique.")
            step_ids.add(step.id)
            by_id[step.id] = step

        for step in self.steps:
            for dependency_id in step.dependencies:
                if dependency_id not in by_id:
                    raise ValueError("A dependency must reference a step in this task plan.")

        outgoing: dict[str, list[str]] = defaultdict(list)
        indegree = {step_id: 0 for step_id in step_ids}
        for step in self.steps:
            for dependency_id in step.dependencies:
                outgoing[dependency_id].append(step.id)
                indegree[step.id] += 1

        ready: deque[str] = deque(step_id for step_id, degree in indegree.items() if degree == 0)
        visited = 0
        while ready:
            step_id = ready.popleft()
            visited += 1
            for dependent_id in outgoing[step_id]:
                indegree[dependent_id] -= 1
                if indegree[dependent_id] == 0:
                    ready.append(dependent_id)
        if visited != len(step_ids):
            raise ValueError("Task plan dependencies must not contain a cycle.")
        return self


class StepAttempt(DomainModel):
    id: OpaqueId
    step_id: OpaqueId
    attempt_number: int = Field(ge=1, le=10)
    outcome: AttemptOutcome = AttemptOutcome.RUNNING
    replay_safety: ReplaySafety
    idempotency_key: OpaqueId | None = None
    authorization_binding: AttemptAuthorizationBinding | None = None
    dispatch_claimed_at: datetime | None = None
    error_code: SafeErrorCode | None = None
    started_at: datetime
    finished_at: datetime | None = None

    @field_validator("started_at", "finished_at", "dispatch_claimed_at")
    @classmethod
    def timestamps_are_aware_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_aware_utc(value)

    @model_validator(mode="after")
    def validate_attempt_completion(self) -> StepAttempt:
        if (self.outcome is AttemptOutcome.RUNNING) != (self.finished_at is None):
            raise ValueError("Only running attempts may omit a finish timestamp.")
        if self.replay_safety is ReplaySafety.IDEMPOTENT and not self.idempotency_key:
            raise ValueError("Idempotent attempts require a stable idempotency key.")
        return self


class TaskCheckpoint(DomainModel):
    id: OpaqueId
    task_id: OpaqueId
    sequence: int = Field(ge=1)
    step_id: OpaqueId
    completed_step_ids: list[OpaqueId] = Field(max_length=500)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def timestamp_is_aware_utc(cls, value: datetime) -> datetime:
        return _require_aware_utc(value)


class StepResult(DomainModel):
    """Validated JSON result returned by an in-process step runner."""

    data: dict[str, object]

    @field_validator("data")
    @classmethod
    def data_is_deterministic_json(cls, value: dict[str, object]) -> dict[str, object]:
        try:
            json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
        except (TypeError, ValueError, RecursionError):
            raise ValueError("Step results must contain only finite JSON values.") from None
        return value


class TaskEvent(DomainModel):
    id: int = Field(ge=1)
    event_type: TaskEventType
    goal_id: OpaqueId
    task_id: OpaqueId | None = None
    step_id: OpaqueId | None = None
    from_state: str | None = Field(default=None, max_length=40)
    to_state: str | None = Field(default=None, max_length=40)
    occurred_at: datetime
    metadata: EventMetadata = Field(default_factory=EventMetadata)

    @field_validator("occurred_at")
    @classmethod
    def timestamp_is_aware_utc(cls, value: datetime) -> datetime:
        return _require_aware_utc(value)
