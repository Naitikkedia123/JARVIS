"""Durable owner approval and explicit task resume integration coverage."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier, Event, Thread

import pytest
from pydantic import SecretStr

from jarvis.devices.models import DevicePlatform
from jarvis.devices.service import DeviceService
from jarvis.devices.store import DeviceStore, DeviceStoreError
from jarvis.policy.allowlist import DeviceTaskToolPolicy
from jarvis.policy.device_grants import LocalDeviceTaskGrantPolicy
from jarvis.policy.task_submission import LocalTaskSubmissionPolicy
from jarvis.tasks.action import AuthorizedTaskAction
from jarvis.tasks.authorization import TaskSubmissionProposal
from jarvis.tasks.errors import (
    InvalidTransitionError,
    TaskAuthorizationError,
    TaskDispatchDeniedError,
)
from jarvis.tasks.models import (
    ApprovalAuditEventType,
    ApprovalStatus,
    AttemptAuthorization,
    StepState,
    TaskState,
)
from jarvis.tasks.planner import DeterministicPlanner
from jarvis.tasks.runner import DispatcherBackedTaskRunner
from jarvis.tasks.runtime import TaskRuntime
from jarvis.tasks.store import TaskStore
from jarvis.tools.dispatcher import InMemoryToolAuditSink, ToolDispatcher
from jarvis.tools.models import ToolAccess, ToolDefinition
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.status import StatusInput, StatusOutput

BOOTSTRAP = "approval-lifecycle-bootstrap-0123456789"


class CountingStatusExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, arguments: StatusInput) -> object:
        del arguments
        self.calls += 1
        await asyncio.sleep(0)
        return StatusOutput(
            component="jarvis-control-plane",
            state="responding",
            external_services_checked=False,
        )


def build_approval_runtime(tmp_path, *, requester_is_companion: bool = False):
    now = [datetime(2026, 10, 20, 8, 0, tzinfo=UTC)]
    device_store = DeviceStore(tmp_path / "devices.sqlite3", clock=lambda: now[0])
    service = DeviceService(
        device_store,
        bootstrap_code=SecretStr(BOOTSTRAP),
        clock=lambda: now[0],
    )
    owner = service.pair(
        SecretStr(BOOTSTRAP),
        name="owner workstation",
        platform=DevicePlatform.WINDOWS,
    )
    requester = owner
    if requester_is_companion:
        pairing_code = service.issue_pairing_code(owner.access_token)
        requester = service.pair(
            pairing_code,
            name="task device",
            platform=DevicePlatform.ANDROID,
        )

    grants = tmp_path / "grants.json"
    grants.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "allowed_device_ids": [requester.device.id],
            }
        ),
        encoding="utf-8",
    )
    submissions = tmp_path / "submission-grants.json"
    submissions.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "allowed_device_ids": [requester.device.id],
            }
        ),
        encoding="utf-8",
    )

    store = TaskStore(tmp_path / "tasks.sqlite3", clock=lambda: now[0])
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="jarvis.status",
            version="approval-test-v1",
            description="Read local JARVIS status.",
            input_schema=StatusInput,
            output_schema=StatusOutput,
            required_capabilities=frozenset({"system:status:read"}),
            access=ToolAccess.READ,
            requires_approval=True,
            approval_summary="Read local JARVIS status.",
        )
    )
    executor = CountingStatusExecutor()
    dispatcher = ToolDispatcher(
        registry,
        DeviceTaskToolPolicy(registry),
        {"jarvis.status": executor},
        InMemoryToolAuditSink(),
    )
    runner = DispatcherBackedTaskRunner(
        device_service=service,
        store=store,
        dispatcher=dispatcher,
        policy=LocalDeviceTaskGrantPolicy(grants),
    )
    runtime = TaskRuntime(
        store,
        DeterministicPlanner(),
        authorized_runner=runner,
        approval_lifetime_minutes=5,
    )
    submission = runtime.submit_device_task(
        requester.device,
        TaskSubmissionProposal(
            idempotency_key="approval-test-idempotency-0001",
            title="Read status after approval",
            action=AuthorizedTaskAction(tool_name="jarvis.status", arguments={}),
        ),
        policy=LocalTaskSubmissionPolicy(submissions),
    )
    return device_store, store, service, runtime, owner, requester, submission, executor, grants, now


def wait_for_approval(runtime, submission, token):
    task = asyncio.run(
        runtime.run_authorized_task_async(
            submission.plan.task.id,
            token,
        )
    )
    assert task.state is TaskState.WAITING_APPROVAL
    return runtime.store.list_approval_requests(task.id)[0]


def test_owned_approval_is_durable_and_resume_consumes_once(tmp_path) -> None:
    device_store, store, _service, runtime, owner, requester, submission, executor, _grants, _now = (
        build_approval_runtime(tmp_path, requester_is_companion=True)
    )
    try:
        request = wait_for_approval(runtime, submission, requester.access_token)
        assert request.status is ApprovalStatus.PENDING
        assert store.get_task(submission.plan.task.id).state is TaskState.WAITING_APPROVAL
        assert store.get_step(submission.plan.steps[0].id).state is StepState.WAITING_APPROVAL
        assert store.list_attempts(submission.plan.steps[0].id) == []
        assert [event.event_type for event in store.list_approval_audit_events(request.request_id)] == [
            ApprovalAuditEventType.REQUESTED
        ]
        assert executor.calls == 0

        approved = runtime.decide_approval(
            request.request_id,
            owner.access_token,
            approve=True,
        )
        assert approved.status is ApprovalStatus.APPROVED
        assert executor.calls == 0
        assert store.get_task(submission.plan.task.id).state is TaskState.WAITING_APPROVAL

        with pytest.raises(TaskAuthorizationError):
            asyncio.run(
                runtime.resume_approved_task_async(
                    submission.plan.task.id,
                    owner.access_token,
                )
            )
        assert executor.calls == 0

        completed = asyncio.run(
            runtime.resume_approved_task_async(
                submission.plan.task.id,
                requester.access_token,
            )
        )
        assert completed.state is TaskState.COMPLETED
        attempts = store.list_attempts(submission.plan.steps[0].id)
        assert len(attempts) == 1
        assert attempts[0].outcome.value == "succeeded"
        assert attempts[0].dispatch_claimed_at is not None
        assert store.get_approval_request(request.request_id).status is ApprovalStatus.CONSUMED
        events = store.list_approval_audit_events(request.request_id)
        assert [event.event_type for event in events] == [
            ApprovalAuditEventType.REQUESTED,
            ApprovalAuditEventType.APPROVED,
            ApprovalAuditEventType.CONSUMED,
        ]
        assert events[-1].attempt_id == attempts[0].id
        assert executor.calls == 1

        with pytest.raises(InvalidTransitionError):
            asyncio.run(
                runtime.resume_approved_task_async(
                    submission.plan.task.id,
                    requester.access_token,
                )
            )
        assert len(store.list_attempts(submission.plan.steps[0].id)) == 1
        assert executor.calls == 1
    finally:
        store.close()
        device_store.close()


def test_expiry_at_exact_boundary_fails_waiting_task_without_execution(tmp_path) -> None:
    device_store, store, _service, runtime, owner, requester, submission, executor, _grants, now = (
        build_approval_runtime(tmp_path)
    )
    try:
        request = wait_for_approval(runtime, submission, requester.access_token)
        now[0] = request.expires_at
        expired = runtime.decide_approval(
            request.request_id,
            owner.access_token,
            approve=True,
        )
        assert expired.status is ApprovalStatus.EXPIRED
        assert store.get_task(submission.plan.task.id).state is TaskState.FAILED
        assert store.get_step(submission.plan.steps[0].id).state is StepState.FAILED
        assert store.list_attempts(submission.plan.steps[0].id) == []
        assert store.list_approval_audit_events(request.request_id)[-1].event_type is ApprovalAuditEventType.EXPIRED
        assert executor.calls == 0
    finally:
        store.close()
        device_store.close()


def test_owner_credential_rotation_revokes_pending_approval(tmp_path) -> None:
    device_store, store, service, runtime, owner, requester, submission, executor, _grants, _now = (
        build_approval_runtime(tmp_path)
    )
    try:
        request = wait_for_approval(runtime, submission, requester.access_token)
        rotated = service.rotate_owner_credential(owner.access_token)
        revoked = runtime.decide_approval(
            request.request_id,
            rotated.access_token,
            approve=True,
        )
        assert revoked.status is ApprovalStatus.REVOKED
        assert revoked.owner_credential_generation == owner.device.credential_generation
        assert store.get_task(submission.plan.task.id).state is TaskState.FAILED
        assert executor.calls == 0
    finally:
        store.close()
        device_store.close()


def test_host_recovery_generation_revokes_pending_approval(tmp_path) -> None:
    device_store, store, service, runtime, owner, requester, submission, executor, _grants, _now = (
        build_approval_runtime(tmp_path)
    )
    try:
        request = wait_for_approval(runtime, submission, requester.access_token)
        device_store.recover_owner_credential(hashlib.sha256(b"replacement-token").hexdigest())
        current_owner = device_store.get_device(owner.device.id)
        with service.reserve_owner_generation(
            current_owner.id, current_owner.credential_generation
        ):
            revoked = store.decide_approval_request(
                request.request_id,
                owner_device_id=current_owner.id,
                owner_credential_generation=current_owner.credential_generation,
                action_digest=request.action_digest,
                tool_definition_digest=request.tool_definition_digest,
                approve=True,
                precommit_validation=lambda: service.validate_reserved_owner_generation(
                    current_owner.id, current_owner.credential_generation
                ),
            )
        assert current_owner.credential_generation == owner.device.credential_generation + 1
        assert revoked.status is ApprovalStatus.REVOKED
        assert store.get_task(submission.plan.task.id).state is TaskState.FAILED
        assert executor.calls == 0
    finally:
        store.close()
        device_store.close()


def test_consumption_audit_failure_rolls_back_before_executor(tmp_path) -> None:
    device_store, store, _service, runtime, owner, requester, submission, executor, _grants, _now = (
        build_approval_runtime(tmp_path)
    )
    try:
        request = wait_for_approval(runtime, submission, requester.access_token)
        runtime.decide_approval(request.request_id, owner.access_token, approve=True)
        store._connection.execute(
            """CREATE TRIGGER reject_consumed_approval_audit
                BEFORE INSERT ON approval_audit_events WHEN NEW.event_type = 'consumed'
                BEGIN SELECT RAISE(ABORT, 'injected consumed audit failure'); END"""
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected consumed audit failure"):
            asyncio.run(
                runtime.resume_approved_task_async(
                    submission.plan.task.id,
                    requester.access_token,
                )
            )
        assert store.get_approval_request(request.request_id).status is ApprovalStatus.APPROVED
        assert store.get_task(submission.plan.task.id).state is TaskState.WAITING_APPROVAL
        assert store.get_step(submission.plan.steps[0].id).state is StepState.WAITING_APPROVAL
        assert store.list_attempts(submission.plan.steps[0].id) == []
        assert executor.calls == 0
    finally:
        store.close()
        device_store.close()


def test_decision_audit_failure_rolls_back_owner_decision(tmp_path) -> None:
    device_store, store, _service, runtime, owner, requester, submission, executor, _grants, _now = (
        build_approval_runtime(tmp_path)
    )
    try:
        request = wait_for_approval(runtime, submission, requester.access_token)
        store._connection.execute(
            """CREATE TRIGGER reject_owner_decision_audit
                BEFORE INSERT ON approval_audit_events WHEN NEW.event_type = 'approved'
                BEGIN SELECT RAISE(ABORT, 'injected decision audit failure'); END"""
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected decision audit failure"):
            runtime.decide_approval(request.request_id, owner.access_token, approve=True)
        assert store.get_approval_request(request.request_id).status is ApprovalStatus.PENDING
        assert store.get_task(submission.plan.task.id).state is TaskState.WAITING_APPROVAL
        assert store.get_step(submission.plan.steps[0].id).state is StepState.WAITING_APPROVAL
        assert executor.calls == 0
    finally:
        store.close()
        device_store.close()


def test_owner_denial_fails_waiter_without_execution(tmp_path) -> None:
    device_store, store, _service, runtime, owner, requester, submission, executor, _grants, _now = (
        build_approval_runtime(tmp_path)
    )
    try:
        request = wait_for_approval(runtime, submission, requester.access_token)
        denied = runtime.decide_approval(request.request_id, owner.access_token, approve=False)
        assert denied.status is ApprovalStatus.DENIED
        assert store.get_task(submission.plan.task.id).state is TaskState.FAILED
        assert store.get_step(submission.plan.steps[0].id).state is StepState.FAILED
        assert store.list_attempts(submission.plan.steps[0].id) == []
        assert store.list_approval_audit_events(request.request_id)[-1].event_type is ApprovalAuditEventType.DENIED
        assert executor.calls == 0
    finally:
        store.close()
        device_store.close()


def test_dispatch_grant_is_rechecked_at_resume(tmp_path) -> None:
    device_store, store, _service, runtime, owner, requester, submission, executor, grants, _now = (
        build_approval_runtime(tmp_path, requester_is_companion=True)
    )
    try:
        request = wait_for_approval(runtime, submission, requester.access_token)
        runtime.decide_approval(request.request_id, owner.access_token, approve=True)
        grants.write_text(json.dumps({"schema_version": 1, "allowed_device_ids": []}), encoding="utf-8")
        with pytest.raises(TaskDispatchDeniedError):
            asyncio.run(
                runtime.resume_approved_task_async(
                    submission.plan.task.id,
                    requester.access_token,
                )
            )
        assert store.get_approval_request(request.request_id).status is ApprovalStatus.APPROVED
        assert store.get_task(submission.plan.task.id).state is TaskState.WAITING_APPROVAL
        assert store.list_attempts(submission.plan.steps[0].id) == []
        assert executor.calls == 0
    finally:
        store.close()
        device_store.close()


@pytest.mark.parametrize("first_decision", [True, False])
def test_competing_owner_decisions_have_one_winner(tmp_path, first_decision) -> None:
    device_store, store, _service, runtime, owner, requester, submission, executor, _grants, now = (
        build_approval_runtime(tmp_path)
    )
    database = tmp_path / "tasks.sqlite3"
    device_database = tmp_path / "devices.sqlite3"
    try:
        request = wait_for_approval(runtime, submission, requester.access_token)
        barrier = Barrier(2)

        def decide(approve: bool) -> str:
            connection = TaskStore(database, clock=lambda: now[0])
            credentials = DeviceStore(device_database, clock=lambda: now[0])
            credential_service = DeviceService(
                credentials,
                bootstrap_code=None,
                clock=lambda: now[0],
            )
            try:
                barrier.wait()
                with credential_service.reserve_authenticated_owner_credential(
                    owner.access_token
                ) as current_owner:
                    return connection.decide_approval_request(
                        request.request_id,
                        owner_device_id=current_owner.id,
                        owner_credential_generation=current_owner.credential_generation,
                        action_digest=request.action_digest,
                        tool_definition_digest=request.tool_definition_digest,
                        approve=approve,
                        precommit_validation=lambda: credential_service.validate_reserved_owner_generation(
                            current_owner.id,
                            current_owner.credential_generation,
                            access_token=owner.access_token,
                        ),
                    ).status.value
            except InvalidTransitionError:
                return "lost"
            finally:
                connection.close()
                credentials.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = tuple(pool.map(decide, (first_decision, not first_decision)))
        assert sum(outcome in {"approved", "denied"} for outcome in outcomes) == 1
        assert outcomes.count("lost") == 1
        assert len(store.list_approval_audit_events(request.request_id)) == 2
        assert store.get_approval_request(request.request_id).status in {
            ApprovalStatus.APPROVED,
            ApprovalStatus.DENIED,
        }
        assert executor.calls == 0
    finally:
        store.close()
        device_store.close()


def test_competing_resumes_create_one_attempt_and_dispatch(tmp_path) -> None:
    device_store, store, _service, runtime, owner, requester, submission, executor, _grants, _now = (
        build_approval_runtime(tmp_path)
    )
    try:
        request = wait_for_approval(runtime, submission, requester.access_token)
        runtime.decide_approval(request.request_id, owner.access_token, approve=True)

        async def resume_pair():
            return await asyncio.gather(
                runtime.resume_approved_task_async(
                    submission.plan.task.id,
                    requester.access_token,
                ),
                runtime.resume_approved_task_async(
                    submission.plan.task.id,
                    requester.access_token,
                ),
                return_exceptions=True,
            )

        results = asyncio.run(resume_pair())
        assert sum(not isinstance(result, Exception) for result in results) == 1
        attempts = store.list_attempts(submission.plan.steps[0].id)
        assert len(attempts) == 1
        assert sum(attempt.dispatch_claimed_at is not None for attempt in attempts) == 1
        assert store.get_approval_request(request.request_id).status is ApprovalStatus.CONSUMED
        assert executor.calls == 1
    finally:
        store.close()
        device_store.close()


def test_waiting_state_audit_failure_rolls_back_request_and_state(tmp_path) -> None:
    device_store, store, _service, runtime, _owner, requester, submission, executor, _grants, _now = (
        build_approval_runtime(tmp_path)
    )
    try:
        store._connection.execute(
            """CREATE TRIGGER reject_waiting_state_event
                BEFORE INSERT ON task_events WHEN NEW.to_state = 'waiting_approval'
                BEGIN SELECT RAISE(ABORT, 'injected waiting-state event failure'); END"""
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected waiting-state event failure"):
            asyncio.run(
                runtime.run_authorized_task_async(
                    submission.plan.task.id,
                    requester.access_token,
                )
            )
        assert store.list_approval_requests(submission.plan.task.id) == ()
        assert store.get_task(submission.plan.task.id).state is TaskState.READY
        assert store.get_step(submission.plan.steps[0].id).state is StepState.PENDING
        assert store.list_approval_audit_events() == ()
        assert executor.calls == 0
    finally:
        store.close()
        device_store.close()


def test_attempt_insert_failure_rolls_back_consumption_and_claim(tmp_path) -> None:
    device_store, store, _service, runtime, owner, requester, submission, executor, _grants, _now = (
        build_approval_runtime(tmp_path)
    )
    try:
        request = wait_for_approval(runtime, submission, requester.access_token)
        runtime.decide_approval(request.request_id, owner.access_token, approve=True)
        store._connection.execute(
            """CREATE TRIGGER reject_approval_attempt
                BEFORE INSERT ON step_attempts
                BEGIN SELECT RAISE(ABORT, 'injected attempt failure'); END"""
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected attempt failure"):
            asyncio.run(
                runtime.resume_approved_task_async(
                    submission.plan.task.id,
                    requester.access_token,
                )
            )
        assert store.get_approval_request(request.request_id).status is ApprovalStatus.APPROVED
        assert store.get_task(submission.plan.task.id).state is TaskState.WAITING_APPROVAL
        assert store.get_step(submission.plan.steps[0].id).state is StepState.WAITING_APPROVAL
        assert store.list_attempts(submission.plan.steps[0].id) == []
        assert executor.calls == 0
    finally:
        store.close()
        device_store.close()


def test_dispatch_claim_failure_rolls_back_consumption_and_preexecution_state(tmp_path) -> None:
    device_store, store, _service, runtime, owner, requester, submission, executor, _grants, _now = (
        build_approval_runtime(tmp_path)
    )
    try:
        request = wait_for_approval(runtime, submission, requester.access_token)
        runtime.decide_approval(request.request_id, owner.access_token, approve=True)
        store._connection.execute(
            """CREATE TRIGGER reject_approval_dispatch_claim
                BEFORE INSERT ON step_attempts WHEN NEW.dispatch_claimed_at IS NOT NULL
                BEGIN SELECT RAISE(ABORT, 'injected dispatch claim failure'); END"""
        )

        with pytest.raises(sqlite3.IntegrityError, match="injected dispatch claim failure"):
            asyncio.run(
                runtime.resume_approved_task_async(
                    submission.plan.task.id,
                    requester.access_token,
                )
            )

        assert store.get_approval_request(request.request_id).status is ApprovalStatus.APPROVED
        assert store.get_task(submission.plan.task.id).state is TaskState.WAITING_APPROVAL
        assert store.get_step(submission.plan.steps[0].id).state is StepState.WAITING_APPROVAL
        assert store.list_attempts(submission.plan.steps[0].id) == []
        assert executor.calls == 0
    finally:
        store.close()
        device_store.close()


def test_crash_after_consumption_requires_reconciliation_without_replay(tmp_path) -> None:
    device_store, store, service, runtime, owner, requester, submission, executor, _grants, _now = (
        build_approval_runtime(tmp_path)
    )
    try:
        request = wait_for_approval(runtime, submission, requester.access_token)
        runtime.decide_approval(request.request_id, owner.access_token, approve=True)
        step = store.get_step(submission.plan.steps[0].id)
        authorization, definition = runtime.authorized_runner.inspect_approval_action(
            submission.plan.task.id,
            step,
            requester.access_token,
        )
        current_owner = service.get_active_owner()
        with service.reserve_owner_generation(
            current_owner.id, current_owner.credential_generation
        ):
            attempt = store.consume_approval_and_start_attempt(
                request.request_id,
                requesting_device_id=requester.device.id,
                owner_credential_generation=current_owner.credential_generation,
                action_digest=step.action.digest(),
                tool_definition_digest=definition.definition_digest(),
                authorization=AttemptAuthorization(
                    device_id=authorization.device_id,
                    action_digest=authorization.action_digest,
                    authorization_fingerprint=authorization.fingerprint,
                ),
                precommit_validation=lambda: service.validate_reserved_owner_generation(
                    current_owner.id, current_owner.credential_generation
                ),
            )
        assert attempt.dispatch_claimed_at is not None

        recovered = store.recover_interrupted_attempt(
            step.id,
            allow_idempotent_replay=True,
        )
        assert recovered.state is StepState.RECONCILIATION_REQUIRED
        assert store.list_attempts(step.id)[0].outcome.value == "outcome_unknown"
        assert store.get_approval_request(request.request_id).status is ApprovalStatus.CONSUMED
        assert executor.calls == 0
    finally:
        store.close()
        device_store.close()


def test_restart_preserves_approved_request_and_stale_action_fails_closed(tmp_path) -> None:
    device_store, store, _service, runtime, owner, requester, submission, executor, _grants, _now = (
        build_approval_runtime(tmp_path)
    )
    database = tmp_path / "tasks.sqlite3"
    active_store = store
    try:
        request = wait_for_approval(runtime, submission, requester.access_token)
        runtime.decide_approval(request.request_id, owner.access_token, approve=True)
        store.close()
        reopened = TaskStore(database, clock=lambda: _now[0])
        active_store = reopened
        runtime.store = reopened
        runtime.authorized_runner.store = reopened
        assert reopened.get_approval_request(request.request_id).status is ApprovalStatus.APPROVED
        assert reopened.list_approval_audit_events(request.request_id)[0].event_type is ApprovalAuditEventType.REQUESTED
        reopened._connection.execute(
            "UPDATE steps SET action_json = ? WHERE id = ?",
            (
                json.dumps({"tool_name": "jarvis.status", "arguments": {"unexpected": True}}),
                submission.plan.steps[0].id,
            ),
        )
        with pytest.raises(InvalidTransitionError):
            asyncio.run(
                runtime.resume_approved_task_async(
                    submission.plan.task.id,
                    requester.access_token,
                )
            )
        assert active_store.get_approval_request(request.request_id).status is ApprovalStatus.REVOKED
        assert active_store.get_task(submission.plan.task.id).state is TaskState.FAILED
        assert executor.calls == 0
    finally:
        active_store.close()
        device_store.close()


def test_rotation_after_approval_revokes_resume_without_execution(tmp_path) -> None:
    device_store, store, service, runtime, owner, requester, submission, executor, _grants, _now = (
        build_approval_runtime(tmp_path, requester_is_companion=True)
    )
    try:
        request = wait_for_approval(runtime, submission, requester.access_token)
        runtime.decide_approval(request.request_id, owner.access_token, approve=True)
        service.rotate_owner_credential(owner.access_token)

        with pytest.raises(InvalidTransitionError):
            asyncio.run(
                runtime.resume_approved_task_async(
                    submission.plan.task.id,
                    requester.access_token,
                )
            )

        assert store.get_approval_request(request.request_id).status is ApprovalStatus.REVOKED
        assert store.get_task(submission.plan.task.id).state is TaskState.FAILED
        assert store.list_attempts(submission.plan.steps[0].id) == []
        assert executor.calls == 0
    finally:
        store.close()
        device_store.close()


def test_revoked_task_owner_before_resume_reservation_cannot_consume_approval(
    tmp_path, monkeypatch
) -> None:
    (
        device_store,
        store,
        service,
        runtime,
        approval_owner,
        task_owner,
        submission,
        executor,
        _grants,
        now,
    ) = build_approval_runtime(tmp_path, requester_is_companion=True)
    other_store = DeviceStore(tmp_path / "devices.sqlite3", clock=lambda: now[0])
    other_service = DeviceService(other_store, bootstrap_code=None, clock=lambda: now[0])
    try:
        assert task_owner.device.id != approval_owner.device.id
        request = wait_for_approval(runtime, submission, task_owner.access_token)
        runtime.decide_approval(request.request_id, approval_owner.access_token, approve=True)
        reserve = service.reserve_approval_resume

        def revoke_then_reserve(*args, **kwargs):
            other_service.revoke_device(
                approval_owner.access_token, task_owner.device.id
            )
            return reserve(*args, **kwargs)

        monkeypatch.setattr(service, "reserve_approval_resume", revoke_then_reserve)
        with pytest.raises(InvalidTransitionError):
            asyncio.run(
                runtime.resume_approved_task_async(
                    submission.plan.task.id,
                    task_owner.access_token,
                )
            )

        assert store.get_approval_request(request.request_id).status is ApprovalStatus.REVOKED
        assert all(
            event.event_type is not ApprovalAuditEventType.CONSUMED
            for event in store.list_approval_audit_events(request.request_id)
        )
        assert store.list_attempts(submission.plan.steps[0].id) == []
        assert executor.calls == 0
    finally:
        store.close()
        other_store.close()
        device_store.close()


def test_task_owner_revocation_racing_resume_serializes_after_consumption(
    tmp_path, monkeypatch
) -> None:
    (
        device_store,
        store,
        service,
        runtime,
        approval_owner,
        task_owner,
        submission,
        executor,
        _grants,
        now,
    ) = build_approval_runtime(tmp_path, requester_is_companion=True)
    device_database = tmp_path / "devices.sqlite3"
    try:
        assert task_owner.device.id != approval_owner.device.id
        request = wait_for_approval(runtime, submission, task_owner.access_token)
        runtime.decide_approval(request.request_id, approval_owner.access_token, approve=True)

        writer_entered_begin = Event()
        revocation_finished = Event()
        writer_errors: list[BaseException] = []
        validate_resume = service.validate_reserved_approval_resume

        def trace_revocation(statement: str) -> None:
            if statement.strip().upper() == "BEGIN IMMEDIATE":
                writer_entered_begin.set()

        def revoke_task_owner() -> None:
            other_store = DeviceStore(device_database, clock=lambda: now[0])
            try:
                other_store._connection.set_trace_callback(trace_revocation)
                other_service = DeviceService(
                    other_store, bootstrap_code=None, clock=lambda: now[0]
                )
                other_service.revoke_device(
                    approval_owner.access_token, task_owner.device.id
                )
            except BaseException as exc:
                writer_errors.append(exc)
            finally:
                other_store.close()
                revocation_finished.set()

        writer = Thread(target=revoke_task_owner)

        def pause_at_precommit(*args, **kwargs):
            writer.start()
            assert writer_entered_begin.wait(timeout=5)
            return validate_resume(*args, **kwargs)

        monkeypatch.setattr(service, "validate_reserved_approval_resume", pause_at_precommit)
        runner = runtime.authorized_runner
        execute_claimed = runner.execute_claimed

        async def wait_for_revocation_then_dispatch(*args, **kwargs):
            assert revocation_finished.wait(timeout=5)
            return await execute_claimed(*args, **kwargs)

        monkeypatch.setattr(runner, "execute_claimed", wait_for_revocation_then_dispatch)
        resumed = asyncio.run(
            runtime.resume_approved_task_async(
                submission.plan.task.id,
                task_owner.access_token,
            )
        )
        writer.join(timeout=5)

        assert not writer.is_alive()
        assert writer_errors == []
        assert revocation_finished.is_set()
        assert resumed.state is TaskState.FAILED
        assert store.get_approval_request(request.request_id).status is ApprovalStatus.CONSUMED
        attempts = store.list_attempts(submission.plan.steps[0].id)
        assert len(attempts) == 1
        assert attempts[0].dispatch_claimed_at is not None
        assert executor.calls == 0
        with pytest.raises(InvalidTransitionError):
            asyncio.run(
                runtime.resume_approved_task_async(
                    submission.plan.task.id,
                    task_owner.access_token,
                )
            )
        assert len(store.list_attempts(submission.plan.steps[0].id)) == 1
    finally:
        store.close()
        device_store.close()


@pytest.mark.parametrize("lifecycle_change", ["rotation", "recovery"])
def test_credential_change_waits_for_taskstore_consumption_commit(
    tmp_path, monkeypatch, lifecycle_change
) -> None:
    (
        device_store,
        store,
        service,
        runtime,
        owner,
        requester,
        submission,
        executor,
        _grants,
        now,
    ) = build_approval_runtime(tmp_path, requester_is_companion=True)
    device_database = tmp_path / "devices.sqlite3"
    try:
        request = wait_for_approval(runtime, submission, requester.access_token)
        runtime.decide_approval(request.request_id, owner.access_token, approve=True)
        writer_entered_begin = Event()
        validate_reached = Event()
        writer_errors: list[BaseException] = []
        writer: Thread
        validate_resume = service.validate_reserved_approval_resume

        def pause_at_precommit(*args, **kwargs):
            validate_reached.set()
            writer.start()
            assert writer_entered_begin.wait(timeout=5)
            return validate_resume(*args, **kwargs)

        monkeypatch.setattr(
            service,
            "validate_reserved_approval_resume",
            pause_at_precommit,
        )

        def change_owner_credential() -> None:
            other_store = DeviceStore(device_database, clock=lambda: now[0])
            other_store._connection.set_trace_callback(
                lambda statement: writer_entered_begin.set()
                if statement.strip().upper() == "BEGIN IMMEDIATE"
                else None
            )
            try:
                if lifecycle_change == "rotation":
                    other_service = DeviceService(
                        other_store,
                        bootstrap_code=None,
                        clock=lambda: now[0],
                    )
                    other_service.rotate_owner_credential(owner.access_token)
                else:
                    other_store.recover_owner_credential(
                        hashlib.sha256(b"serialized recovery replacement").hexdigest()
                    )
            except BaseException as exc:
                writer_errors.append(exc)
            finally:
                other_store.close()

        writer = Thread(target=change_owner_credential)
        resumed = asyncio.run(
            runtime.resume_approved_task_async(
                submission.plan.task.id,
                requester.access_token,
            )
        )
        writer.join(timeout=5)

        assert validate_reached.is_set()
        assert not writer.is_alive()
        assert writer_errors == []
        assert resumed.state is TaskState.COMPLETED
        assert device_store.get_device(owner.device.id).credential_generation == (
            owner.device.credential_generation + 1
        )
        assert store.get_approval_request(request.request_id).status is ApprovalStatus.CONSUMED
        attempts = store.list_attempts(submission.plan.steps[0].id)
        assert len(attempts) == 1
        assert attempts[0].dispatch_claimed_at is not None
        assert executor.calls == 1
    finally:
        store.close()
        device_store.close()


def test_rotation_before_consumption_linearization_rejects_stale_generation(tmp_path) -> None:
    device_store, store, _service, runtime, owner, requester, submission, executor, _grants, now = (
        build_approval_runtime(tmp_path, requester_is_companion=True)
    )
    other_store = DeviceStore(tmp_path / "devices.sqlite3", clock=lambda: now[0])
    other_service = DeviceService(other_store, bootstrap_code=None, clock=lambda: now[0])
    try:
        request = wait_for_approval(runtime, submission, requester.access_token)
        runtime.decide_approval(request.request_id, owner.access_token, approve=True)
        other_service.rotate_owner_credential(owner.access_token)

        with pytest.raises(InvalidTransitionError):
            asyncio.run(
                runtime.resume_approved_task_async(
                    submission.plan.task.id,
                    requester.access_token,
                )
            )

        assert store.get_approval_request(request.request_id).status is ApprovalStatus.REVOKED
        assert store.get_task(submission.plan.task.id).state is TaskState.FAILED
        assert store.list_attempts(submission.plan.steps[0].id) == []
        assert executor.calls == 0
    finally:
        store.close()
        device_store.close()
        other_store.close()


def test_rotation_after_consumption_linearization_does_not_replay(tmp_path, monkeypatch) -> None:
    device_store, store, service, runtime, owner, requester, submission, executor, _grants, _now = (
        build_approval_runtime(tmp_path, requester_is_companion=True)
    )
    try:
        request = wait_for_approval(runtime, submission, requester.access_token)
        runtime.decide_approval(request.request_id, owner.access_token, approve=True)
        runner = runtime.authorized_runner
        execute_claimed = runner.execute_claimed
        rotation_happened = False

        async def rotate_after_commit(*args, **kwargs):
            nonlocal rotation_happened
            if not rotation_happened:
                rotation_happened = True
                service.rotate_owner_credential(owner.access_token)
            return await execute_claimed(*args, **kwargs)

        monkeypatch.setattr(runner, "execute_claimed", rotate_after_commit)
        completed = asyncio.run(
            runtime.resume_approved_task_async(
                submission.plan.task.id,
                requester.access_token,
            )
        )

        assert completed.state is TaskState.COMPLETED
        assert store.get_approval_request(request.request_id).status is ApprovalStatus.CONSUMED
        assert len(store.list_attempts(submission.plan.steps[0].id)) == 1
        assert executor.calls == 1
        with pytest.raises(InvalidTransitionError):
            asyncio.run(
                runtime.resume_approved_task_async(
                    submission.plan.task.id,
                    requester.access_token,
                )
            )
        assert executor.calls == 1
    finally:
        store.close()
        device_store.close()


def test_device_lock_contention_leaves_approval_unconsumed(tmp_path, monkeypatch) -> None:
    device_store, store, _service, runtime, owner, requester, submission, executor, _grants, _now = (
        build_approval_runtime(tmp_path, requester_is_companion=True)
    )
    blocker = DeviceStore(tmp_path / "devices.sqlite3")
    blocker_started = False
    try:
        request = wait_for_approval(runtime, submission, requester.access_token)
        runtime.decide_approval(request.request_id, owner.access_token, approve=True)
        device_store._connection.execute("PRAGMA busy_timeout = 25")
        inspect = runtime.authorized_runner.inspect_stored_approval_action

        def acquire_external_writer(*args, **kwargs):
            nonlocal blocker_started
            result = inspect(*args, **kwargs)
            blocker._connection.execute("BEGIN IMMEDIATE")
            blocker_started = True
            return result

        monkeypatch.setattr(
            runtime.authorized_runner,
            "inspect_stored_approval_action",
            acquire_external_writer,
        )
        with pytest.raises(DeviceStoreError):
            asyncio.run(
                runtime.resume_approved_task_async(
                    submission.plan.task.id,
                    requester.access_token,
                )
            )

        assert blocker_started
        assert store.get_approval_request(request.request_id).status is ApprovalStatus.APPROVED
        assert store.get_task(submission.plan.task.id).state is TaskState.WAITING_APPROVAL
        assert store.list_attempts(submission.plan.steps[0].id) == []
        assert executor.calls == 0
    finally:
        if blocker._connection.in_transaction:
            blocker._connection.rollback()
        blocker.close()
        store.close()
        device_store.close()


def test_approved_request_expires_at_resume_boundary(tmp_path) -> None:
    device_store, store, _service, runtime, owner, requester, submission, executor, _grants, now = (
        build_approval_runtime(tmp_path)
    )
    try:
        request = wait_for_approval(runtime, submission, requester.access_token)
        runtime.decide_approval(request.request_id, owner.access_token, approve=True)
        now[0] = request.expires_at

        with pytest.raises(InvalidTransitionError):
            asyncio.run(
                runtime.resume_approved_task_async(
                    submission.plan.task.id,
                    requester.access_token,
                )
            )

        assert store.get_approval_request(request.request_id).status is ApprovalStatus.EXPIRED
        assert store.get_task(submission.plan.task.id).state is TaskState.FAILED
        assert store.list_attempts(submission.plan.steps[0].id) == []
        assert executor.calls == 0
    finally:
        store.close()
        device_store.close()


def test_tool_definition_version_change_revokes_approval(tmp_path) -> None:
    device_store, store, _service, runtime, owner, requester, submission, executor, _grants, _now = (
        build_approval_runtime(tmp_path)
    )
    try:
        request = wait_for_approval(runtime, submission, requester.access_token)
        runtime.decide_approval(request.request_id, owner.access_token, approve=True)
        registry = runtime.authorized_runner.dispatcher._registry
        registry._definitions["jarvis.status"] = registry.get("jarvis.status").model_copy(
            update={"version": "approval-test-v2"}
        )

        with pytest.raises(InvalidTransitionError):
            asyncio.run(
                runtime.resume_approved_task_async(
                    submission.plan.task.id,
                    requester.access_token,
                )
            )

        assert store.get_approval_request(request.request_id).status is ApprovalStatus.REVOKED
        assert store.get_task(submission.plan.task.id).state is TaskState.FAILED
        assert store.list_attempts(submission.plan.steps[0].id) == []
        assert executor.calls == 0
    finally:
        store.close()
        device_store.close()


def test_executor_invoked_but_result_persistence_fails_requires_reconciliation(tmp_path) -> None:
    device_store, store, _service, runtime, owner, requester, submission, executor, _grants, _now = (
        build_approval_runtime(tmp_path)
    )
    database = tmp_path / "tasks.sqlite3"
    active_store = store
    try:
        request = wait_for_approval(runtime, submission, requester.access_token)
        runtime.decide_approval(request.request_id, owner.access_token, approve=True)
        store._connection.execute(
            """CREATE TRIGGER reject_result_persistence
                BEFORE UPDATE OF result_json ON steps WHEN NEW.result_json IS NOT NULL
                BEGIN SELECT RAISE(ABORT, 'injected result persistence failure'); END"""
        )

        with pytest.raises(sqlite3.IntegrityError, match="injected result persistence failure"):
            asyncio.run(
                runtime.resume_approved_task_async(
                    submission.plan.task.id,
                    requester.access_token,
                )
            )

        assert executor.calls == 1
        assert store.get_approval_request(request.request_id).status is ApprovalStatus.CONSUMED
        assert store.list_attempts(submission.plan.steps[0].id)[0].outcome.value == "running"
        store.close()
        reopened = TaskStore(database)
        active_store = reopened
        recovered = reopened.recover_interrupted_attempt(
            submission.plan.steps[0].id,
            allow_idempotent_replay=True,
        )
        assert recovered.state is StepState.RECONCILIATION_REQUIRED
        assert reopened.list_attempts(submission.plan.steps[0].id)[0].outcome.value == "outcome_unknown"
        assert reopened.get_approval_request(request.request_id).status is ApprovalStatus.CONSUMED
    finally:
        active_store.close()
        device_store.close()
