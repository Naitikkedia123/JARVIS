"""Device task authorization, ownership, and idempotent creation tests."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from pydantic import BaseModel, SecretStr, ValidationError

from jarvis.devices.models import Device, DevicePlatform, DeviceRole
from jarvis.tasks.action import AuthorizedTaskAction
from jarvis.tasks.authorization import (
    DispatchAuthorization,
    TaskSubmissionPolicyDecision,
    TaskSubmissionProposal,
    resolve_task_authorization,
)
from jarvis.tasks.errors import (
    EntityNotFoundError,
    TaskAuthorizationError,
    TaskStoreError,
    TaskSubmissionConflictError,
)
from jarvis.tasks.models import Step, StepResult
from jarvis.tasks.planner import DeterministicPlanner
from jarvis.tasks.runtime import TaskRuntime
from jarvis.tasks.store import TaskStore
from jarvis.tools.dispatcher import InMemoryToolAuditSink, ToolDispatcher
from jarvis.tools.errors import ApprovalRequestStoreError
from jarvis.tools.models import AuthorizationContext, ToolAccess, ToolDefinition, ToolRequest
from jarvis.tools.policy import ToolPolicy
from jarvis.tools.registry import ToolRegistry


def device(
    device_id: str = "a" * 32,
    *,
    role: DeviceRole = DeviceRole.COMPANION,
    reported_capabilities: frozenset[str] = frozenset(),
    revoked_at: datetime | None = None,
) -> Device:
    now = datetime.now(UTC)
    return Device(
        id=device_id,
        name="test device",
        platform=DevicePlatform.ANDROID,
        role=role,
        reported_capabilities=reported_capabilities,
        paired_at=now,
        last_seen_at=now,
        revoked_at=revoked_at,
    )


def proposal(
    *, key: str = "retry-key-00000001", title: str = "Summarize notes", description: str = ""
) -> TaskSubmissionProposal:
    return TaskSubmissionProposal(
        idempotency_key=key,
        title=title,
        description=description,
        action=AuthorizedTaskAction(tool_name="jarvis.status", arguments={}),
    )


class AllowPolicy:
    def __init__(self, capabilities: frozenset[str] = frozenset()) -> None:
        self.capabilities = capabilities
        self.calls: list[tuple[str, TaskSubmissionProposal]] = []

    def authorize(
        self, *, device_id: str, proposal: TaskSubmissionProposal
    ) -> TaskSubmissionPolicyDecision:
        self.calls.append((device_id, proposal))
        return TaskSubmissionPolicyDecision(
            allowed=True,
            authorization_context=AuthorizationContext(
                principal_id=device_id,
                granted_capabilities=self.capabilities,
            ),
        )


class DenyPolicy:
    def authorize(
        self, *, device_id: str, proposal: TaskSubmissionProposal
    ) -> TaskSubmissionPolicyDecision:
        del device_id, proposal
        return TaskSubmissionPolicyDecision(allowed=False)


class FailingPolicy:
    def authorize(
        self, *, device_id: str, proposal: TaskSubmissionProposal
    ) -> TaskSubmissionPolicyDecision:
        del device_id, proposal
        raise RuntimeError("private policy backend detail")


class AuthorizedRunner:
    deduplicates_idempotency_keys = True

    def __init__(self) -> None:
        self.authorized_calls: list[tuple[Step, object]] = []
        self.legacy_calls: list[Step] = []

    def execute(self, step: Step, *, idempotency_key: str | None) -> StepResult:
        del idempotency_key
        self.legacy_calls.append(step)
        return StepResult(data={"legacy": True})

    def execute_authorized(
        self,
        step: Step,
        *,
        idempotency_key: str | None,
        authorization,
        memory_context,
    ) -> StepResult:
        del idempotency_key, memory_context
        self.authorized_calls.append((step, authorization))
        return StepResult(data={"authorized": True})


def runtime_for(
    tmp_path, runner=None, *, authorized_runner=None
) -> tuple[TaskStore, TaskRuntime, object]:
    store = TaskStore(tmp_path / "task-authorization.sqlite3")
    actual_runner = runner or AuthorizedRunner()
    runtime = TaskRuntime(
        store,
        DeterministicPlanner(),
        actual_runner,
        authorized_runner=authorized_runner,
    )
    return store, runtime, actual_runner


class AsyncAuthorizationRunner:
    """Small token-checking harness for the runtime's async authorization boundary."""

    def __init__(self, store: TaskStore, device_id: str, token: str) -> None:
        self.store = store
        self.device_id = device_id
        self.token = token
        self.authorizations: list[DispatchAuthorization] = []
        self.executions = 0

    def authorize(self, task_id, step, access_token) -> DispatchAuthorization:
        if (
            not isinstance(access_token, SecretStr)
            or access_token.get_secret_value() != self.token
            or step.action is None
        ):
            raise TaskAuthorizationError("Task execution is not authorized.")
        authorization = DispatchAuthorization(
            device_id=self.device_id,
            action_digest=step.action.digest(),
            decision_id="unit-test-grant",
            fingerprint="f" * 64,
            tool_authorization=AuthorizationContext(
                principal_id=self.device_id,
                granted_capabilities=frozenset({"system:status:read"}),
            ),
        )
        self.authorizations.append(authorization)
        return authorization

    async def execute_claimed(self, task_id, step, access_token, binding) -> StepResult:
        assert access_token.get_secret_value() == self.token
        self.store.claim_dispatch(binding)
        self.executions += 1
        return StepResult(data={"state": "responding"})


