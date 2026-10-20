"""Authenticated, owner-scoped read-only task status API."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from jarvis.devices.api import authenticated_device
from jarvis.devices.models import Device, DeviceRole
from jarvis.tasks.action import AuthorizedTaskAction
from jarvis.tasks.authorization import TaskSubmissionPolicy, TaskSubmissionProposal
from jarvis.tasks.errors import (
    EntityNotFoundError,
    InvalidTransitionError,
    TaskAuthorizationError,
    TaskStoreError,
)
from jarvis.tasks.models import (
    ApprovalStatus,
    AttemptOutcome,
    SafeErrorCode,
    Step,
    StepAttempt,
    StepState,
    TaskPlan,
    TaskState,
)
from jarvis.tasks.runtime import TaskRuntime
from jarvis.tasks.store import TaskStore
from jarvis.tools.status import StatusOutput

router = APIRouter(prefix="/tasks", tags=["tasks"])
_STATUS_ACTION = "jarvis.status"
MAX_TASK_REQUEST_BYTES = 16 * 1024
MAX_TASK_JSON_DEPTH = 32
MAX_TASK_JSON_NODES = 2048


class TaskSubmissionRequest(TaskSubmissionProposal):
    """Public request contract; actions are required at the API boundary."""

    action: AuthorizedTaskAction


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key.")
        result[key] = value
    return result


def _reject_non_json_constant(value: str) -> object:
    raise ValueError(f"Unsupported JSON constant: {value}")


def _validate_json_structure(raw: bytes) -> None:
    """Bound nesting and structural complexity before invoking the JSON parser."""
    depth = 0
    nodes = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):  # { [
            depth += 1
            nodes += 1
            if depth > MAX_TASK_JSON_DEPTH:
                raise ValueError("JSON nesting exceeds the allowed limit.")
        elif byte in (0x7D, 0x5D):  # } ]
            depth -= 1
            nodes += 1
            if depth < 0:
                raise ValueError("JSON structure is invalid.")
        elif byte in (0x2C, 0x3A):  # , :
            nodes += 1
        if nodes > MAX_TASK_JSON_NODES:
            raise ValueError("JSON complexity exceeds the allowed limit.")

    json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_non_json_constant,
    )


class TaskRequestSafetyMiddleware:
    """Bound and preflight task JSON before FastAPI performs model validation."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_TASK_REQUEST_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or not (
                scope.get("path") == "/tasks"
                or (
                    scope.get("path", "").startswith("/tasks/approvals/")
                    and scope.get("path", "").endswith("/decision")
                )
            )
        ):
            await self.app(scope, receive, send)
            return

        content_length = next(
            (value for name, value in scope.get("headers", []) if name.lower() == b"content-length"),
            None,
        )
        if content_length is not None:
            try:
                if int(content_length) > self.max_bytes:
                    await self._send_error(scope, receive, send, 413)
                    return
            except ValueError:
                pass

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                await self._send_error(scope, receive, send, 413)
                return
            if not message.get("more_body", False):
                break

        try:
            _validate_json_structure(bytes(body))
        except (UnicodeDecodeError, ValueError, RecursionError, json.JSONDecodeError):
            await self._send_error(scope, receive, send, 422)
            return

        delivered = False

        async def replay_body() -> Message:
            nonlocal delivered
            if delivered:
                return {"type": "http.request", "body": b"", "more_body": False}
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay_body, send)

    @staticmethod
    async def _send_error(
        scope: Scope, receive: Receive, send: Send, status_code: int
    ) -> None:
        if status_code == 413:
            payload = {
                "error": {
                    "code": "request_too_large",
                    "message": "Request body exceeds the allowed size.",
                }
            }
        else:
            payload = {
                "error": {
                    "code": "invalid_request",
                    "message": "Request validation failed.",
                }
            }
        await JSONResponse(status_code=status_code, content=payload)(scope, receive, send)


class TaskAttemptStatusResponse(BaseModel):
    """Allowlisted persisted attempt facts safe for the task owner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_number: int
    outcome: AttemptOutcome
    error_code: SafeErrorCode | None = None
    started_at: datetime
    finished_at: datetime | None = None


class TaskStepStatusResponse(BaseModel):
    """Public step state with only the existing safe status-tool output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str
    state: StepState
    dependencies: tuple[str, ...]
    attempt_count: int
    attempt_limit: int
    attempts: tuple[TaskAttemptStatusResponse, ...]
    created_at: datetime
    updated_at: datetime
    latest_attempt_outcome: AttemptOutcome | None = None
    latest_error_code: SafeErrorCode | None = None
    latest_attempt_started_at: datetime | None = None
    latest_attempt_finished_at: datetime | None = None
    result: StatusOutput | None = None


class TaskProgressResponse(BaseModel):
    """Deterministic step counts derived from the persisted plan state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    completed_steps: int
    total_steps: int


class TaskStatusResponse(BaseModel):
    """Task status with explicit public status and legacy state fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    status: TaskState
    # Preserve the previously published wire field for existing clients.
    state: TaskState
    created_at: datetime
    updated_at: datetime
    progress: TaskProgressResponse
    result: StatusOutput | None
    steps: tuple[TaskStepStatusResponse, ...]


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal["approve", "deny"]


class ApprovalDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: UUID
    status: ApprovalStatus
    expires_at: datetime


def _task_store(request: Request) -> TaskStore:
    services = getattr(request.app.state, "_task_services", None)
    store = None if services is None else getattr(services, "store", None)
    if not isinstance(store, TaskStore):
        raise HTTPException(status_code=503, detail="Task status is unavailable.")
    return store


def _task_runtime(request: Request) -> TaskRuntime:
    services = getattr(request.app.state, "_task_services", None)
    runtime = None if services is None else getattr(services, "runtime", None)
    if not isinstance(runtime, TaskRuntime):
        raise HTTPException(status_code=503, detail="Task submission is unavailable.")
    return runtime


def _task_submission_policy(request: Request) -> TaskSubmissionPolicy:
    services = getattr(request.app.state, "_task_services", None)
    policy = None if services is None else getattr(services, "submission_policy", None)
    if policy is None or not callable(getattr(policy, "authorize", None)):
        raise HTTPException(status_code=503, detail="Task submission is unavailable.")
    return policy


def _safe_status_result(store: TaskStore, step: Step) -> StatusOutput | None:
    action = step.action
    if (
        not isinstance(action, AuthorizedTaskAction)
        or action.tool_name != _STATUS_ACTION
        or action.arguments != {}
    ):
        return None
    stored = store.get_step_result(step.id)
    if stored is None:
        return None
    try:
        return StatusOutput.model_validate(stored.data)
    except (ValidationError, TypeError, ValueError):
        return None


def _safe_attempt(attempt: StepAttempt) -> TaskAttemptStatusResponse:
    """Project a persisted attempt without identifiers or authorization data."""
    return TaskAttemptStatusResponse(
        attempt_number=attempt.attempt_number,
        outcome=attempt.outcome,
        error_code=attempt.error_code,
        started_at=attempt.started_at,
        finished_at=attempt.finished_at,
    )


def _project_task_status(plan: TaskPlan, store: TaskStore) -> TaskStatusResponse:
    steps: list[TaskStepStatusResponse] = []
    for step in plan.steps:
        attempts = store.list_attempts(step.id)
        if len(attempts) != step.attempt_count or tuple(
            attempt.attempt_number for attempt in attempts
        ) != tuple(range(1, step.attempt_count + 1)):
            raise TaskStoreError("Persisted attempt metadata is inconsistent.")
        latest = attempts[-1] if attempts else None
        steps.append(
            TaskStepStatusResponse(
                step_id=step.id,
                state=step.state,
                dependencies=tuple(step.dependencies),
                attempt_count=step.attempt_count,
                attempt_limit=step.attempt_limit,
                attempts=tuple(_safe_attempt(attempt) for attempt in attempts),
                created_at=step.created_at,
                updated_at=step.updated_at,
                latest_attempt_outcome=None if latest is None else latest.outcome,
                latest_error_code=None if latest is None else latest.error_code,
                latest_attempt_started_at=None if latest is None else latest.started_at,
                latest_attempt_finished_at=None if latest is None else latest.finished_at,
                result=_safe_status_result(store, step),
            )
        )
    completed_steps = sum(step.state is StepState.SUCCEEDED for step in plan.steps)
    step_responses = tuple(steps)
    task_result = step_responses[0].result if len(step_responses) == 1 else None
    return TaskStatusResponse(
        task_id=plan.task.id,
        status=plan.task.state,
        state=plan.task.state,
        created_at=plan.task.created_at,
        updated_at=plan.task.updated_at,
        progress=TaskProgressResponse(
            completed_steps=completed_steps,
            total_steps=len(plan.steps),
        ),
        result=task_result,
        steps=step_responses,
    )


@router.post("", response_model=TaskStatusResponse, status_code=201)
async def submit_task(
    payload: TaskSubmissionRequest,
    authenticated: Annotated[tuple[SecretStr, Device], Depends(authenticated_device)],
    store: Annotated[TaskStore, Depends(_task_store)],
    runtime: Annotated[TaskRuntime, Depends(_task_runtime)],
    policy: Annotated[TaskSubmissionPolicy, Depends(_task_submission_policy)],
    response: Response,
) -> TaskStatusResponse:
    """Authorize and persist a task owned by the authenticated device."""
    device = authenticated[1]
    try:
        receipt = runtime.submit_device_task(device, payload, policy=policy)
        # Return a fresh owner-scoped projection of the committed task state.
        plan = store.get_task_plan_for_device(receipt.plan.task.id, device.id)
        projected = _project_task_status(plan, store)
    except EntityNotFoundError:
        raise HTTPException(status_code=404, detail="Task was not found.") from None
    except (TaskStoreError, sqlite3.Error, TypeError, ValueError):
        raise TaskStoreError("Task submission is temporarily unavailable.") from None

    response.status_code = 201 if receipt.created else 200
    response.headers["Location"] = f"/tasks/{projected.task_id}"
    response.headers["Cache-Control"] = "no-store"
    return projected


