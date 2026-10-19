"""Authorized device-task dispatch integration tests using temporary SQLite."""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import SecretStr

from jarvis.devices.models import DevicePlatform
from jarvis.devices.service import DeviceService
from jarvis.devices.store import DeviceStore
from jarvis.policy.allowlist import DeviceTaskToolPolicy
from jarvis.policy.device_grants import LocalDeviceTaskGrantPolicy
from jarvis.policy.task_submission import LocalTaskSubmissionPolicy
from jarvis.tasks.action import AuthorizedTaskAction
from jarvis.tasks.authorization import TaskSubmissionProposal, resolve_dispatch_authorization
from jarvis.tasks.errors import TaskDispatchDeniedError
from jarvis.tasks.models import TaskState
from jarvis.tasks.planner import DeterministicPlanner
from jarvis.tasks.runner import DispatcherBackedTaskRunner
from jarvis.tasks.runtime import TaskRuntime
from jarvis.tasks.store import TaskStore
from jarvis.tools.dispatcher import InMemoryToolAuditSink, ToolDispatcher
from jarvis.tools.models import ToolAuditOutcome
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.status import StatusOutput, register_status_tool

BOOTSTRAP = "dispatch-test-bootstrap-0123456789"


class CountingStatusExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, arguments) -> object:
        del arguments
        self.calls += 1
        return StatusOutput(
            component="jarvis-control-plane",
            state="responding",
            external_services_checked=False,
        )


def build_runtime(tmp_path, *, dispatch_ids: list[str]):
    device_store = DeviceStore(tmp_path / "devices.sqlite3")
    device_service = DeviceService(
        device_store,
        bootstrap_code=SecretStr(BOOTSTRAP),
    )
    receipt = device_service.pair(
        SecretStr(BOOTSTRAP),
        name="owner workstation",
        platform=DevicePlatform.WINDOWS,
    )

    submission_path = tmp_path / "submission-grants.json"
    submission_path.write_text(
        json.dumps({"schema_version": 1, "allowed_device_ids": [receipt.device.id]}),
        encoding="utf-8",
    )
    dispatch_path = tmp_path / "dispatch-grants.json"
    dispatch_path.write_text(
        json.dumps({"schema_version": 1, "allowed_device_ids": dispatch_ids}),
        encoding="utf-8",
    )

    store = TaskStore(tmp_path / "tasks.sqlite3")
    registry = ToolRegistry()
    register_status_tool(registry)
    executor = CountingStatusExecutor()
    audit = InMemoryToolAuditSink()
    dispatcher = ToolDispatcher(
        registry,
        DeviceTaskToolPolicy(registry),
        {"jarvis.status": executor},
        audit,
    )
    runner = DispatcherBackedTaskRunner(
        device_service=device_service,
        store=store,
        dispatcher=dispatcher,
        policy=LocalDeviceTaskGrantPolicy(dispatch_path),
    )
    runtime = TaskRuntime(
        store,
        DeterministicPlanner(),
        authorized_runner=runner,
    )
    proposal = TaskSubmissionProposal(
        idempotency_key="dispatch-test-key-0001",
        title="Read local status",
        action=AuthorizedTaskAction(tool_name="jarvis.status", arguments={}),
    )
    receipt_task = runtime.submit_device_task(
        receipt.device,
        proposal,
        policy=LocalTaskSubmissionPolicy(submission_path),
    )
    return device_store, store, runtime, receipt, receipt_task, executor, audit


def test_authorized_dispatch_reauthenticates_and_uses_tool_dispatcher(tmp_path) -> None:
    device_store, store, runtime, paired, submission, executor, audit = build_runtime(
        tmp_path,
        dispatch_ids=[],
    )
    try:
        assert submission.plan.task.state is TaskState.READY
        # Submission permission alone is insufficient; current dispatch policy
        # denies and the executor remains untouched.
        denied = asyncio.run(
            runtime.run_authorized_task_async(
                submission.plan.task.id,
                paired.access_token,
            )
        )
        assert denied.state is TaskState.FAILED
        assert executor.calls == 0
        # Policy denial is decided by the task dispatch boundary before the
        # request reaches ToolDispatcher, so no dispatcher audit event exists.
        assert audit.events == []

        dispatch_file = tmp_path / "dispatch-grants.json"
        dispatch_file.write_text(
            json.dumps(
                {"schema_version": 1, "allowed_device_ids": [paired.device.id]}
            ),
            encoding="utf-8",
        )

        # A failed task is terminal, so submit a fresh key after granting.
        retry_plan = runtime.submit_device_task(
            paired.device,
            TaskSubmissionProposal(
                idempotency_key="dispatch-test-key-0002",
                title="Read local status again",
                action=AuthorizedTaskAction(tool_name="jarvis.status", arguments={}),
            ),
            policy=LocalTaskSubmissionPolicy(tmp_path / "submission-grants.json"),
        )
        completed = asyncio.run(
            runtime.run_authorized_task_async(
                retry_plan.plan.task.id,
                paired.access_token,
            )
        )
        assert completed.state is TaskState.COMPLETED
        assert executor.calls == 1
        assert store.list_attempts(retry_plan.plan.steps[0].id)[0].outcome.value == "succeeded"
        assert any(event.outcome is ToolAuditOutcome.EXECUTION_SUCCEEDED for event in audit.events)
    finally:
        store.close()
        device_store.close()


def test_dispatch_policy_rejects_unsupported_actions_before_execution(tmp_path) -> None:
    device_store, store, runtime, paired, _submission, executor, _audit = build_runtime(
        tmp_path,
        dispatch_ids=["a" * 32],
    )
    try:
        action = AuthorizedTaskAction(tool_name="workspace.read_file", arguments={})
        with pytest.raises(TaskDispatchDeniedError):
            resolve_dispatch_authorization(
                paired.device,
                action,
                runtime.authorized_runner.policy,
            )
        assert executor.calls == 0
    finally:
        store.close()
        device_store.close()