def test_policy_decision_is_required_and_device_metadata_never_grants() -> None:
    companion_with_claimed_write = device(
        reported_capabilities=frozenset({"tool:write", "workspace:write"})
    )
    with pytest.raises(TaskAuthorizationError):
        resolve_task_authorization(companion_with_claimed_write, proposal(), DenyPolicy())

    granted = resolve_task_authorization(
        companion_with_claimed_write,
        proposal(),
        AllowPolicy(),
    )
    assert granted.device_id == companion_with_claimed_write.id
    assert granted.tool_authorization.principal_id == companion_with_claimed_write.id
    assert granted.tool_authorization.granted_capabilities == frozenset()

    owner_without_policy_grant = device("b" * 32, role=DeviceRole.OWNER)
    with pytest.raises(TaskAuthorizationError):
        resolve_task_authorization(owner_without_policy_grant, proposal(), DenyPolicy())


@pytest.mark.parametrize(
    "policy",
    [None, FailingPolicy(), DenyPolicy()],
)
def test_missing_denied_or_failing_policy_creates_no_task(tmp_path, policy) -> None:
    store, runtime, _runner = runtime_for(tmp_path)
    with pytest.raises(TaskAuthorizationError) as raised:
        runtime.submit_device_task(device(), proposal(), policy=policy)
    assert "private policy backend detail" not in str(raised.value)
    assert store._connection.execute("SELECT count(*) FROM goals").fetchone()[0] == 0
    assert store._connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    store.close()


def test_resolved_authorization_context_cannot_be_mutated() -> None:
    context = resolve_task_authorization(device(), proposal(), AllowPolicy())

    with pytest.raises(ValidationError):
        context.device_id = "f" * 32
    with pytest.raises(ValidationError):
        context.tool_authorization.principal_id = "f" * 32
    with pytest.raises(AttributeError):
        context.tool_authorization.granted_capabilities.add("tool:write")

    assert context.device_id == "a" * 32
    assert context.tool_authorization.principal_id == "a" * 32
    assert context.tool_authorization.granted_capabilities == frozenset()


def test_policy_context_must_be_bound_to_authenticated_device(tmp_path) -> None:
    class MissingContextPolicy:
        def authorize(self, *, device_id: str, proposal: TaskSubmissionProposal):
            del device_id, proposal
            return TaskSubmissionPolicyDecision(allowed=True)

    class MismatchedPolicy:
        def authorize(self, *, device_id: str, proposal: TaskSubmissionProposal):
            del device_id, proposal
            return TaskSubmissionPolicyDecision(
                allowed=True,
                authorization_context=AuthorizationContext(
                    principal_id="c" * 32,
                    granted_capabilities=frozenset({"tool:write"}),
                ),
            )

    store, runtime, _runner = runtime_for(tmp_path)
    with pytest.raises(TaskAuthorizationError):
        runtime.submit_device_task(device(), proposal(), policy=MissingContextPolicy())
    with pytest.raises(TaskAuthorizationError):
        runtime.submit_device_task(device(), proposal(), policy=MismatchedPolicy())
    assert store._connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    store.close()


