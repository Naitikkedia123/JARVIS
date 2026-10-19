"""Trusted device-scoped task authorization contracts.

Device identity authenticates a caller; an injected policy decision supplies
tool permissions. Device role and reported capabilities are intentionally not
inputs to this policy contract.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from jarvis.devices.models import Device, DeviceId
from jarvis.tasks.action import AuthorizedTaskAction
from jarvis.tasks.models import TaskPlan
from jarvis.tools.models import AuthorizationContext

IdempotencyKey = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9._~-]+$",
    ),
]


class TaskSubmissionProposal(BaseModel):
    """Validated untrusted task description plus a caller retry key."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    idempotency_key: IdempotencyKey
    title: str = Field(min_length=1, max_length=200, strict=True)
    description: str = Field(default="", max_length=4000, strict=True)
    action: AuthorizedTaskAction | None = None

    def request_digest(self) -> str:
        canonical = json.dumps(
            {
                "title": self.title,
                "description": self.description,
                "action_digest": None if self.action is None else self.action.digest(),
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TaskSubmissionPolicyDecision(BaseModel):
    """Explicit trusted policy outcome; denied decisions carry no grant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    authorization_context: AuthorizationContext | None = None


class TaskSubmissionPolicy(Protocol):
    """Trusted application policy, distinct from device metadata and requests."""

    def authorize(
        self, *, device_id: DeviceId, proposal: TaskSubmissionProposal
    ) -> TaskSubmissionPolicyDecision: ...


class TaskAuthorizationContext(BaseModel):
    """Ephemeral, policy-issued context bound to one device submission."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    device_id: DeviceId
    idempotency_key: IdempotencyKey
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    action_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_authorization: AuthorizationContext


class DeviceTaskDispatchDecision(BaseModel):
    """Explicit dispatch decision from a trusted server-side device policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    decision_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    authorization_context: AuthorizationContext | None = None


class DeviceTaskDispatchPolicy(Protocol):
    """Separate policy boundary for converting authenticated identity to a grant."""

    def authorize(
        self, *, device_id: DeviceId, action: AuthorizedTaskAction
    ) -> DeviceTaskDispatchDecision: ...


class DispatchAuthorization(BaseModel):
    """Ephemeral grant bound to the current device, action, and policy decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    device_id: DeviceId
    action_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_authorization: AuthorizationContext


def resolve_dispatch_authorization(
    device: Device,
    action: AuthorizedTaskAction,
    policy: DeviceTaskDispatchPolicy | None,
) -> DispatchAuthorization:
    """Resolve a narrow grant without consulting role or reported capabilities."""
    from jarvis.tasks.errors import TaskAuthorizationError, TaskDispatchDeniedError

    action_digest = action.digest()
    if device.revoked_at is not None:
        raise TaskAuthorizationError("Task execution is not authorized.")
    if policy is None or action.tool_name != "jarvis.status":
        raise TaskDispatchDeniedError(device_id=device.id, action_digest=action_digest)
    try:
        decision = policy.authorize(device_id=device.id, action=action)
        decision = DeviceTaskDispatchDecision.model_validate(decision)
    except Exception:
        raise TaskDispatchDeniedError(
            device_id=device.id, action_digest=action_digest
        ) from None
    context = decision.authorization_context
    expected_capabilities = frozenset({"system:status:read"})
    canonical = json.dumps(
        {
            "action_digest": action_digest,
            "decision_id": decision.decision_id,
            "device_id": device.id,
            "allowed": decision.allowed,
            "principal_id": None if context is None else context.principal_id,
            "granted_capabilities": (
                [] if context is None else sorted(context.granted_capabilities)
            ),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    fingerprint = hashlib.sha256(
        b"jarvis:device-task-dispatch-decision:v1\0" + canonical.encode("utf-8")
    ).hexdigest()
    if (
        not decision.allowed
        or context is None
        or context.principal_id != device.id
        or context.granted_capabilities != expected_capabilities
    ):
        raise TaskDispatchDeniedError(
            device_id=device.id,
            action_digest=action_digest,
            authorization_fingerprint=fingerprint,
        )
    return DispatchAuthorization(
        device_id=device.id,
        action_digest=action_digest,
        decision_id=decision.decision_id,
        fingerprint=fingerprint,
        tool_authorization=context,
    )


class TaskOwnership(BaseModel):
    """Durable owner and request identity for a device-submitted task."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    device_id: DeviceId
    idempotency_key: IdempotencyKey
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class TaskSubmissionReceipt(BaseModel):
    """Result of durable submission; a duplicate returns its original plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan: TaskPlan
    created: bool


def resolve_task_authorization(
    device: Device,
    proposal: TaskSubmissionProposal,
    policy: TaskSubmissionPolicy | None,
) -> TaskAuthorizationContext:
    """Resolve a policy-issued context or fail closed without exposing policy errors."""

    from jarvis.tasks.errors import (
        TaskAuthorizationError,
        TaskSubmissionPolicyUnavailableError,
    )

    if device.revoked_at is not None or policy is None or proposal.action is None:
        raise TaskAuthorizationError("Task submission is not authorized.")
    try:
        decision = policy.authorize(device_id=device.id, proposal=proposal)
        decision = TaskSubmissionPolicyDecision.model_validate(decision)
    except TaskSubmissionPolicyUnavailableError:
        raise
    except Exception:
        raise TaskAuthorizationError("Task submission is not authorized.") from None
    authorization = decision.authorization_context
    if (
        not decision.allowed
        or authorization is None
        or authorization.principal_id != device.id
    ):
        raise TaskAuthorizationError("Task submission is not authorized.")
    return TaskAuthorizationContext(
        device_id=device.id,
        idempotency_key=proposal.idempotency_key,
        request_digest=proposal.request_digest(),
        action_digest=proposal.action.digest(),
        tool_authorization=authorization,
    )
