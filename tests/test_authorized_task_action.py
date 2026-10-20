"""Structural, immutability, and digest tests for typed task actions."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from jarvis.tasks.action import AuthorizedTaskAction
from jarvis.tools.models import ToolRequest


def test_nested_arguments_are_immutable_and_tool_request_is_a_detached_copy() -> None:
    action = AuthorizedTaskAction(
        tool_name="jarvis.status",
        arguments={"filter": {"tags": ["local", "safe"]}},
    )

    with pytest.raises(TypeError):
        action.arguments["filter"]["new"] = True  # type: ignore[index]
    with pytest.raises(AttributeError):
        action.arguments["filter"]["tags"].append("changed")  # type: ignore[union-attr]
    with pytest.raises(ValidationError):
        action.tool_name = "workspace.read_file"

    request = action.to_tool_request()
    assert isinstance(request, ToolRequest)
    request.arguments["filter"]["tags"].append("detached")
    assert action.arguments["filter"]["tags"] == ("local", "safe")


def test_action_digest_is_canonical_for_object_keys_and_sensitive_to_values() -> None:
    first = AuthorizedTaskAction(
        tool_name="jarvis.status",
        arguments={"b": 2, "a": {"x": 1}},
    )
    reordered = AuthorizedTaskAction(
        tool_name="jarvis.status",
        arguments={"a": {"x": 1}, "b": 2},
    )
    changed_array = AuthorizedTaskAction(
        tool_name="jarvis.status",
        arguments={"b": 2, "a": {"x": 1, "list": [1, 2]}},
    )

    assert first.digest() == reordered.digest()
    assert first.digest() != changed_array.digest()
    assert first.digest() != AuthorizedTaskAction(
        tool_name="jarvis.status", arguments={"b": 2.0, "a": {"x": 1}}
    ).digest()


@pytest.mark.parametrize(
    "tool_name",
    ["", "Jarvis.status", "workspace/read", "has space", "x" * 65],
)
def test_invalid_tool_names_are_rejected(tool_name: str) -> None:
    with pytest.raises(ValidationError):
        AuthorizedTaskAction(tool_name=tool_name, arguments={})


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        {1: "non-string key"},
        {"tuple": (1, 2)},
        {"nan": math.nan},
        {"infinity": math.inf},
        {"object": object()},
    ],
)
def test_arguments_must_be_finite_json_objects(arguments: object) -> None:
    with pytest.raises(ValidationError):
        AuthorizedTaskAction(tool_name="jarvis.status", arguments=arguments)


def test_circular_argument_structure_is_rejected() -> None:
    circular: dict[str, object] = {}
    circular["self"] = circular
    with pytest.raises((ValidationError, ValueError, RecursionError)):
        AuthorizedTaskAction(tool_name="jarvis.status", arguments=circular)

