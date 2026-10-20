"""Tests for the narrow device-task tool allowlist."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel, ConfigDict

from jarvis.policy.allowlist import DeviceTaskToolPolicy
from jarvis.tools.dispatcher import InMemoryToolAuditSink, ToolDispatcher
from jarvis.tools.errors import AuthorizationDeniedError, ToolInputValidationError
from jarvis.tools.models import (
    AuthorizationContext,
    ToolAccess,
    ToolAuditOutcome,
    ToolDefinition,
    ToolRequest,
)
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.status import StatusInput, StatusOutput, register_status_tool


class ExtraInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


class ExtraOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: bool


def context(*capabilities: str) -> AuthorizationContext:
    return AuthorizationContext(
        principal_id="test-principal",
        granted_capabilities=frozenset(capabilities),
    )


def test_allowlist_requires_context_and_the_status_capability() -> None:
    registry = ToolRegistry()
    register_status_tool(registry)
    policy = DeviceTaskToolPolicy(registry)
    definition = registry.get("jarvis.status")

    assert policy.authorize(definition, None).allowed is False
    assert policy.authorize(definition, context()).allowed is False
    decision = policy.authorize(definition, context("system:status:read"))
    assert decision.allowed is True


def test_other_registered_tools_remain_denied_even_with_broad_permissions() -> None:
    registry = ToolRegistry()
    register_status_tool(registry)
    workspace = ToolDefinition(
        name="workspace.read_file",
        description="Read a workspace file.",
        input_schema=ExtraInput,
        output_schema=ExtraOutput,
        required_capabilities=frozenset({"workspace:read"}),
        access=ToolAccess.READ,
    )
    registry.register(workspace)
    policy = DeviceTaskToolPolicy(registry)

    decision = policy.authorize(
        workspace,
        context("workspace:read", "system:status:read", "tool:write"),
    )
    assert decision.allowed is False
    assert decision.reason == "tool_not_allowlisted"


def test_registered_tool_identity_and_exact_contract_are_enforced() -> None:
    registry = ToolRegistry()
    register_status_tool(registry)
    policy = DeviceTaskToolPolicy(registry)
    status = registry.get("jarvis.status")
    replacement = ToolDefinition(
        name=status.name,
        description=status.description,
        input_schema=ExtraInput,
        output_schema=StatusOutput,
        required_capabilities=status.required_capabilities,
        access=status.access,
    )

    unregistered = policy.authorize(replacement, context("system:status:read"))
    assert unregistered.allowed is False
    assert unregistered.reason == "unregistered_tool"

    wrong_registry = ToolRegistry()
    policy_bound_elsewhere = DeviceTaskToolPolicy(wrong_registry)
    mismatched_schema = ToolDefinition(
        name="jarvis.status",
        description="Status with an unsafe input contract.",
        input_schema=ExtraInput,
        output_schema=StatusOutput,
        required_capabilities=frozenset({"system:status:read"}),
        access=ToolAccess.READ,
    )
    wrong_registry.register(mismatched_schema)
    decision = policy_bound_elsewhere.authorize(
        mismatched_schema,
        context("system:status:read"),
    )
    assert decision.allowed is False
    assert decision.reason == "tool_contract_mismatch"
    assert StatusInput is not ExtraInput


def test_policy_must_be_bound_to_dispatchers_exact_registry() -> None:
    first = ToolRegistry()
    second = ToolRegistry()
    policy = DeviceTaskToolPolicy(first)
    policy.validate_registry(first)
    try:
        policy.validate_registry(second)
    except ValueError as error:
        assert "different tool registry" in str(error)
    else:
        raise AssertionError("A mismatched registry should fail closed.")


def test_dispatch_denies_unallowlisted_tool_before_executor() -> None:
    registry = ToolRegistry()
    register_status_tool(registry)
    definition = ToolDefinition(
        name="workspace.read_file",
        description="Read a workspace file.",
        input_schema=ExtraInput,
        output_schema=ExtraOutput,
        required_capabilities=frozenset({"workspace:read"}),
        access=ToolAccess.READ,
    )
    registry.register(definition)

    class Executor:
        calls = 0

        async def execute(self, arguments) -> object:
            del arguments
            self.calls += 1
            return {"accepted": True}

    executor = Executor()
    audit = InMemoryToolAuditSink()
    dispatcher = ToolDispatcher(
        registry,
        DeviceTaskToolPolicy(registry),
        {"workspace.read_file": executor},
        audit,
    )
    dispatcher.require_policy(DeviceTaskToolPolicy)
    with pytest.raises(AuthorizationDeniedError):
        asyncio.run(
            dispatcher.dispatch(
                ToolRequest(
                    tool_name="workspace.read_file",
                    arguments={"value": "anything"},
                ),
                authorization_context=context(
                    "workspace:read", "system:status:read", "tool:write"
                ),
            )
        )
    assert executor.calls == 0
    assert audit.events[-1].outcome is ToolAuditOutcome.AUTHORIZATION_DENIED


def test_status_argument_validation_happens_before_executor() -> None:
    registry = ToolRegistry()
    status_executor = register_status_tool(registry)

    class Executor:
        calls = 0

        async def execute(self, arguments) -> object:
            del arguments
            self.calls += 1
            return await status_executor.execute(ExtraInput(value="unused"))

    executor = Executor()
    dispatcher = ToolDispatcher(
        registry,
        DeviceTaskToolPolicy(registry),
        {"jarvis.status": executor},
        InMemoryToolAuditSink(),
    )
    with pytest.raises(ToolInputValidationError):
        asyncio.run(
            dispatcher.dispatch(
                ToolRequest(
                    tool_name="jarvis.status",
                    arguments={"unexpected": "value"},
                ),
                authorization_context=context("system:status:read"),
            )
        )
    assert executor.calls == 0

