"""Single-process task execution with bounded attempts and conservative recovery."""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Protocol
from uuid import UUID, uuid5

from jarvis.devices.models import Device
from jarvis.memory.context import MemoryContextAssembler, TaskMemoryContext
from jarvis.memory.errors import MemoryError
from jarvis.memory.models import RetrievalContext
from jarvis.tasks.authorization import (
    TaskAuthorizationContext,
    TaskOwnership,
    TaskSubmissionPolicy,
    TaskSubmissionProposal,
    TaskSubmissionReceipt,
    resolve_task_authorization,
)
from jarvis.tasks.errors import (
    InvalidTransitionError,
    TaskAuthorizationError,
    TaskError,
)
from jarvis.tasks.models import (
    AttemptOutcome,
    Goal,
    GoalState,
    ReconciliationOutcome,
    SafeErrorCode,
    Step,
    StepResult,
    StepState,
    Task,
    TaskPlan,
    TaskState,
)
from jarvis.tasks.planner import Planner
from jarvis.tasks.store import TaskStore


class StepRunner(Protocol):
    """Typed in-process executor; truthy capability promises stable-key deduplication."""

    deduplicates_idempotency_keys: bool

    def execute(self, step: Step, *, idempotency_key: str | None) -> StepResult: ...


class ContextAwareStepRunner(Protocol):
    """Optional runner interface for explicit delivery of untrusted memory data."""

    def execute_with_memory_context(
        self,
        step: Step,
        *,
        idempotency_key: str | None,
        memory_context: TaskMemoryContext,
    ) -> StepResult: ...


class AuthorizedStepRunner(Protocol):
    """Runner boundary for device tasks.

    Implementations must route tool requests through ``ToolDispatcher`` using
    ``authorization.tool_authorization`` unchanged. They must not derive grants
    from device roles, reported capabilities, or task content.
    """

    def execute_authorized(
        self,
        step: Step,
        *,
        idempotency_key: str | None,
        authorization: TaskAuthorizationContext,
        memory_context: TaskMemoryContext | None,
    ) -> StepResult: ...


class StepExecutionFailure(Exception):
    """A known non-applied execution failure safe to classify and bound."""

    def __init__(self, *, retryable: bool) -> None:
        self.retryable = retryable
        super().__init__("Step execution failed.")


class UncertainStepOutcomeError(TaskError):
    """Execution did not provide enough evidence to record a known outcome."""


class RuntimeBusyError(TaskError):
    """Another task is currently being dispatched in this process."""


class MemoryContextUnavailableError(TaskError):
    """Task execution cannot safely retrieve or deliver its requested memory context."""


_PROCESS_RUN_LOCK = Lock()
_CONTROL_LOCK = Lock()
_ACTIVE_TASK: tuple[str, str] | None = None
_PAUSE_REQUESTS: set[tuple[str, str]] = set()
_CANCEL_REQUESTS: set[tuple[str, str]] = set()
_DEVICE_GOAL_NAMESPACE = UUID("87526be7-1b0e-4b9a-8ea2-04e0f119f0af")


