"""Deterministic tests for the typed tool registry and authorization boundary."""

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jarvis.tools import (
    MAX_TOOL_RESULT_BYTES,
    WRITE_CAPABILITY,
    AuthorizationContext,
    AuthorizationDeniedError,
    DuplicateToolError,
    InMemoryToolAuditSink,
    SealedToolRegistryError,
    ToolAccess,
    ToolAuditError,
    ToolAuditOutcome,
    ToolDefinition,
    ToolDispatcher,
    ToolExecutionError,
    ToolInputValidationError,
    ToolOutputValidationError,
    ToolPolicy,
    ToolRegistry,
    ToolRequest,
    ToolResultTooLargeError,
    ToolTimeoutError,
    UnknownToolError,
)


class EchoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=32)


class EchoOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: str


def definition(**overrides: object) -> ToolDefinition:
    values: dict[str, object] = {
        "name": "echo",
        "description": "Return a deterministic value.",
        "input_schema": EchoInput,
        "output_schema": EchoOutput,
        "required_capabilities": frozenset({"demo.read"}),
        "access": ToolAccess.READ,
        "audit_fields": frozenset({"text", "result"}),
    }
    values.update(overrides)
    return ToolDefinition(**values)


class MockExecutor:
    def __init__(
        self,
        result: object = EchoOutput(result="ok"),
        *,
        action: Callable[[BaseModel], Awaitable[object]] | None = None,
    ) -> None:
        self.result = result
        self.action = action
        self.calls: list[BaseModel] = []

    async def execute(self, arguments: BaseModel) -> object:
        self.calls.append(arguments)
        if self.action is not None:
            return await self.action(arguments)
        return self.result


def dispatcher(
    executor: MockExecutor | None = None,
    *,
    tool_definition: ToolDefinition | None = None,
) -> tuple[ToolDispatcher, MockExecutor, InMemoryToolAuditSink]:
    registered = tool_definition or definition()
    registry = ToolRegistry()
    registry.register(registered)
    mock = executor or MockExecutor()
    sink = InMemoryToolAuditSink()
    return ToolDispatcher(registry, ToolPolicy(), {registered.name: mock}, sink), mock, sink


def test_registry_registers_definitions_and_rejects_duplicates_and_unknown_names() -> None:
    registry = ToolRegistry()
    registered = definition()
    registry.register(registered)

    assert registry.get("echo") is registered
    with pytest.raises(DuplicateToolError, match="already registered"):
        registry.register(registered)
    with pytest.raises(UnknownToolError, match="not registered"):
        registry.get("missing")


