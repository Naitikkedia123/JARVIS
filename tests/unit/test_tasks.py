"""Domain-model, finite-state, and dependency-graph tests."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from jarvis.tasks import (
    AttemptOutcome,
    EventMetadata,
    Goal,
    GoalState,
    InvalidDependencyError,
    InvalidTransitionError,
    ReplaySafety,
    Step,
    StepState,
    Task,
    TaskPlan,
    TaskState,
    validate_dependency_graph,
    validate_goal_transition,
    validate_step_transition,
    validate_task_transition,
)


def make_step(
    step_id: str,
    *,
    task_id: str = "task-1",
    dependencies: list[str] | None = None,
    replay_safety: ReplaySafety = ReplaySafety.PURE,
    idempotency_key: str | None = None,
) -> Step:
    return Step(
        id=step_id,
        task_id=task_id,
        title=step_id,
        dependencies=dependencies or [],
        replay_safety=replay_safety,
        idempotency_key=idempotency_key,
    )


def test_models_validate_attempt_limits_replay_keys_and_running_attempts() -> None:
    with pytest.raises(ValidationError):
        Step(id="bad-limit", task_id="task-1", title="invalid", attempt_limit=0)

    with pytest.raises(ValidationError):
        Step(
            id="too-many-attempts",
            task_id="task-1",
            title="invalid",
            attempt_limit=2,
            attempt_count=3,
        )

    with pytest.raises(ValidationError):
        make_step("idempotent-without-key", replay_safety=ReplaySafety.IDEMPOTENT)

    with pytest.raises(ValidationError):
        Step(id="running-without-attempt", task_id="task-1", title="invalid", state=StepState.RUNNING)

    assert make_step(
        "idempotent",
        replay_safety=ReplaySafety.IDEMPOTENT,
        idempotency_key="stable-key",
    ).attempt_limit == 3
    assert AttemptOutcome.OUTCOME_UNKNOWN.value == "outcome_unknown"


def test_models_require_timezone_aware_timestamps_and_normalize_to_utc() -> None:
    with pytest.raises(ValidationError):
        Goal(id="naive-goal", title="Naive", created_at=datetime(2026, 10, 18))

    local_time = datetime(2026, 10, 18, 12, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    goal = Goal(id="aware-goal", title="Aware", created_at=local_time, updated_at=local_time)

    assert goal.created_at == datetime(2026, 10, 18, 6, 30, tzinfo=UTC)
    assert goal.created_at.utcoffset() == timedelta(0)


def test_goal_task_and_step_transition_guards_accept_only_declared_edges() -> None:
    validate_goal_transition(GoalState.ACTIVE, GoalState.PAUSED)
    validate_task_transition(TaskState.READY, TaskState.RUNNING)
    validate_step_transition(StepState.PENDING, StepState.READY)

    with pytest.raises(InvalidTransitionError):
        validate_goal_transition(GoalState.COMPLETED, GoalState.ACTIVE)
    with pytest.raises(InvalidTransitionError):
        validate_task_transition(TaskState.CANCELLED, TaskState.READY)
    with pytest.raises(InvalidTransitionError):
        validate_step_transition(StepState.SUCCEEDED, StepState.READY)


def test_task_plan_validates_task_membership_and_complete_dependency_graph() -> None:
    task = Task(id="task-1", goal_id="goal-1", title="Plan")
    first = make_step("first")
    second = make_step("second", dependencies=[first.id])
    plan = TaskPlan(task=task, steps=[first, second])

    validate_dependency_graph(plan.steps, expected_task_id=task.id)

    with pytest.raises(ValidationError, match="dependency"):
        TaskPlan(task=task, steps=[make_step("missing-ref", dependencies=["absent"])])

    cycle_a = make_step("cycle-a", dependencies=["cycle-b"])
    cycle_b = make_step("cycle-b", dependencies=["cycle-a"])
    with pytest.raises(ValidationError, match="cycle"):
        TaskPlan(task=task, steps=[cycle_a, cycle_b])

    with pytest.raises(InvalidDependencyError, match="different task"):
        validate_dependency_graph(
            [make_step("foreign", task_id="task-2")], expected_task_id=task.id
        )


def test_step_rejects_duplicate_and_self_dependencies() -> None:
    with pytest.raises(ValidationError, match="unique"):
        make_step("duplicate", dependencies=["other", "other"])
    with pytest.raises(ValidationError, match="itself"):
        make_step("self", dependencies=["self"])


def test_audit_metadata_rejects_arbitrary_text_fields() -> None:
    with pytest.raises(ValidationError):
        EventMetadata.model_validate({"exception_message": "must not be persisted"})
