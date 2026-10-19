"""Immutable structured actions proposed for later authorized task execution.

This model checks tool-name syntax and JSON structure only. It does not authorize
the tool or validate arguments against a registered tool's input schema; those
checks remain at the trusted policy and ``ToolDispatcher`` boundaries.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator

from jarvis.tools.models import ToolName, ToolRequest

_ACTION_DIGEST_DOMAIN = b"jarvis:authorized-task-action:v1\0"


def _validate_json(value: object, active_containers: set[int] | None = None) -> None:
    """Accept only JSON's exact primitive, list, and string-keyed object types."""
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite.")
        return
    if type(value) not in (list, dict):
        raise ValueError("Arguments must contain only JSON-compatible values.")

    if active_containers is None:
        active_containers = set()
    identity = id(value)
    if identity in active_containers:
        raise ValueError("Arguments cannot contain circular references.")
    active_containers.add(identity)
    try:
        if type(value) is list:
            for item in value:
                _validate_json(item, active_containers)
        else:
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError("JSON object keys must be strings.")
                _validate_json(item, active_containers)
    finally:
        active_containers.remove(identity)


def _freeze_json(value: object) -> Any:
    if type(value) is dict:
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_json(item) for item in value]
    return value


class AuthorizedTaskAction(BaseModel):
    """Typed tool proposal with deeply immutable, structurally valid JSON args."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: ToolName
    arguments: dict[str, object]

    @field_validator("arguments", mode="before")
    @classmethod
    def arguments_are_json_objects(cls, value: object) -> object:
        if type(value) is not dict:
            raise ValueError("Arguments must be a JSON object.")
        _validate_json(value)
        return value

    @field_validator("arguments", mode="after")
    @classmethod
    def arguments_are_deeply_immutable(cls, value: dict[str, object]) -> dict[str, object]:
        return _freeze_json(value)

    @field_serializer("arguments")
    def serialize_arguments(self, value: dict[str, object]) -> dict[str, object]:
        return _thaw_json(value)

    @classmethod
    def from_tool_request(cls, request: ToolRequest) -> AuthorizedTaskAction:
        """Copy a request into the immutable action shape without authorizing it."""
        return cls(tool_name=request.tool_name, arguments=request.arguments)

    def to_tool_request(self) -> ToolRequest:
        """Create a fresh dispatcher request; dispatcher policy/schema checks still apply."""
        return ToolRequest(
            tool_name=self.tool_name,
            arguments=_thaw_json(self.arguments),
        )

    def digest(self) -> str:
        """Return a stable SHA-256 digest of this versioned action contract.

        The canonical JSON representation sorts object keys, preserves the
        complete normalized JSON argument value, and rejects non-finite
        numbers. The domain prefix prevents this digest from being confused
        with an unrelated SHA-256 value.
        """
        canonical = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(_ACTION_DIGEST_DOMAIN + canonical).hexdigest()
