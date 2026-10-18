"""Injected execution boundary with validation, authorization, and audit events."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from typing import Protocol

from pydantic import BaseModel, ValidationError

from jarvis.tools.approval import (
    ApprovalRequest,
    ApprovalRequestService,
    ApprovalRequestState,
)
from jarvis.tools.errors import (
    ApprovalRequestStoreError,
    AuthorizationDeniedError,
    ToolApprovalRequiredError,
    ToolAuditError,
    ToolExecutionError,
    ToolInputValidationError,
    ToolOutputValidationError,
    ToolResultTooLargeError,
    ToolTimeoutError,
)
from jarvis.tools.models import (
    AuthorizationContext,
    ToolAuditEvent,
    ToolAuditOutcome,
    ToolDefinition,
    ToolRequest,
)
from jarvis.tools.policy import ToolPolicy
from jarvis.tools.registry import ToolRegistry


class ToolExecutor(Protocol):
    async def execute(self, arguments: BaseModel) -> object: ...


class ToolAuditSink(Protocol):
    """Receives safe, in-process audit events; durability is sink-specific."""

    def record(self, event: ToolAuditEvent) -> None: ...


class InMemoryToolAuditSink:
    """Simple observable sink; records are lost when the process exits."""

    def __init__(self) -> None:
        self.events: list[ToolAuditEvent] = []

    def record(self, event: ToolAuditEvent) -> None:
        self.events.append(event)


class ToolExecutionResult(BaseModel):
    """Validated output from one injected executor."""

    model_config = {"extra": "forbid", "arbitrary_types_allowed": True}

    tool_name: str
    principal_id: str
    output: BaseModel


class ToolDispatcher:
    def __init__(
        self,
        registry: ToolRegistry,
        policy: ToolPolicy,
        executors: Mapping[str, ToolExecutor],
        audit_sink: ToolAuditSink,
        approval_service: ApprovalRequestService | None = None,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._executors = dict(executors)
        self._audit_sink = audit_sink
        self._approval_service = approval_service
        self._registry.seal()

    async def dispatch(
        self,
        request: ToolRequest,
        *,
        authorization_context: AuthorizationContext | None = None,
    ) -> ToolExecutionResult:
        definition = self._registry.get(request.tool_name)
        decision = self._policy.authorize(definition, authorization_context)
        if authorization_context is None or not decision.allowed:
            reason = "missing_context" if authorization_context is None else decision.reason
            self._record_denial(definition, authorization_context, reason)
            raise AuthorizationDeniedError()

        self._record(
            self._event(
                definition,
                authorization_context,
                ToolAuditOutcome.AUTHORIZATION_GRANTED,
                "granted",
            )
        )

        try:
            validated_input = self._validate_input(definition, request.arguments)
        except ToolInputValidationError:
            self._record(
                self._event(
                    definition,
                    authorization_context,
                    ToolAuditOutcome.VALIDATION_FAILED,
                    "input_validation_failed",
                )
            )
            raise

        if definition.requires_approval:
            self._request_approval(definition, authorization_context)

        executor = self._executors.get(definition.name)
        if executor is None:
            self._record(
                self._event(
                    definition,
                    authorization_context,
                    ToolAuditOutcome.EXECUTION_FAILED,
                    "executor_unavailable",
                )
            )
            raise ToolExecutionError() from None

        self._record(
            self._event(
                definition,
                authorization_context,
                ToolAuditOutcome.EXECUTION_STARTED,
                "execution_started",
            )
        )
        started = time.monotonic()
        try:
            raw_output = await asyncio.wait_for(
                executor.execute(validated_input), timeout=definition.timeout_seconds
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._record(
                self._event(
                    definition,
                    authorization_context,
                    ToolAuditOutcome.EXECUTION_TIMED_OUT,
                    "timeout",
                    started=started,
                )
            )
            raise ToolTimeoutError() from None
        except Exception:
            self._record(
                self._event(
                    definition,
                    authorization_context,
                    ToolAuditOutcome.EXECUTION_FAILED,
                    "executor_error",
                    started=started,
                )
            )
            raise ToolExecutionError() from None

        try:
            output = definition.output_schema.model_validate(raw_output)
            serialized = json.dumps(
                output.model_dump(mode="json"),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (ValidationError, TypeError, ValueError, RecursionError):
            self._record(
                self._event(
                    definition,
                    authorization_context,
                    ToolAuditOutcome.VALIDATION_FAILED,
                    "output_validation_failed",
                    started=started,
                )
            )
            raise ToolOutputValidationError() from None
        if len(serialized.encode("utf-8")) > definition.max_result_bytes:
            self._record(
                self._event(
                    definition,
                    authorization_context,
                    ToolAuditOutcome.VALIDATION_FAILED,
                    "result_size_exceeded",
                    started=started,
                )
            )
            raise ToolResultTooLargeError()

        self._record(
            self._event(
                definition,
                authorization_context,
                ToolAuditOutcome.EXECUTION_SUCCEEDED,
                "success",
                started=started,
            )
        )
        return ToolExecutionResult(
            tool_name=definition.name,
            principal_id=authorization_context.principal_id,
            output=output,
        )

    def _request_approval(
        self,
        definition: ToolDefinition,
        authorization_context: AuthorizationContext,
    ) -> None:
        service = self._approval_service
        summary = definition.approval_summary
        if service is None or summary is None:
            self._record(
                self._event(
                    definition,
                    authorization_context,
                    ToolAuditOutcome.APPROVAL_FAILED,
                    "approval_service_unavailable",
                )
            )
            raise ApprovalRequestStoreError() from None

        try:
            request = service.create_pending_request(
                tool_name=definition.name,
                principal_id=authorization_context.principal_id,
                summary=summary,
            )
            request = ApprovalRequest.model_validate(request)
            if (
                request.state is not ApprovalRequestState.PENDING
                or request.tool_name != definition.name
                or request.requesting_principal != authorization_context.principal_id
                or request.summary != summary
            ):
                raise ApprovalRequestStoreError()
        except Exception:
            self._record(
                self._event(
                    definition,
                    authorization_context,
                    ToolAuditOutcome.APPROVAL_FAILED,
                    "approval_storage_failed",
                )
            )
            raise ApprovalRequestStoreError() from None

        self._record(
            self._event(
                definition,
                authorization_context,
                ToolAuditOutcome.APPROVAL_REQUESTED,
                "approval_requested",
            )
        )
        raise ToolApprovalRequiredError(request)

    def _record_denial(
        self,
        definition: ToolDefinition,
        context: AuthorizationContext | None,
        reason: str,
    ) -> None:
        try:
            self._record(
                self._event(
                    definition,
                    context,
                    ToolAuditOutcome.AUTHORIZATION_DENIED,
                    reason,
                )
            )
        except ToolAuditError:
            # Preserve the denial even when the sink is unavailable; never continue dispatch.
            pass

    def _record(self, event: ToolAuditEvent) -> None:
        try:
            self._audit_sink.record(event)
        except Exception:
            # Pre-execution audit failures fail closed. Post-execution failures surface
            # as this same typed error so callers know the outcome could not be recorded.
            raise ToolAuditError() from None

    @staticmethod
    def _event(
        definition: ToolDefinition,
        context: AuthorizationContext | None,
        outcome: ToolAuditOutcome,
        reason_code: str,
        *,
        started: float | None = None,
    ) -> ToolAuditEvent:
        duration_ms = None if started is None else max(0, round((time.monotonic() - started) * 1000))
        return ToolAuditEvent(
            tool_name=definition.name,
            principal_id=None if context is None else context.principal_id,
            access=definition.access,
            outcome=outcome,
            reason_code=reason_code,
            duration_ms=duration_ms,
        )

    @staticmethod
    def _validate_input(definition: ToolDefinition, arguments: dict[str, object]) -> BaseModel:
        if not set(arguments) <= set(definition.input_schema.model_fields):
            raise ToolInputValidationError()
        try:
            return definition.input_schema.model_validate(arguments)
        except (ValidationError, TypeError, ValueError):
            raise ToolInputValidationError() from None
