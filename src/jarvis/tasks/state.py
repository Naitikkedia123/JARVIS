"""Finite state machines and dependency validation for durable task records."""

from collections.abc import Iterable, Mapping

from jarvis.tasks.errors import InvalidDependencyError, InvalidTransitionError
from jarvis.tasks.models import (
    ApprovalStatus,
    GoalState,
    Step,
    StepState,
    TaskState,
)

GOAL_TRANSITIONS: dict[GoalState, frozenset[GoalState]] = {
    GoalState.ACTIVE: frozenset(
        {GoalState.PAUSED, GoalState.CANCELLING, GoalState.COMPLETED}
    ),
    GoalState.PAUSED: frozenset({GoalState.ACTIVE, GoalState.CANCELLING}),
    GoalState.CANCELLING: frozenset({GoalState.CANCELLED}),
    GoalState.COMPLETED: frozenset(),
    GoalState.CANCELLED: frozenset(),
}

TASK_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.READY: frozenset(
        {TaskState.RUNNING, TaskState.PAUSED, TaskState.WAITING_APPROVAL, TaskState.CANCELLED}
    ),
    TaskState.RUNNING: frozenset(
        {
            TaskState.READY,
            TaskState.PAUSED,
            TaskState.CANCELLING,
            TaskState.RECONCILIATION_REQUIRED,
            TaskState.COMPLETED,
            TaskState.FAILED,
        }
    ),
    TaskState.PAUSED: frozenset(
        {TaskState.READY, TaskState.CANCELLING, TaskState.CANCELLED}
    ),
    TaskState.WAITING_APPROVAL: frozenset(
        {TaskState.RUNNING, TaskState.FAILED}
    ),
    TaskState.CANCELLING: frozenset(
        {TaskState.CANCELLED, TaskState.RECONCILIATION_REQUIRED}
    ),
    TaskState.RECONCILIATION_REQUIRED: frozenset(
        {TaskState.READY, TaskState.COMPLETED, TaskState.FAILED}
    ),
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}

STEP_TRANSITIONS: dict[StepState, frozenset[StepState]] = {
    StepState.PENDING: frozenset(
        {StepState.READY, StepState.WAITING_APPROVAL, StepState.CANCELLED}
    ),
    StepState.READY: frozenset(
        {StepState.RUNNING, StepState.PAUSED, StepState.WAITING_APPROVAL, StepState.CANCELLED}
    ),
    StepState.RUNNING: frozenset(
        {
            StepState.SUCCEEDED,
            StepState.RETRY_WAIT,
            StepState.PAUSED,
            StepState.RECONCILIATION_REQUIRED,
            StepState.FAILED,
        }
    ),
    StepState.RETRY_WAIT: frozenset({StepState.READY, StepState.CANCELLED}),
    StepState.PAUSED: frozenset({StepState.READY, StepState.CANCELLED}),
    StepState.WAITING_APPROVAL: frozenset(
        {StepState.RUNNING, StepState.FAILED}
    ),
    StepState.RECONCILIATION_REQUIRED: frozenset(
        {StepState.SUCCEEDED, StepState.READY, StepState.FAILED}
    ),
    StepState.SUCCEEDED: frozenset(),
    StepState.FAILED: frozenset(),
    StepState.CANCELLED: frozenset(),
}

# This describes the approval lifecycle persisted by TaskStore.
APPROVAL_TRANSITIONS: dict[ApprovalStatus, frozenset[ApprovalStatus]] = {
    ApprovalStatus.PENDING: frozenset(
        {
            ApprovalStatus.APPROVED,
            ApprovalStatus.DENIED,
            ApprovalStatus.EXPIRED,
            ApprovalStatus.REVOKED,
        }
    ),
    ApprovalStatus.APPROVED: frozenset(
        {ApprovalStatus.EXPIRED, ApprovalStatus.REVOKED, ApprovalStatus.CONSUMED}
    ),
    ApprovalStatus.DENIED: frozenset(),
    ApprovalStatus.EXPIRED: frozenset(),
    ApprovalStatus.REVOKED: frozenset(),
    ApprovalStatus.CONSUMED: frozenset(),
}


def validate_goal_transition(current: GoalState, target: GoalState) -> None:
    _validate_transition("goal", current, target, GOAL_TRANSITIONS)


def validate_task_transition(current: TaskState, target: TaskState) -> None:
    _validate_transition("task", current, target, TASK_TRANSITIONS)


def validate_step_transition(current: StepState, target: StepState) -> None:
    _validate_transition("step", current, target, STEP_TRANSITIONS)


def validate_approval_transition(
    current: ApprovalStatus, target: ApprovalStatus
) -> None:
    _validate_transition("approval", current, target, APPROVAL_TRANSITIONS)


def _validate_transition(state_name: str, current: object, target: object, transitions: Mapping) -> None:
    if target not in transitions.get(current, frozenset()):
        raise InvalidTransitionError(f"Illegal {state_name} state transition.")


def validate_dependency_graph(
    steps: Iterable[Step], *, expected_task_id: str | None = None
) -> None:
    """Validate in-plan references, task ownership, and acyclicity."""
    step_list = list(steps)
    step_by_id: dict[str, Step] = {}
    for step in step_list:
        if expected_task_id is not None and step.task_id != expected_task_id:
            raise InvalidDependencyError("A plan step belongs to a different task.")
        if step.id in step_by_id:
            raise InvalidDependencyError("Step identifiers in a plan must be unique.")
        step_by_id[step.id] = step

    outgoing: dict[str, list[str]] = {step_id: [] for step_id in step_by_id}
    indegree = dict.fromkeys(step_by_id, 0)
    for step in step_list:
        if len(set(step.dependencies)) != len(step.dependencies):
            raise InvalidDependencyError("Step dependencies must be unique.")
        if step.id in step.dependencies:
            raise InvalidDependencyError("A step cannot depend on itself.")
        for dependency_id in step.dependencies:
            if dependency_id not in step_by_id:
                raise InvalidDependencyError("A dependency is missing from this task plan.")
            if step_by_id[dependency_id].task_id != step.task_id:
                raise InvalidDependencyError("Cross-task dependencies are not permitted.")
            outgoing[dependency_id].append(step.id)
            indegree[step.id] += 1

    ready = [step_id for step_id, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        step_id = ready.pop()
        visited += 1
        for dependent_id in outgoing[step_id]:
            indegree[dependent_id] -= 1
            if indegree[dependent_id] == 0:
                ready.append(dependent_id)
    if visited != len(step_by_id):
        raise InvalidDependencyError("Task plan dependencies must not contain a cycle.")
