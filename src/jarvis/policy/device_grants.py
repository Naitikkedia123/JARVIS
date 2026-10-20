"""Locally managed, default-deny device task grants."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from jarvis.devices.models import DeviceId
from jarvis.tasks.action import AuthorizedTaskAction
from jarvis.tasks.authorization import DeviceTaskDispatchDecision
from jarvis.tools.models import AuthorizationContext

_STATUS_TOOL_NAME = "jarvis.status"
_STATUS_CAPABILITY = "system:status:read"
_CONFIG_DOMAIN = b"jarvis:device-task-grants:v1\0"
_MAX_CONFIG_BYTES = 16 * 1024


class DeviceTaskGrantConfig(BaseModel):
    """Strict JSON configuration; IDs are local operator policy, not device claims."""

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
    def ids_must_be_unique(self) -> DeviceTaskGrantConfig:
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


def _revision(config: DeviceTaskGrantConfig) -> str:
    canonical = json.dumps(
        {
            "schema_version": config.schema_version,
            "allowed_device_ids": sorted(config.allowed_device_ids),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(_CONFIG_DOMAIN + canonical).hexdigest()


def _failure_revision(marker: bytes) -> str:
    return hashlib.sha256(_CONFIG_DOMAIN + marker).hexdigest()


class LocalDeviceTaskGrantPolicy:
    """Read the operator's current device-ID allowlist for every decision.

    A missing path, absent file, or any unreadable/invalid configuration denies
    access. No device IDs are built in. The policy is intentionally not wired
    into application startup by this module.
    """

    def __init__(self, config_path: Path | None) -> None:
        self._config_path = config_path

    def authorize(
        self, *, device_id: DeviceId, action: AuthorizedTaskAction
    ) -> DeviceTaskDispatchDecision:
        if action.tool_name != _STATUS_TOOL_NAME or action.arguments != {}:
            return self._deny(_failure_revision(b"unsupported-action-v1"))

        config, revision = self._read_current_config()
        if config is None or device_id not in config.allowed_device_ids:
            return self._deny(revision)

        return DeviceTaskDispatchDecision(
            allowed=True,
            decision_id=f"device-status-v1:{revision}",
            authorization_context=AuthorizationContext(
                principal_id=device_id,
                granted_capabilities=frozenset({_STATUS_CAPABILITY}),
            ),
        )

    def _read_current_config(self) -> tuple[DeviceTaskGrantConfig | None, str]:
        if self._config_path is None:
            return None, _failure_revision(b"missing-source-v1")

        try:
            with self._config_path.open("rb") as source:
                raw = source.read(_MAX_CONFIG_BYTES + 1)
        except FileNotFoundError:
            return None, _failure_revision(b"missing-file-v1")
        except OSError:
            return None, _failure_revision(b"unavailable-file-v1")

        if len(raw) > _MAX_CONFIG_BYTES:
            return None, _failure_revision(b"oversized-file-v1" + raw[:_MAX_CONFIG_BYTES])
        try:
            payload = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_non_json_constant,
            )
            config = DeviceTaskGrantConfig.model_validate(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
            return None, _failure_revision(b"invalid-file-v1" + raw)
        except Exception:
            # Pydantic validation and filesystem adapters can raise typed errors;
            # details are deliberately not exposed to callers.
            return None, _failure_revision(b"invalid-file-v1" + raw)
        return config, _revision(config)

    @staticmethod
    def _deny(revision: str) -> DeviceTaskDispatchDecision:
        return DeviceTaskDispatchDecision(
            allowed=False,
            decision_id=f"device-status-v1:{revision}",
            authorization_context=None,
        )
