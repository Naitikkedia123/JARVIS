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

    def request_digest(self) -> str:
        canonical = json.dumps(
            {"title": self.title, "description": self.description},
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
    tool_authorization: AuthorizationContext


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

    from jarvis.tasks.errors import TaskAuthorizationError

    if device.revoked_at is not None or policy is None:
        raise TaskAuthorizationError("Task submission is not authorized.")
    try:
        decision = policy.authorize(device_id=device.id, proposal=proposal)
        decision = TaskSubmissionPolicyDecision.model_validate(decision)
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
        tool_authorization=authorization,
    )
