"""Tests for the fail-closed approval-request boundary."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from jarvis.tools import (
    ApprovalRequest,
    ApprovalRequestService,
    ApprovalRequestState,
    ApprovalRequestStoreError,
    AuthorizationContext,
    AuthorizationDeniedError,
    InMemoryApprovalRequestService,
    InMemoryToolAuditSink,
    ToolAccess,
    ToolApprovalRequiredError,
    ToolAuditError,
    ToolAuditOutcome,
    ToolAuditSink,
    ToolDefinition,
    ToolDispatcher,
    ToolInputValidationError,
    ToolPolicy,
    ToolRegistry,
    ToolRequest,
    UnknownApprovalRequestError,
)


class ApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload: str


class ApprovalOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: str


class MockExecutor:
    def __init__(self) -> None:
        self.calls: list[BaseModel] = []

    async def execute(self, arguments: BaseModel) -> object:
        self.calls.append(arguments)
        return ApprovalOutput(result="executed")


def approval_definition(**overrides: object) -> ToolDefinition:
    values: dict[str, object] = {
        "name": "report.publish",
        "description": "Publish a report after review.",
        "input_schema": ApprovalInput,
        "output_schema": ApprovalOutput,
        "required_capabilities": frozenset({"report:publish"}),
        "access": ToolAccess.WRITE,
        "requires_approval": True,
        "approval_summary": "Publish the prepared report after human review.",
    }
    values.update(overrides)
    return ToolDefinition(**values)


def build_dispatcher(
    *,
    service: ApprovalRequestService | None = None,
    sink: ToolAuditSink | None = None,
    definition: ToolDefinition | None = None,
) -> tuple[ToolDispatcher, MockExecutor, ToolAuditSink]:
    registered = definition or approval_definition()
    registry = ToolRegistry()
    registry.register(registered)
    executor = MockExecutor()
    audit_sink = sink or InMemoryToolAuditSink()
    return (
        ToolDispatcher(
            registry,
            ToolPolicy(),
            {registered.name: executor},
            audit_sink,
            approval_service=service,
        ),
        executor,
        audit_sink,
    )


def authorized_context() -> AuthorizationContext:
    return AuthorizationContext(
        principal_id="trusted-reviewer",
        granted_capabilities=frozenset({"report:publish", "tool:write"}),
    )


def request(payload: str = "private report contents") -> ToolRequest:
    return ToolRequest(
        tool_name="report.publish",
        arguments={"payload": payload},
    )


def test_approval_request_blocks_executor_and_uses_trusted_safe_metadata() -> None:
    now = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    request_id = UUID("f137c357-ff5f-4d2c-b16e-6b412703319a")
    service = InMemoryApprovalRequestService(clock=lambda: now, id_factory=lambda: request_id)
    dispatcher, executor, sink = build_dispatcher(service=service)

    with pytest.raises(ToolApprovalRequiredError) as raised:
        asyncio.run(
            dispatcher.dispatch(
                request(), authorization_context=authorized_context()
            )
        )

    approval = raised.value.approval_request
    assert approval == service.get_request(request_id)
    assert approval.request_id == request_id
    assert approval.tool_name == "report.publish"
    assert approval.requesting_principal == "trusted-reviewer"
    assert approval.created_at == now
    assert approval.state is ApprovalRequestState.PENDING
    assert approval.summary == "Publish the prepared report after human review."
    assert executor.calls == []
    assert [event.outcome for event in sink.events] == [
        ToolAuditOutcome.AUTHORIZATION_GRANTED,
        ToolAuditOutcome.APPROVAL_REQUESTED,
    ]
    audit_json = " ".join(event.model_dump_json() for event in sink.events)
    assert "private report contents" not in audit_json
    assert "Publish the prepared report" not in audit_json


def test_untrusted_request_cannot_supply_identity_permissions_or_approval_state() -> None:
    service = InMemoryApprovalRequestService()
    dispatcher, executor, _ = build_dispatcher(service=service)

    with pytest.raises(ToolInputValidationError):
        asyncio.run(
            dispatcher.dispatch(
                ToolRequest(
                    tool_name="report.publish",
                    arguments={
                        "payload": "private report contents",
                        "principal_id": "attacker",
                        "granted_capabilities": ["report:publish", "tool:write"],
                        "state": "approved",
                        "decision": "approved",
                    },
                ),
                authorization_context=authorized_context(),
            )
        )
    for field in ("principal_id", "granted_capabilities", "approval_state", "decision"):
        with pytest.raises(ValidationError):
            ToolRequest.model_validate(
                {"tool_name": "report.publish", field: "forged"}
            )
    assert executor.calls == []
    with pytest.raises(UnknownApprovalRequestError):
        service.get_request(uuid4())


def test_low_risk_tool_still_executes_without_creating_approval_request() -> None:
    service = InMemoryApprovalRequestService()
    definition = approval_definition(
        name="report.preview",
        access=ToolAccess.READ,
        required_capabilities=frozenset({"report:read"}),
        requires_approval=False,
        approval_summary=None,
    )
    dispatcher, executor, sink = build_dispatcher(
        service=service,
        definition=definition,
    )

    result = asyncio.run(
        dispatcher.dispatch(
            ToolRequest(tool_name="report.preview", arguments={"payload": "preview"}),
            authorization_context=AuthorizationContext(
                principal_id="reader", granted_capabilities=frozenset({"report:read"})
            ),
        )
    )

    assert result.output == ApprovalOutput(result="executed")
    assert len(executor.calls) == 1
    assert sink.events[-1].outcome is ToolAuditOutcome.EXECUTION_SUCCEEDED


def test_unauthorized_request_is_denied_before_approval_storage() -> None:
    class RecordingService(InMemoryApprovalRequestService):
        create_calls = 0

        def create_pending_request(self, **kwargs: str) -> ApprovalRequest:
            self.create_calls += 1
            return super().create_pending_request(**kwargs)

    service = RecordingService()
    dispatcher, executor, sink = build_dispatcher(service=service)

    with pytest.raises(AuthorizationDeniedError):
        asyncio.run(
            dispatcher.dispatch(
                request(),
                authorization_context=AuthorizationContext(
                    principal_id="underprivileged",
                    granted_capabilities=frozenset({"report:publish"}),
                ),
            )
        )

    assert service.create_calls == 0
    assert executor.calls == []
    assert sink.events[-1].outcome is ToolAuditOutcome.AUTHORIZATION_DENIED


def test_storage_failure_is_safe_audited_and_never_executes() -> None:
    class BrokenService:
        def create_pending_request(self, **kwargs: str) -> ApprovalRequest:
            del kwargs
            raise OSError("private storage detail")

        def get_request(self, request_id: UUID | str) -> ApprovalRequest:
            del request_id
            raise AssertionError("not used")

    dispatcher, executor, sink = build_dispatcher(service=BrokenService())

    with pytest.raises(ApprovalRequestStoreError) as error:
        asyncio.run(
            dispatcher.dispatch(request(), authorization_context=authorized_context())
        )

    assert str(error.value) == ApprovalRequestStoreError.safe_message
    assert "private storage detail" not in str(error.value)
    assert executor.calls == []
    assert sink.events[-1].outcome is ToolAuditOutcome.APPROVAL_FAILED
    assert sink.events[-1].reason_code == "approval_storage_failed"


def test_audit_sink_failure_never_executes_approval_required_tool() -> None:
    class FailApprovalAuditSink:
        def record(self, event: object) -> None:
            if event.outcome is ToolAuditOutcome.APPROVAL_REQUESTED:
                raise RuntimeError("private audit detail")

    service = InMemoryApprovalRequestService()
    dispatcher, executor, _ = build_dispatcher(
        service=service,
        sink=FailApprovalAuditSink(),
    )

    with pytest.raises(ToolAuditError):
        asyncio.run(
            dispatcher.dispatch(request(), authorization_context=authorized_context())
        )

    assert executor.calls == []


def test_invalid_requests_and_unknown_or_malformed_ids_fail_safely() -> None:
    with pytest.raises(ValidationError):
        ApprovalRequest(
            tool_name="report.publish",
            requesting_principal="reviewer",
            created_at=datetime(2026, 10, 18, 12, 0),
            summary="Review this request.",
        )
    with pytest.raises(ValidationError):
        ApprovalRequest(
            tool_name="report.publish",
            requesting_principal="reviewer",
            state="approved",
            summary="Review this request.",
        )

    service = InMemoryApprovalRequestService()
    with pytest.raises(ApprovalRequestStoreError):
        service.get_request("not-a-request-id")
    with pytest.raises(UnknownApprovalRequestError):
        service.get_request(uuid4())


def test_in_memory_service_documents_non_durable_storage() -> None:
    assert "Non-durable" in (InMemoryApprovalRequestService.__doc__ or "")


def test_approval_required_definition_must_have_safe_summary() -> None:
    with pytest.raises(ValidationError, match="safe approval summary"):
        approval_definition(approval_summary=None)
