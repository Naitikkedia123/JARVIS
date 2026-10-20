"""Default-deny policy for device task submissions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from jarvis.devices.models import DeviceId
from jarvis.tasks.action import AuthorizedTaskAction
from jarvis.tasks.authorization import TaskSubmissionPolicyDecision, TaskSubmissionProposal
from jarvis.tasks.errors import TaskSubmissionPolicyUnavailableError
from jarvis.tools.models import AuthorizationContext

_SUPPORTED_TOOL = "jarvis.status"
_MAX_POLICY_BYTES = 16 * 1024


class TaskSubmissionGrantConfig(BaseModel):
    """Strict operator-managed allowlist; device claims are never consulted."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    allowed_device_ids: tuple[DeviceId, ...]

    @field_validator("allowed_device_ids", mode="before")
    @classmethod
    def ids_must_be_a_json_array(cls, value: object) -> object:
        if type(value) is not list:
            raise ValueError("allowed_device_ids must be an array.")
        return tuple(value)

    @model_validator(mode="after")
    def ids_must_be_unique(self) -> TaskSubmissionGrantConfig:
        if len(self.allowed_device_ids) != len(set(self.allowed_device_ids)):
            raise ValueError("allowed_device_ids must not contain duplicates.")
        return self


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key.")
        result[key] = value
    return result


def _reject_non_json_constant(value: str) -> object:
    raise ValueError(f"Unsupported JSON constant: {value}")


class LocalTaskSubmissionPolicy:
    """Read a bounded policy file on every decision and authorize narrowly.

    Missing, empty, unreadable, or invalid policy data is a service
    unavailability. A valid policy that does not list a device or action is an
    ordinary authorization denial. Neither roles nor reported capabilities
    participate in the decision.
    """

    def __init__(self, config_path: Path | None) -> None:
        self._config_path = config_path

    def authorize(
        self, *, device_id: DeviceId, proposal: TaskSubmissionProposal
    ) -> TaskSubmissionPolicyDecision:
        config = self._read_current_config()
        action = proposal.action
        if (
            config is None
            or device_id not in config.allowed_device_ids
            or not isinstance(action, AuthorizedTaskAction)
            or action.tool_name != _SUPPORTED_TOOL
            or action.arguments != {}
        ):
            return TaskSubmissionPolicyDecision(allowed=False)

        return TaskSubmissionPolicyDecision(
            allowed=True,
            authorization_context=AuthorizationContext(principal_id=device_id),
        )

    def _read_current_config(self) -> TaskSubmissionGrantConfig | None:
        if self._config_path is None:
            raise TaskSubmissionPolicyUnavailableError(
                "Task submission policy is unavailable."
            )

        try:
            with self._config_path.open("rb") as source:
                raw = source.read(_MAX_POLICY_BYTES + 1)
        except OSError:
            raise TaskSubmissionPolicyUnavailableError(
                "Task submission policy is unavailable."
            ) from None

        if not raw or len(raw) > _MAX_POLICY_BYTES:
            raise TaskSubmissionPolicyUnavailableError(
                "Task submission policy is unavailable."
            )
        try:
            payload = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_non_json_constant,
            )
            return TaskSubmissionGrantConfig.model_validate(payload)
        except Exception:
            # Policy contents and parser details must never escape this boundary.
            raise TaskSubmissionPolicyUnavailableError(
                "Task submission policy is unavailable."
            ) from None