class TaskRuntime:
    """Coordinates planning, durable state, optional memory context, and a runner."""

    def __init__(
        self,
        store: TaskStore,
        planner: Planner,
        runner: StepRunner,
        *,
        memory_context_assembler: MemoryContextAssembler | None = None,
    ) -> None:
        self.store = store
        self.planner = planner
        self.runner = runner
        self.memory_context_assembler = memory_context_assembler
        self._database_key = str(Path(store.db_path).resolve())

    def create_task(self, goal_id: str) -> TaskPlan:
        goal = self.store.get_goal(goal_id)
        if goal.state is not GoalState.ACTIVE:
            raise InvalidTransitionError("Only active goals can be planned.")
        plan = self.planner.plan(goal)
        if plan.task.planner_version != self.planner.version:
            plan.task.planner_version = self.planner.version
        return self.store.add_task_plan(plan.task, plan.steps)

    def submit_device_task(
        self,
        device: Device,
        proposal: TaskSubmissionProposal,
        *,
        policy: TaskSubmissionPolicy | None,
    ) -> TaskSubmissionReceipt:
        """Authorize and durably create an owned task without executing it."""
        resolve_task_authorization(device, proposal, policy)
        ownership = TaskOwnership(
            device_id=device.id,
            idempotency_key=proposal.idempotency_key,
            request_digest=proposal.request_digest(),
        )
        existing = self.store.find_device_task_submission(ownership)
        if existing is not None:
            return TaskSubmissionReceipt(
                plan=existing,
                created=False,
            )
        goal_id = uuid5(
            _DEVICE_GOAL_NAMESPACE,
            f"{device.id}:{proposal.idempotency_key}",
        ).hex
        goal = Goal(
            id=goal_id,
            title=proposal.title,
            description=proposal.description,
        )
        plan = self.planner.plan(goal)
        if plan.task.planner_version != self.planner.version:
            plan.task.planner_version = self.planner.version
        persisted_plan, created = self.store.add_device_task_submission(
            goal=goal,
            plan=plan,
            ownership=ownership,
        )
        return TaskSubmissionReceipt(
            plan=persisted_plan,
            created=created,
        )

    def pause_task(self, task_id: str) -> Task:
        self._require_unowned_task(task_id)
        request_key = (self._database_key, task_id)
        with _CONTROL_LOCK:
            if _ACTIVE_TASK == request_key:
                _PAUSE_REQUESTS.add(request_key)
                return self.store.get_task(task_id)
        task = self.store.get_task(task_id)
        if task.state in (TaskState.READY, TaskState.RUNNING):
            return self.store.transition_task(task_id, TaskState.PAUSED)
        if task.state is TaskState.PAUSED:
            return task
        raise InvalidTransitionError("Only ready or running tasks can be paused.")

    def resume_task(
        self, task_id: str, *, memory_access: RetrievalContext | None = None
    ) -> Task:
        self._require_unowned_task(task_id)
        self._require_context_runner()
        task = self.store.get_task(task_id)
        if task.state is TaskState.PAUSED:
            self.store.transition_task(task_id, TaskState.READY)
        return self.run_task(task_id, memory_access=memory_access)

    def cancel_task(self, task_id: str) -> Task:
        self._require_unowned_task(task_id)
        request_key = (self._database_key, task_id)
        with _CONTROL_LOCK:
            if _ACTIVE_TASK == request_key:
                _CANCEL_REQUESTS.add(request_key)
                return self.store.get_task(task_id)
        task = self.store.get_task(task_id)
        if task.state in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED):
            return task
        if task.state is TaskState.RUNNING:
            self._recover_running_steps(task_id)
            task = self.store.get_task(task_id)
        if task.state is TaskState.RECONCILIATION_REQUIRED:
            return task
        if task.state is TaskState.CANCELLING:
            return self._finish_cancellation(task_id)
        if task.state is not TaskState.CANCELLING:
            if task.state is TaskState.READY:
                task = self.store.transition_task(task_id, TaskState.CANCELLED)
                self._cancel_unstarted_steps(task_id)
                return task
            task = self.store.transition_task(task_id, TaskState.CANCELLING)
        self._cancel_unstarted_steps(task_id)
        return self.store.transition_task(task_id, TaskState.CANCELLED)

    def run_task(
        self, task_id: str, *, memory_access: RetrievalContext | None = None
    ) -> Task:
        return self._run_task(task_id, memory_access=memory_access, authorization=None)

    def run_authorized_task(
        self,
        task_id: str,
        device: Device,
        *,
        policy: TaskSubmissionPolicy | None,
        memory_access: RetrievalContext | None = None,
    ) -> Task:
        """Re-authorize the owning device before execution and dispatch handoff."""
        ownership = self.store.get_task_ownership(task_id)
        if ownership is None or ownership.device_id != device.id:
            raise TaskAuthorizationError("Task execution is not authorized.")
        task = self.store.get_task(task_id)
        goal = self.store.get_goal(task.goal_id)
        proposal = TaskSubmissionProposal(
            idempotency_key=ownership.idempotency_key,
            title=goal.title,
            description=goal.description,
        )
        authorization = resolve_task_authorization(device, proposal, policy)
        if authorization.request_digest != ownership.request_digest:
            raise TaskAuthorizationError("Task execution is not authorized.")
        if not callable(getattr(self.runner, "execute_authorized", None)):
            raise TaskAuthorizationError("Authorized task execution is unavailable.")
        return self._run_task(
            task_id,
            memory_access=memory_access,
            authorization=authorization,
        )

    def _run_task(
        self,
        task_id: str,
        *,
        memory_access: RetrievalContext | None,
        authorization: TaskAuthorizationContext | None,
    ) -> Task:
        global _ACTIVE_TASK
        if self.memory_context_assembler is None and memory_access is not None:
            raise MemoryContextUnavailableError("Task memory context is not configured.")
        ownership = self.store.get_task_ownership(task_id)
        if ownership is not None and (
            authorization is None
            or ownership.device_id != authorization.device_id
            or ownership.idempotency_key != authorization.idempotency_key
            or ownership.request_digest != authorization.request_digest
        ):
            raise TaskAuthorizationError("Task execution is not authorized.")
        self._require_context_runner(authorized=authorization is not None)
        if not _PROCESS_RUN_LOCK.acquire(blocking=False):
            raise RuntimeBusyError("A task is already being dispatched in this process.")
        request_key = (self._database_key, task_id)
        with _CONTROL_LOCK:
            _ACTIVE_TASK = request_key
        try:
            memory_context = self._assemble_task_context(task_id, memory_access)
            return self._run_task_locked(task_id, memory_context, authorization)
        finally:
            with _CONTROL_LOCK:
                _ACTIVE_TASK = None
                _PAUSE_REQUESTS.discard(request_key)
                _CANCEL_REQUESTS.discard(request_key)
            _PROCESS_RUN_LOCK.release()

    def _require_context_runner(self, *, authorized: bool = False) -> None:
        method_name = "execute_authorized" if authorized else "execute_with_memory_context"
        if self.memory_context_assembler is not None and not callable(
            getattr(self.runner, method_name, None)
        ):
            raise MemoryContextUnavailableError(
                "The configured step runner cannot receive task memory context."
            )

    def _require_unowned_task(self, task_id: str) -> None:
        if self.store.get_task_ownership(task_id) is not None:
            raise TaskAuthorizationError("Task operation is not authorized.")

    def reconcile_step(
        self,
        step_id: str,
        outcome: ReconciliationOutcome,
        *,
        confirmation_id: UUID,
        result: StepResult | None = None,
    ) -> Task:
        step = self.store.get_step(step_id)
        task = self.store.get_task(step.task_id)
        self._require_unowned_task(task.id)
        if task.state is TaskState.RUNNING:
            self._recover_running_steps(task.id)
            if any(
                item.state is StepState.RECONCILIATION_REQUIRED
                for item in self.store.list_steps(task.id)
            ):
                task = self.store.transition_task(
                    task.id, TaskState.RECONCILIATION_REQUIRED
                )
        if task.state is not TaskState.RECONCILIATION_REQUIRED:
            raise InvalidTransitionError("Task is not awaiting reconciliation.")
        self.store.reconcile_step(
            step_id, outcome, confirmation_id=confirmation_id, result=result
        )
        steps = self.store.list_steps(task.id)
        if any(item.state is StepState.RECONCILIATION_REQUIRED for item in steps):
            return self.store.get_task(task.id)
        if any(item.state is StepState.FAILED for item in steps):
            return self.store.transition_task(task.id, TaskState.FAILED)
        if all(item.state is StepState.SUCCEEDED for item in steps):
            return self.store.transition_task(task.id, TaskState.COMPLETED)
        return self.store.transition_task(task.id, TaskState.READY)

    def _assemble_task_context(
        self, task_id: str, memory_access: RetrievalContext | None
    ) -> TaskMemoryContext | None:
        assembler = self.memory_context_assembler
        if assembler is None:
            return None
        task = self.store.get_task(task_id)
        if task.state not in (TaskState.READY, TaskState.RUNNING):
            return None
        try:
            goal = self.store.get_goal(task.goal_id)
            return assembler.assemble(task, goal, memory_access or RetrievalContext())
        except MemoryError as exc:
            raise MemoryContextUnavailableError(
                "Task memory context could not be retrieved."
            ) from exc

    def _run_task_locked(
        self,
        task_id: str,
        memory_context: TaskMemoryContext | None = None,
        authorization: TaskAuthorizationContext | None = None,
    ) -> Task:
        task = self.store.get_task(task_id)
        if task.state in (
            TaskState.PAUSED,
            TaskState.CANCELLED,
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.RECONCILIATION_REQUIRED,
        ):
            return task
        if task.state is TaskState.CANCELLING:
            return self._finish_cancellation(task_id)
        if task.state is TaskState.READY:
            task = self.store.transition_task(task_id, TaskState.RUNNING)

        self._recover_running_steps(task_id)
        while True:
            task = self.store.get_task(task_id)
            if task.state is TaskState.RUNNING:
                steps = self.store.list_steps(task_id)
                if any(step.state is StepState.RECONCILIATION_REQUIRED for step in steps):
                    return self.store.transition_task(task_id, TaskState.RECONCILIATION_REQUIRED)
                if any(step.state is StepState.FAILED for step in steps):
                    return self.store.transition_task(task_id, TaskState.FAILED)
                if all(step.state is StepState.SUCCEEDED for step in steps):
                    return self.store.transition_task(task_id, TaskState.COMPLETED)

                candidate = next(
                    (
                        step
                        for step in steps
                        if step.state in (StepState.PENDING, StepState.READY, StepState.RETRY_WAIT)
                        and all(
                            self.store.get_step(dependency).state is StepState.SUCCEEDED
                            for dependency in step.dependencies
                        )
                    ),
                    None,
                )
                if candidate is None:
                    return self.store.transition_task(task_id, TaskState.FAILED)
                if candidate.state in (StepState.PENDING, StepState.RETRY_WAIT):
                    candidate = self.store.transition_step(candidate.id, StepState.READY)
                self._dispatch(candidate, memory_context, authorization)
                pause, cancel = self._requested_control(task_id)
                current = self.store.get_step(candidate.id)
                if current.state is StepState.RECONCILIATION_REQUIRED:
                    return self.store.transition_task(task_id, TaskState.RECONCILIATION_REQUIRED)
                if cancel:
                    self.store.transition_task(task_id, TaskState.CANCELLING)
                    self._cancel_unstarted_steps(task_id)
                    return self.store.transition_task(task_id, TaskState.CANCELLED)
                if pause:
                    return self.store.transition_task(task_id, TaskState.PAUSED)
        return self.store.get_task(task_id)

    def _dispatch(
        self,
        step: Step,
        memory_context: TaskMemoryContext | None = None,
        authorization: TaskAuthorizationContext | None = None,
    ) -> None:
        attempt = self.store.start_step_attempt(step.id)
        if step.replay_safety.value == "idempotent" and not getattr(
            self.runner, "deduplicates_idempotency_keys", False
        ):
            self.store.finish_step_attempt(
                attempt.step_id,
                AttemptOutcome.PERMANENT_FAILURE,
                error_code=SafeErrorCode.PERMANENT,
            )
            return
        try:
            if authorization is not None:
                execute_authorized = getattr(self.runner, "execute_authorized", None)
                if not callable(execute_authorized):
                    raise TaskAuthorizationError("Authorized task execution is unavailable.")
                result = execute_authorized(
                    step,
                    idempotency_key=step.idempotency_key,
                    authorization=authorization,
                    memory_context=memory_context,
                )
            elif memory_context is None:
                result = self.runner.execute(step, idempotency_key=step.idempotency_key)
            else:
                execute_with_context = getattr(
                    self.runner, "execute_with_memory_context", None
                )
                if not callable(execute_with_context):
                    raise MemoryContextUnavailableError(
                        "The configured step runner cannot receive task memory context."
                    )
                result = execute_with_context(
                    step,
                    idempotency_key=step.idempotency_key,
                    memory_context=memory_context,
                )
            if not isinstance(result, StepResult):
                raise TypeError("Step runners must return StepResult.")
        except StepExecutionFailure as exc:
            outcome = (
                AttemptOutcome.RETRYABLE_FAILURE
                if exc.retryable
                else AttemptOutcome.PERMANENT_FAILURE
            )
            error_code = SafeErrorCode.TRANSIENT if exc.retryable else SafeErrorCode.PERMANENT
            self.store.finish_step_attempt(step.id, outcome, error_code=error_code)
        except Exception:
            safe_replay = step.replay_safety.value == "pure" or (
                step.replay_safety.value == "idempotent"
                and getattr(self.runner, "deduplicates_idempotency_keys", False) is True
            )
            self.store.finish_step_attempt(
                step.id,
                AttemptOutcome.OUTCOME_UNKNOWN,
                allow_safe_replay=safe_replay,
            )
        else:
            self.store.finish_step_attempt(step.id, AttemptOutcome.SUCCEEDED, result=result)

    def _recover_running_steps(self, task_id: str) -> None:
        allow_idempotent = getattr(self.runner, "deduplicates_idempotency_keys", False) is True
        for step in self.store.list_steps(task_id):
            if step.state is StepState.RUNNING:
                self.store.recover_interrupted_attempt(
                    step.id, allow_idempotent_replay=allow_idempotent
                )

    def _requested_control(self, task_id: str) -> tuple[bool, bool]:
        request_key = (self._database_key, task_id)
        with _CONTROL_LOCK:
            return request_key in _PAUSE_REQUESTS, request_key in _CANCEL_REQUESTS

    def _cancel_unstarted_steps(self, task_id: str) -> None:
        for step in self.store.list_steps(task_id):
            if step.state in (
                StepState.PENDING,
                StepState.READY,
                StepState.RETRY_WAIT,
                StepState.PAUSED,
            ):
                self.store.transition_step(step.id, StepState.CANCELLED)

    def _finish_cancellation(self, task_id: str) -> Task:
        self._recover_running_steps(task_id)
        steps = self.store.list_steps(task_id)
        if any(step.state is StepState.RECONCILIATION_REQUIRED for step in steps):
            return self.store.transition_task(
                task_id, TaskState.RECONCILIATION_REQUIRED
            )
        self._cancel_unstarted_steps(task_id)
        return self.store.transition_task(task_id, TaskState.CANCELLED)