def test_dispatcher_seals_registry_and_lookup_remains_available() -> None:
    registry = ToolRegistry()
    registered = definition()
    registry.register(registered)
    assert not registry.is_sealed
    assert registry.get("echo") is registered

    sink = InMemoryToolAuditSink()
    executor = MockExecutor()
    dispatcher = ToolDispatcher(registry, ToolPolicy(), {"echo": executor}, sink)

    assert registry.is_sealed
    registry.seal()
    assert registry.get("echo") is registered
    with pytest.raises(SealedToolRegistryError, match="sealed"):
        registry.register(definition(name="other"))
    with pytest.raises(SealedToolRegistryError, match="sealed"):
        registry.register(definition())

    context = AuthorizationContext(
        principal_id="reader", granted_capabilities=frozenset({"demo.read"})
    )
    result = asyncio.run(
        dispatcher.dispatch(
            ToolRequest(tool_name="echo", arguments={"text": "still-readable"}),
            authorization_context=context,
        )
    )
    assert result.output == EchoOutput(result="ok")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout_seconds", 0),
        ("timeout_seconds", 60.1),
        ("max_result_bytes", 0),
        ("max_result_bytes", MAX_TOOL_RESULT_BYTES + 1),
        ("audit_fields", frozenset({"undeclared"})),
    ],
)
def test_tool_definition_validates_timeout_result_size_and_audit_fields(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        definition(**{field: value})


def test_tool_definition_accepts_project_result_size_ceiling() -> None:
    assert definition(max_result_bytes=MAX_TOOL_RESULT_BYTES).max_result_bytes == MAX_TOOL_RESULT_BYTES


def test_policy_allows_only_explicitly_granted_capabilities() -> None:
    policy = ToolPolicy()
    registered = definition()
    context = AuthorizationContext(
        principal_id="local-user", granted_capabilities=frozenset({"demo.read"})
    )

    decision = policy.authorize(registered, context)

    assert decision.allowed
    assert decision.reason == "granted"


@pytest.mark.parametrize(
    ("context", "reason"),
    [
        (None, "missing_context"),
        (
            AuthorizationContext(principal_id="local-user", granted_capabilities=frozenset()),
            "missing_capabilities",
        ),
    ],
)
def test_dispatch_denies_missing_context_or_capability_before_executor(
    context: AuthorizationContext | None, reason: str
) -> None:
    registered = definition()
    decision = ToolPolicy().authorize(registered, context)
    assert not decision.allowed
    assert decision.reason == reason

    tool_dispatcher, executor, sink = dispatcher()
    with pytest.raises(AuthorizationDeniedError):
        asyncio.run(
            tool_dispatcher.dispatch(
                ToolRequest(tool_name="echo", arguments={"text": "proposed"}),
                authorization_context=context,
            )
        )
    assert executor.calls == []
    event = sink.events[-1]
    assert event.outcome is ToolAuditOutcome.AUTHORIZATION_DENIED
    assert event.reason_code == reason
    assert event.principal_id == (None if context is None else context.principal_id)
    assert event.access is ToolAccess.READ
    assert event.occurred_at.utcoffset().total_seconds() == 0


def test_model_proposed_unauthorized_call_is_blocked_before_execution() -> None:
    tool_dispatcher, executor, sink = dispatcher()
    request = ToolRequest(tool_name="echo", arguments={"text": "proposed"})

    with pytest.raises(AuthorizationDeniedError):
        asyncio.run(
            tool_dispatcher.dispatch(
                request,
                authorization_context=AuthorizationContext(
                    principal_id="model", granted_capabilities=frozenset()
                ),
            )
        )
    assert executor.calls == []
    assert sink.events[-1].outcome is ToolAuditOutcome.AUTHORIZATION_DENIED


def test_request_cannot_self_grant_identity_or_capabilities() -> None:
    with pytest.raises(ValidationError):
        ToolRequest.model_validate(
            {
                "tool_name": "echo",
                "arguments": {"text": "proposed"},
                "principal_id": "administrator",
                "granted_capabilities": ["demo.read"],
            }
        )


def test_dispatch_validates_input_and_output_and_returns_typed_output() -> None:
    context = AuthorizationContext(
        principal_id="local-user", granted_capabilities=frozenset({"demo.read"})
    )
    tool_dispatcher, executor, sink = dispatcher()
    result = asyncio.run(
        tool_dispatcher.dispatch(
            ToolRequest(tool_name="echo", arguments={"text": "hello"}),
            authorization_context=context,
        )
    )
    assert result.principal_id == "local-user"
    assert result.output == EchoOutput(result="ok")
    assert len(executor.calls) == 1
    assert [event.outcome for event in sink.events] == [
        ToolAuditOutcome.AUTHORIZATION_GRANTED,
        ToolAuditOutcome.EXECUTION_STARTED,
        ToolAuditOutcome.EXECUTION_SUCCEEDED,
    ]
    assert sink.events[-1].duration_ms is not None
    assert all("hello" not in event.model_dump_json() for event in sink.events)

    with pytest.raises(ToolInputValidationError):
        asyncio.run(
            tool_dispatcher.dispatch(
                ToolRequest(tool_name="echo", arguments={"text": ""}),
                authorization_context=context,
            )
        )
    assert sink.events[-1].outcome is ToolAuditOutcome.VALIDATION_FAILED
    assert sink.events[-1].reason_code == "input_validation_failed"

    bad_output_dispatcher, _, bad_output_sink = dispatcher(
        executor=MockExecutor({"not_result": 1})
    )
    with pytest.raises(ToolOutputValidationError):
        asyncio.run(
            bad_output_dispatcher.dispatch(
                ToolRequest(tool_name="echo", arguments={"text": "hello"}),
                authorization_context=context,
            )
        )
    assert bad_output_sink.events[-1].outcome is ToolAuditOutcome.VALIDATION_FAILED
    assert bad_output_sink.events[-1].reason_code == "output_validation_failed"


def test_dispatch_enforces_result_size_timeout_and_safe_executor_errors() -> None:
    context = AuthorizationContext(
        principal_id="local-user", granted_capabilities=frozenset({"demo.read"})
    )
    too_small = definition(max_result_bytes=1)
    size_dispatcher, _, size_sink = dispatcher(tool_definition=too_small)
    with pytest.raises(ToolResultTooLargeError):
        asyncio.run(
            size_dispatcher.dispatch(
                ToolRequest(tool_name="echo", arguments={"text": "hello"}),
                authorization_context=context,
            )
        )
    assert size_sink.events[-1].reason_code == "result_size_exceeded"

    async def wait_forever(_: BaseModel) -> object:
        await asyncio.Event().wait()
        return EchoOutput(result="unreachable")

    timeout_dispatcher, _, timeout_sink = dispatcher(
        executor=MockExecutor(action=wait_forever),
        tool_definition=definition(timeout_seconds=0.001),
    )
    with pytest.raises(ToolTimeoutError):
        asyncio.run(
            timeout_dispatcher.dispatch(
                ToolRequest(tool_name="echo", arguments={"text": "hello"}),
                authorization_context=context,
            )
        )
    assert timeout_sink.events[-1].outcome is ToolAuditOutcome.EXECUTION_TIMED_OUT
    assert timeout_sink.events[-1].duration_ms is not None

    class BrokenExecutor:
        async def execute(self, arguments: BaseModel) -> object:
            del arguments
            raise RuntimeError("private executor detail")

    registry = ToolRegistry()
    registry.register(definition())
    broken_sink = InMemoryToolAuditSink()
    broken_dispatcher = ToolDispatcher(
        registry, ToolPolicy(), {"echo": BrokenExecutor()}, broken_sink
    )
    with pytest.raises(ToolExecutionError) as error:
        asyncio.run(
            broken_dispatcher.dispatch(
                ToolRequest(tool_name="echo", arguments={"text": "hello"}),
                authorization_context=context,
            )
        )
    assert "private executor detail" not in str(error.value)
    assert broken_sink.events[-1].outcome is ToolAuditOutcome.EXECUTION_FAILED
    assert broken_sink.events[-1].reason_code == "executor_error"


def test_write_tools_require_tool_write_and_declared_capabilities() -> None:
    write_definition = definition(access=ToolAccess.WRITE)
    executor = MockExecutor()
    tool_dispatcher, executor, sink = dispatcher(
        executor=executor,
        tool_definition=write_definition,
    )
    ordinary_context = AuthorizationContext(
        principal_id="writer", granted_capabilities=frozenset({"demo.read"})
    )

    with pytest.raises(AuthorizationDeniedError):
        asyncio.run(
            tool_dispatcher.dispatch(
                ToolRequest(tool_name="echo", arguments={"text": "write"}),
                authorization_context=ordinary_context,
            )
        )
    assert executor.calls == []
    assert sink.events[-1].reason_code == "missing_capabilities"

    authorized_context = AuthorizationContext(
        principal_id="writer",
        granted_capabilities=frozenset({"demo.read", WRITE_CAPABILITY}),
    )
    result = asyncio.run(
        tool_dispatcher.dispatch(
            ToolRequest(tool_name="echo", arguments={"text": "write"}),
            authorization_context=authorized_context,
        )
    )
    assert result.output == EchoOutput(result="ok")
    assert len(executor.calls) == 1
    assert sink.events[-3].access is ToolAccess.WRITE


def test_read_tool_does_not_require_write_permission() -> None:
    tool_dispatcher, executor, _ = dispatcher()
    context = AuthorizationContext(
        principal_id="reader", granted_capabilities=frozenset({"demo.read"})
    )

    asyncio.run(
        tool_dispatcher.dispatch(
            ToolRequest(tool_name="echo", arguments={"text": "read"}),
            authorization_context=context,
        )
    )
    assert len(executor.calls) == 1


def test_missing_context_is_denied_even_if_a_custom_policy_allows_it() -> None:
    class PermissivePolicy:
        def authorize(self, definition: ToolDefinition, context: AuthorizationContext | None):
            del definition, context
            return type("Decision", (), {"allowed": True, "reason": "granted"})()

    registry = ToolRegistry()
    registry.register(definition())
    sink = InMemoryToolAuditSink()
    executor = MockExecutor()
    tool_dispatcher = ToolDispatcher(registry, PermissivePolicy(), {"echo": executor}, sink)

    with pytest.raises(AuthorizationDeniedError):
        asyncio.run(
            tool_dispatcher.dispatch(
                ToolRequest(tool_name="echo", arguments={"text": "no-context"})
            )
        )
    assert executor.calls == []
    assert sink.events[-1].reason_code == "missing_context"


def test_audit_sink_failures_never_authorize_or_trigger_execution() -> None:
    class FailingSink:
        def record(self, event: object) -> None:
            del event
            raise RuntimeError("private audit backend detail")

    registry = ToolRegistry()
    registry.register(definition())
    executor = MockExecutor()
    tool_dispatcher = ToolDispatcher(registry, ToolPolicy(), {"echo": executor}, FailingSink())
    request = ToolRequest(tool_name="echo", arguments={"text": "never-run"})

    with pytest.raises(AuthorizationDeniedError):
        asyncio.run(tool_dispatcher.dispatch(request))
    assert executor.calls == []

    context = AuthorizationContext(
        principal_id="local-user", granted_capabilities=frozenset({"demo.read"})
    )
    with pytest.raises(ToolAuditError) as error:
        asyncio.run(
            tool_dispatcher.dispatch(request, authorization_context=context)
        )
    assert str(error.value) == ToolAuditError.safe_message
    assert "private audit backend detail" not in str(error.value)
    assert executor.calls == []


def test_terminal_audit_failure_surfaces_after_one_execution_without_retry() -> None:
    class FailSuccessSink:
        def record(self, event: object) -> None:
            if event.outcome is ToolAuditOutcome.EXECUTION_SUCCEEDED:
                raise RuntimeError("private audit backend detail")

    registry = ToolRegistry()
    registry.register(definition())
    executor = MockExecutor()
    tool_dispatcher = ToolDispatcher(registry, ToolPolicy(), {"echo": executor}, FailSuccessSink())
    context = AuthorizationContext(
        principal_id="local-user", granted_capabilities=frozenset({"demo.read"})
    )

    with pytest.raises(ToolAuditError):
        asyncio.run(
            tool_dispatcher.dispatch(
                ToolRequest(tool_name="echo", arguments={"text": "executed-once"}),
                authorization_context=context,
            )
        )
    assert len(executor.calls) == 1