@router.get("/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(
    task_id: str,
    authenticated: Annotated[tuple[SecretStr, Device], Depends(authenticated_device)],
    store: Annotated[TaskStore, Depends(_task_store)],
    response: Response,
) -> TaskStatusResponse:
    """Return status only after the active bearer identity passes the owner query."""
    device = authenticated[1]
    try:
        plan = store.get_task_plan_for_device(task_id, device.id)
    except EntityNotFoundError:
        # Unknown and foreign task IDs intentionally have the same response.
        raise HTTPException(status_code=404, detail="Task was not found.") from None
    except (TaskStoreError, sqlite3.Error, TypeError, ValueError):
        raise HTTPException(status_code=503, detail="Task status is unavailable.") from None
    response.headers["Cache-Control"] = "no-store"
    try:
        return _project_task_status(plan, store)
    except (TaskStoreError, sqlite3.Error, TypeError, ValueError):
        raise HTTPException(status_code=503, detail="Task status is unavailable.") from None


@router.post("/{task_id}/cancel", response_model=TaskStatusResponse)
async def cancel_task(
    task_id: str,
    authenticated: Annotated[tuple[SecretStr, Device], Depends(authenticated_device)],
    store: Annotated[TaskStore, Depends(_task_store)],
    runtime: Annotated[TaskRuntime, Depends(_task_runtime)],
    response: Response,
) -> TaskStatusResponse:
    """Cancel an owned task only if no execution attempt has started."""
    try:
        runtime.cancel_owned_task(task_id, authenticated[0])
        plan = store.get_task_plan_for_device(task_id, authenticated[1].id)
        projected = _project_task_status(plan, store)
    except EntityNotFoundError:
        # Unknown and foreign task IDs intentionally have the same response.
        raise HTTPException(status_code=404, detail="Task was not found.") from None
    except TaskAuthorizationError:
        raise HTTPException(status_code=404, detail="Task was not found.") from None
    except InvalidTransitionError:
        raise HTTPException(
            status_code=409, detail="Task is unavailable for cancellation."
        ) from None
    except (TaskStoreError, sqlite3.Error, TypeError, ValueError):
        raise HTTPException(status_code=503, detail="Task cancellation is unavailable.") from None
    response.headers["Cache-Control"] = "no-store"
    return projected


@router.post("/approvals/{approval_id}/decision", response_model=ApprovalDecisionResponse)
async def decide_approval(
    approval_id: UUID,
    payload: ApprovalDecisionRequest,
    authenticated: Annotated[tuple[SecretStr, Device], Depends(authenticated_device)],
    runtime: Annotated[TaskRuntime, Depends(_task_runtime)],
    response: Response,
) -> ApprovalDecisionResponse:
    """Record an owner decision only; this route never resumes task execution."""
    if authenticated[1].role is not DeviceRole.OWNER:
        raise HTTPException(status_code=404, detail="Approval request was not found.")
    try:
        decision = runtime.decide_approval(
            approval_id,
            authenticated[0],
            approve=payload.decision == "approve",
        )
    except EntityNotFoundError:
        raise HTTPException(status_code=404, detail="Approval request was not found.") from None
    except InvalidTransitionError:
        raise HTTPException(
            status_code=409, detail="Approval request is unavailable for decision."
        ) from None
    except (TaskStoreError, sqlite3.Error, TypeError, ValueError):
        raise HTTPException(status_code=503, detail="Approval decision is unavailable.") from None
    response.headers["Cache-Control"] = "no-store"
    return ApprovalDecisionResponse(
        approval_id=decision.request_id,
        status=decision.status,
        expires_at=decision.expires_at,
    )


@router.post("/{task_id}/resume", response_model=TaskStatusResponse)
async def resume_approved_task(
    task_id: str,
    authenticated: Annotated[tuple[SecretStr, Device], Depends(authenticated_device)],
    store: Annotated[TaskStore, Depends(_task_store)],
    runtime: Annotated[TaskRuntime, Depends(_task_runtime)],
    response: Response,
) -> TaskStatusResponse:
    """Resume one approved action only after fresh task-owner authentication."""
    try:
        await runtime.resume_approved_task_async(task_id, authenticated[0])
        plan = store.get_task_plan_for_device(task_id, authenticated[1].id)
        projected = _project_task_status(plan, store)
    except EntityNotFoundError:
        raise HTTPException(status_code=404, detail="Task was not found.") from None
    except InvalidTransitionError:
        raise HTTPException(status_code=409, detail="Task is unavailable for resume.") from None
    except TaskAuthorizationError:
        # Unknown and foreign task IDs share the same response on this route.
        raise HTTPException(status_code=404, detail="Task was not found.") from None
    except (TaskStoreError, sqlite3.Error, TypeError, ValueError):
        raise HTTPException(status_code=503, detail="Task resume is unavailable.") from None
    response.headers["Cache-Control"] = "no-store"
    return projected