def test_idempotent_retries_return_original_task_and_conflicts_fail(tmp_path) -> None:
    store, runtime, runner = runtime_for(tmp_path)
    policy = AllowPolicy()
    paired = device()
    first = runtime.submit_device_task(paired, proposal(), policy=policy)
    retry = runtime.submit_device_task(paired, proposal(), policy=policy)

    assert first.created is True
    assert retry.created is False
    assert retry.plan == first.plan
    assert runner.authorized_calls == []
    assert store._connection.execute("SELECT count(*) FROM goals").fetchone()[0] == 1

    with pytest.raises(TaskSubmissionConflictError):
        runtime.submit_device_task(
            paired,
            proposal(title="Different task"),
            policy=policy,
        )

    other_device = device("d" * 32)
    separate = runtime.submit_device_task(other_device, proposal(), policy=policy)
    assert separate.created is True
    assert separate.plan.task.id != first.plan.task.id
    store.close()

    reopened, recovered_runtime, _ = runtime_for(tmp_path)
    recovered = recovered_runtime.submit_device_task(paired, proposal(), policy=policy)
    assert recovered.created is False
    assert recovered.plan == first.plan
    reopened.close()


def test_concurrent_same_device_submissions_create_one_task(tmp_path) -> None:
    database = tmp_path / "concurrent-task-submission.sqlite3"
    bootstrap = TaskStore(database)
    bootstrap.close()
    planner_barrier = Barrier(2)

    class SynchronizedPlanner:
        version = DeterministicPlanner.version

        def plan(self, goal, *, action=None):
            plan = DeterministicPlanner().plan(goal, action=action)
            planner_barrier.wait(timeout=5)
            return plan

    def submit_once():
        store = TaskStore(database)
        try:
            runtime = TaskRuntime(store, SynchronizedPlanner(), AuthorizedRunner())
            return runtime.submit_device_task(
                device(), proposal(), policy=AllowPolicy()
            )
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: submit_once(), range(2)))

    assert sorted(result.created for result in results) == [False, True]
    assert results[0].plan == results[1].plan
    reopened = TaskStore(database)
    assert reopened._connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
    assert reopened._connection.execute("SELECT count(*) FROM task_submissions").fetchone()[0] == 1
    reopened.close()


def test_submission_ownership_is_transactional_and_status_lookup_is_scoped(tmp_path) -> None:
    database = tmp_path / "task-authorization.sqlite3"
    store, runtime, _runner = runtime_for(tmp_path)
    owner = device()
    submission = runtime.submit_device_task(owner, proposal(), policy=AllowPolicy())
    task_id = submission.plan.task.id

    assert store.get_task_for_device(task_id, owner.id) == submission.plan.task
    assert store.get_task_plan_for_device(task_id, owner.id) == submission.plan
    for unknown_id, requesting_device in ((task_id, "e" * 32), ("unknown", owner.id)):
        with pytest.raises(EntityNotFoundError, match="Task was not found"):
            store.get_task_for_device(unknown_id, requesting_device)

    store.close()
    reopened = TaskStore(database)
    assert reopened.get_task_for_device(task_id, owner.id) == submission.plan.task
    with pytest.raises(EntityNotFoundError):
        reopened.get_task_for_device(task_id, "e" * 32)
    reopened.close()


def test_failed_ownership_persistence_rolls_back_goal_plan_and_events(tmp_path) -> None:
    store, runtime, _runner = runtime_for(tmp_path)
    store._connection.execute(
        """CREATE TRIGGER fail_task_submission BEFORE INSERT ON task_submissions
            BEGIN SELECT RAISE(ABORT, 'private sqlite detail'); END"""
    )

    with pytest.raises(TaskStoreError) as raised:
        runtime.submit_device_task(device(), proposal(), policy=AllowPolicy())
    assert "private sqlite detail" not in str(raised.value)
    for table in ("goals", "tasks", "steps", "task_events", "task_submissions"):
        assert store._connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    store.close()


def test_device_owned_task_requires_fresh_async_authorization(tmp_path) -> None:
    paired = device()
    token = "test-owner-access-token"
    store = TaskStore(tmp_path / "task-authorization.sqlite3")
    runner = AsyncAuthorizationRunner(store, paired.id, token)
    runtime = TaskRuntime(
        store,
        DeterministicPlanner(),
        AuthorizedRunner(),
        authorized_runner=runner,
    )
    submission = runtime.submit_device_task(paired, proposal(), policy=AllowPolicy())
    task_id = submission.plan.task.id

    with pytest.raises(TaskAuthorizationError):
        runtime.run_task(task_id)
    for operation in (runtime.pause_task, runtime.resume_task, runtime.cancel_task):
        with pytest.raises(TaskAuthorizationError):
            operation(task_id)
    with pytest.raises(TaskAuthorizationError):
        runtime.run_authorized_task(
            task_id,
            device("f" * 32),
            policy=AllowPolicy(),
        )
    assert runner.authorizations == []
    assert store.get_task(task_id).state == "ready"

    with pytest.raises(TaskAuthorizationError):
        asyncio.run(runtime.run_authorized_task_async(task_id, SecretStr("wrong-token")))
    assert runner.executions == 0
    assert store.list_attempts(submission.plan.steps[0].id) == []

    completed = asyncio.run(
        runtime.run_authorized_task_async(task_id, SecretStr(token))
    )
    assert completed.state == "completed"
    assert runner.executions == 1
    assert runner.authorizations[0].device_id == paired.id
    store.close()


