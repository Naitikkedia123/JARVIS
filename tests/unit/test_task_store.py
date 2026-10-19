"""SQLite migration, persistence, transaction, and audit tests."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from jarvis.tasks import (
    AttemptOutcome,
    DependencyNotSatisfiedError,
    EntityNotFoundError,
    Goal,
    GoalState,
    InvalidDependencyError,
    InvalidTransitionError,
    ReconciliationOutcome,
    ReplaySafety,
    SafeErrorCode,
    Step,
    StepResult,
    StepState,
    Task,
    TaskEventType,
    TaskState,
    TaskStoreError,
)
from jarvis.tasks.store import TaskStore


def seed_task(
    store: TaskStore,
    *,
    step_id: str = "step-1",
    replay_safety: ReplaySafety = ReplaySafety.PURE,
    idempotency_key: str | None = None,
    attempt_limit: int = 3,
    dependencies: list[str] | None = None,
) -> tuple[Goal, Task, Step]:
    goal = Goal(id="goal-1", title="Validate Phase 03")
    task = Task(id="task-1", goal_id=goal.id, title="Persist a task")
    step = Step(
        id=step_id,
        task_id=task.id,
        title=step_id,
        replay_safety=replay_safety,
        idempotency_key=idempotency_key,
        attempt_limit=attempt_limit,
        dependencies=dependencies or [],
    )
    store.add_goal(goal)
    store.add_task_plan(task, [step])
    return goal, task, step


def test_migration_ledger_and_foreign_key_enforcement(tmp_path) -> None:
    database = tmp_path / "migration.sqlite3"
    store = TaskStore(database)

    assert store.schema_version == 3
    assert store._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert [
        row["version"]
        for row in store._connection.execute("SELECT version FROM schema_migrations")
    ] == [1, 2, 3]

    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute(
            """INSERT INTO tasks
                (id, goal_id, title, state, planner_version, created_at, updated_at)
                VALUES ('orphan', 'missing-goal', 'orphan', 'ready', 'test', 'x', 'x')"""
        )
    store.close()


def test_v3_migration_preserves_legacy_tasks_without_implicit_owners(tmp_path) -> None:
    database = tmp_path / "task-owner-migration.sqlite3"
    store = TaskStore(database)
    _, task, _ = seed_task(store)
    store.close()

    connection = sqlite3.connect(database)
    connection.execute("DROP TABLE task_submissions")
    connection.execute("DELETE FROM schema_migrations WHERE version = 3")
    connection.commit()
    connection.close()

    upgraded = TaskStore(database)
    assert upgraded.schema_version == 3
    assert upgraded.get_task(task.id).id == task.id
    assert upgraded.get_task_ownership(task.id) is None
    upgraded.close()


def test_database_persists_goal_plan_attempt_event_and_checkpoint_after_reopen(tmp_path) -> None:
    database = tmp_path / "durable.sqlite3"
    store = TaskStore(database)
    goal, task, step = seed_task(store)
    store.transition_step(step.id, StepState.READY)
    store.start_step_attempt(step.id)
    store.finish_step_attempt(step.id, AttemptOutcome.SUCCEEDED)
    store.close()

    reopened = TaskStore(database)
    assert reopened.get_goal(goal.id).state is GoalState.ACTIVE
    assert reopened.get_task(task.id).state is TaskState.READY
    assert reopened.get_step(step.id).state is StepState.SUCCEEDED
    assert reopened.list_attempts(step.id)[0].outcome is AttemptOutcome.SUCCEEDED
    checkpoints = reopened.list_checkpoints(task.id)
    assert len(checkpoints) == 1
    assert checkpoints[0].completed_step_ids == [step.id]
    assert any(event.event_type is TaskEventType.ATTEMPT_FINISHED for event in reopened.list_events())
    reopened.close()


def test_invalid_transition_leaves_state_and_audit_history_unchanged(tmp_path) -> None:
    store = TaskStore(tmp_path / "invalid-transition.sqlite3")
    _, _, step = seed_task(store)
    events_before = store.list_events(step_id=step.id)

    with pytest.raises(InvalidTransitionError):
        store.transition_step(step.id, StepState.SUCCEEDED)

    assert store.get_step(step.id).state is StepState.PENDING
    assert store.list_events(step_id=step.id) == events_before
    store.close()


def test_step_success_state_event_and_checkpoint_commit_atomically(tmp_path) -> None:
    store = TaskStore(tmp_path / "atomic.sqlite3")
    _, task, step = seed_task(store)
    store.transition_step(step.id, StepState.READY)
    store.start_step_attempt(step.id)
    events_before = store.list_events(step_id=step.id)
    store._connection.execute(
        """CREATE TRIGGER force_checkpoint_failure BEFORE INSERT ON task_checkpoints
            BEGIN SELECT RAISE(ABORT, 'injected checkpoint failure'); END"""
    )

    result = StepResult(data={"value": 7})
    with pytest.raises(sqlite3.IntegrityError):
        store.finish_step_attempt(step.id, AttemptOutcome.SUCCEEDED, result=result)

    assert store.get_step(step.id).state is StepState.RUNNING
    assert store.list_attempts(step.id)[0].outcome is AttemptOutcome.RUNNING
    assert store.list_events(step_id=step.id) == events_before
    assert store.list_checkpoints(task.id) == []
    assert store.get_step_result(step.id) is None

    store._connection.execute("DROP TRIGGER force_checkpoint_failure")
    store.finish_step_attempt(step.id, AttemptOutcome.SUCCEEDED, result=result)
    assert store.get_step(step.id).state is StepState.SUCCEEDED
    assert len(store.list_events(step_id=step.id)) == len(events_before) + 1
    assert len(store.list_checkpoints(task.id)) == 1
    assert store.get_step_result(step.id) == result
    store.close()


@pytest.mark.parametrize(
    "stored_json",
    [
        "{malformed stored content",
        '{"data": "not-an-object"}',
    ],
)
def test_invalid_persisted_step_result_is_normalized_safely(tmp_path, stored_json) -> None:
    store = TaskStore(tmp_path / "invalid-step-result.sqlite3")
    _, _, step = seed_task(store)
    store._connection.execute(
        "UPDATE steps SET result_json = ? WHERE id = ?", (stored_json, step.id)
    )

    with pytest.raises(TaskStoreError, match="Stored step result is invalid") as raised:
        store.get_step_result(step.id)

    assert isinstance(raised.value.__cause__, ValidationError)
    assert stored_json not in str(raised.value)
    store.close()


def test_missing_step_result_and_missing_step_keep_existing_not_found_behavior(tmp_path) -> None:
    store = TaskStore(tmp_path / "missing-step-result.sqlite3")
    _, _, step = seed_task(store)

    assert store.get_step_result(step.id) is None
    with pytest.raises(EntityNotFoundError, match="Step was not found"):
        store.get_step_result("missing-step")
    store.close()


def test_attempts_are_bounded_and_unknown_outcomes_require_reconciliation(tmp_path) -> None:
    store = TaskStore(tmp_path / "attempts.sqlite3")
    _, _, step = seed_task(store, attempt_limit=2, replay_safety=ReplaySafety.NON_IDEMPOTENT)
    store.transition_step(step.id, StepState.READY)
    first_attempt = store.start_step_attempt(step.id)
    store.finish_step_attempt(
        step.id,
        AttemptOutcome.RETRYABLE_FAILURE,
        error_code=SafeErrorCode.TRANSIENT,
    )
    assert store.get_step(step.id).state is StepState.RETRY_WAIT
    store.transition_step(step.id, StepState.READY)
    second_attempt = store.start_step_attempt(step.id)
    store.finish_step_attempt(step.id, AttemptOutcome.OUTCOME_UNKNOWN)

    assert store.get_step(step.id).state is StepState.RECONCILIATION_REQUIRED
    with pytest.raises(InvalidTransitionError):
        store.start_step_attempt(step.id)
    assert [attempt.attempt_number for attempt in store.list_attempts(step.id)] == [1, 2]
    assert first_attempt.attempt_number == 1
    assert second_attempt.attempt_number == 2

    resolved = store.reconcile_step(
        step.id,
        ReconciliationOutcome.CONFIRMED_NOT_APPLIED,
        confirmation_id=uuid4(),
    )
    assert resolved.state is StepState.FAILED
    reconciliation_event = store.list_events(step_id=step.id)[-1]
    assert reconciliation_event.event_type is TaskEventType.RECONCILED
    assert reconciliation_event.metadata.outcome is ReconciliationOutcome.CONFIRMED_NOT_APPLIED
    store.close()


def test_reconciliation_confirmation_and_unknown_attempt_survive_reopen(tmp_path) -> None:
    database = tmp_path / "reconcile.sqlite3"
    store = TaskStore(database)
    _, _, step = seed_task(store, replay_safety=ReplaySafety.NON_IDEMPOTENT)
    store.transition_step(step.id, StepState.READY)
    store.start_step_attempt(step.id)
    store.finish_step_attempt(step.id, AttemptOutcome.OUTCOME_UNKNOWN)
    confirmation_id = uuid4()
    store.reconcile_step(
        step.id,
        ReconciliationOutcome.CONFIRMED_SUCCEEDED,
        confirmation_id=confirmation_id,
    )
    store.close()

    reopened = TaskStore(database)
    assert reopened.get_step(step.id).state is StepState.SUCCEEDED
    assert reopened.list_attempts(step.id)[0].outcome is AttemptOutcome.OUTCOME_UNKNOWN
    event = reopened.list_events(step_id=step.id)[-1]
    assert event.metadata.confirmation_id == confirmation_id
    assert reopened.list_checkpoints("task-1")[0].step_id == step.id
    reopened.close()


def test_dependencies_must_exist_in_plan_and_cannot_cross_tasks(tmp_path) -> None:
    store = TaskStore(tmp_path / "dependencies.sqlite3")
    first_goal = Goal(id="goal-1", title="First")
    first_task = Task(id="task-1", goal_id=first_goal.id, title="First task")
    first_step = Step(id="step-1", task_id=first_task.id, title="First step")
    store.add_goal(first_goal)
    store.add_task_plan(first_task, [first_step])

    second_goal = Goal(id="goal-2", title="Second")
    second_task = Task(id="task-2", goal_id=second_goal.id, title="Second task")
    store.add_goal(second_goal)

    cross_task_step = Step(
        id="step-2", task_id=second_task.id, title="Cross-task", dependencies=[first_step.id]
    )
    with pytest.raises(InvalidDependencyError, match="Cross-task"):
        store.add_task_plan(second_task, [cross_task_step])

    missing_step = Step(
        id="step-3", task_id=second_task.id, title="Missing", dependencies=["not-present"]
    )
    with pytest.raises(InvalidDependencyError, match="missing"):
        store.add_task_plan(second_task, [missing_step])
    assert store.get_task(first_task.id).state is TaskState.READY
    assert [event.event_type for event in store.list_events(goal_id=second_goal.id)] == [
        TaskEventType.GOAL_CREATED
    ]
    store.close()


def test_dependency_completion_is_required_before_starting_attempt(tmp_path) -> None:
    store = TaskStore(tmp_path / "dependency-gate.sqlite3")
    goal = Goal(id="goal-1", title="Dependencies")
    task = Task(id="task-1", goal_id=goal.id, title="Dependency task")
    prerequisite = Step(id="step-a", task_id=task.id, title="A")
    dependent = Step(id="step-b", task_id=task.id, title="B", dependencies=[prerequisite.id])
    store.add_goal(goal)
    store.add_task_plan(task, [prerequisite, dependent])
    store.transition_step(prerequisite.id, StepState.READY)
    store.transition_step(dependent.id, StepState.READY)

    with pytest.raises(DependencyNotSatisfiedError):
        store.start_step_attempt(dependent.id)
    assert store.get_step(dependent.id).attempt_count == 0

    store.start_step_attempt(prerequisite.id)
    store.finish_step_attempt(prerequisite.id, AttemptOutcome.SUCCEEDED)
    store.start_step_attempt(dependent.id)
    assert store.get_step(dependent.id).state is StepState.RUNNING
    store.close()


def test_injected_clock_produces_aware_utc_events_and_updated_timestamps(tmp_path) -> None:
    fixed_time = datetime(2026, 10, 18, 12, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    store = TaskStore(tmp_path / "clock.sqlite3", clock=lambda: fixed_time)
    goal, _, step = seed_task(store)
    store.transition_step(step.id, StepState.READY)

    changed_event = store.list_events(step_id=step.id)[-1]
    updated_step = store.get_step(step.id)
    assert changed_event.occurred_at == datetime(2026, 10, 18, 7, 0, tzinfo=UTC)
    assert updated_step.updated_at == changed_event.occurred_at
    assert goal.created_at.tzinfo is not None
    store.close()


def test_task_event_records_do_not_accept_arbitrary_exception_messages(tmp_path) -> None:
    store = TaskStore(tmp_path / "safe-events.sqlite3")
    _, _, step = seed_task(store)
    store.transition_step(step.id, StepState.READY)
    store.start_step_attempt(step.id)
    store.finish_step_attempt(
        step.id,
        AttemptOutcome.PERMANENT_FAILURE,
        error_code=SafeErrorCode.PERMANENT,
    )

    stored_metadata = json.loads(
        store._connection.execute(
            "SELECT metadata_json FROM task_events WHERE step_id = ? ORDER BY id DESC LIMIT 1",
            (step.id,),
        ).fetchone()[0]
    )
    assert "exception_message" not in stored_metadata
    assert stored_metadata == {
        "attempt_number": 1,
        "error_code": "permanent",
        "outcome": "permanent_failure",
        "replay_safety": "pure",
    }
    with pytest.raises(sqlite3.IntegrityError):
        store._connection.execute("DELETE FROM task_events WHERE step_id = ?", (step.id,))
    store.close()