def test_missing_approval_service_blocks_tool_execution() -> None:
    class Input(BaseModel):
        value: str

    class Output(BaseModel):
        accepted: bool

    class Executor:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, arguments: BaseModel) -> object:
            del arguments
            self.calls += 1
            return {"accepted": True}

    definition = ToolDefinition(
        name="report.publish",
        description="Publish a reviewed report.",
        input_schema=Input,
        output_schema=Output,
        required_capabilities=frozenset({"report:publish"}),
        access=ToolAccess.WRITE,
        requires_approval=True,
        approval_summary="Publish the reviewed report.",
    )
    registry = ToolRegistry()
    registry.register(definition)
    executor = Executor()
    dispatcher = ToolDispatcher(
        registry,
        ToolPolicy(),
        {definition.name: executor},
        InMemoryToolAuditSink(),
    )

    with pytest.raises(ApprovalRequestStoreError):
        asyncio.run(
            dispatcher.dispatch(
                ToolRequest(tool_name=definition.name, arguments={"value": "untrusted"}),
                authorization_context=AuthorizationContext(
                    principal_id="a" * 32,
                    granted_capabilities=frozenset({"report:publish", "tool:write"}),
                ),
            )
        )
    assert executor.calls == 0


def test_authorized_device_task_fails_before_attempt_without_authorized_runner(tmp_path) -> None:
    class LegacyOnlyRunner:
        deduplicates_idempotency_keys = True

        def __init__(self) -> None:
            self.calls = 0

        def execute(self, step: Step, *, idempotency_key: str | None) -> StepResult:
            del step, idempotency_key
            self.calls += 1
            return StepResult(data={"unexpected": True})

    legacy_runner = LegacyOnlyRunner()
    store, runtime, _ = runtime_for(tmp_path, legacy_runner)
    paired = device()
    plan = runtime.submit_device_task(paired, proposal(), policy=AllowPolicy()).plan

    with pytest.raises(TaskAuthorizationError):
        asyncio.run(
            runtime.run_authorized_task_async(plan.task.id, SecretStr("test-token"))
        )

    assert legacy_runner.calls == 0
    assert store.get_task(plan.task.id).state == "ready"
    assert store.list_attempts(plan.steps[0].id) == []
    store.close()


def test_revoked_device_and_invalid_proposal_fail_closed(tmp_path) -> None:
    store, runtime, _runner = runtime_for(tmp_path)
    now = datetime.now(UTC)
    revoked = Device(
        id="a" * 32,
        name="revoked device",
        platform=DevicePlatform.ANDROID,
        role=DeviceRole.COMPANION,
        reported_capabilities=frozenset(),
        paired_at=now - timedelta(minutes=1),
        last_seen_at=now - timedelta(minutes=1),
        revoked_at=now,
    )
    with pytest.raises(TaskAuthorizationError):
        runtime.submit_device_task(revoked, proposal(), policy=AllowPolicy())
    with pytest.raises(ValidationError):
        TaskSubmissionProposal(idempotency_key="short", title="task")
    assert store._connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    store.close()


def test_async_execution_persists_action_bound_authorization(tmp_path) -> None:
    paired = device()
    token = "action-bound-access-token"
    store = TaskStore(tmp_path / "task-authorization.sqlite3")
    runner = AsyncAuthorizationRunner(store, paired.id, token)
    runtime = TaskRuntime(
        store,
        DeterministicPlanner(),
        authorized_runner=runner,
    )
    submission = runtime.submit_device_task(paired, proposal(), policy=AllowPolicy())

    result = asyncio.run(
        runtime.run_authorized_task_async(
            submission.plan.task.id,
            SecretStr(token),
        )
    )

    attempt = store.list_attempts(submission.plan.steps[0].id)[0]
    assert result.state == "completed"
    assert runner.executions == 1
    assert attempt.authorization_binding is not None
    assert attempt.authorization_binding.device_id == paired.id
    assert attempt.authorization_binding.action_digest == submission.plan.steps[0].action.digest()
    assert attempt.authorization_binding.authorization_fingerprint == "f" * 64
    store.close()
