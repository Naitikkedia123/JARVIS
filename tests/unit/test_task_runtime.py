"""Deterministic task-runtime scheduling, control, and retry tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from jarvis.tasks.models import (
    Goal,
    ReplaySafety,
    SafeErrorCode,
    Step,
    StepResult,
    StepState,
    Task,
    TaskPlan,
    TaskState,
)
from jarvis.tasks.planner import DeterministicPlanner
from jarvis.tasks.runtime import RuntimeBusyError, StepExecutionFailure, TaskRuntime
from jarvis.tasks.store import TaskStore


class RecordingRunner:
    deduplicates_idempotency_keys = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.failures_remaining = 0
        self.fail_permanently = False
        self.on_execute = None

    def execute(self, step: Step, *, idempotency_key: str | None) -> StepResult:
        self.calls.append((step.id, idempotency_key))
        if self.on_execute is not None:
            self.on_execute()
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise StepExecutionFailure(retryable=True)
        if self.fail_permanently:
            self.fail_permanently = False
            raise StepExecutionFailure(retryable=False)
        return StepResult(data={"step": step.id, "accepted": True})


class FixedPlanner:
    version = "test-v1"

    def __init__(
        self,
        replay_safety: ReplaySafety = ReplaySafety.PURE,
        *,
        attempt_limit: int = 3,
    ) -> None:
        self.replay_safety = replay_safety
        self.attempt_limit = attempt_limit

    def plan(self, goal: Goal) -> TaskPlan:
        task = Task(id="runtime-task", goal_id=goal.id, title="Runtime task", planner_version=self.version)
        first = Step(
            id="first",
            task_id=task.id,
            title="First",
            replay_safety=self.replay_safety,
            idempotency_key="stable-key" if self.replay_safety is ReplaySafety.IDEMPOTENT else None,
            attempt_limit=self.attempt_limit,
        )
        second = Step(id="second", task_id=task.id, title="Second", dependencies=[first.id])
        return TaskPlan(task=task, steps=[first, second])


def make_runtime(tmp_path, planner=None, runner=None):
    store = TaskStore(tmp_path / "runtime.sqlite3")
    goal = Goal(id="runtime-goal", title="Exercise runtime")
    store.add_goal(goal)
    actual_runner = runner or RecordingRunner()
    runtime = TaskRuntime(store, planner or FixedPlanner(), actual_runner)
    plan = runtime.create_task(goal.id)
    return store, runtime, actual_runner, plan


def test_deterministic_planner_returns_stable_plan_without_model_calls() -> None:
    goal = Goal(id="stable-goal", title="Stable plan")
    planner = DeterministicPlanner()

    first = planner.plan(goal)
    second = planner.plan(goal)

    assert first == second
    assert first.task.planner_version == "deterministic-v1"
    assert first.steps[0].replay_safety is ReplaySafety.PURE


def test_step_result_rejects_values_outside_json() -> None:
    with pytest.raises(ValidationError, match="finite JSON"):
        StepResult(data={"invalid": object()})


def test_runtime_schedules_dependencies_and_persists_results(tmp_path) -> None:
    store, runtime, runner, plan = make_runtime(tmp_path)

    finished = runtime.run_task(plan.task.id)

    assert finished.state is TaskState.COMPLETED
    assert [item[0] for item in runner.calls] == ["first", "second"]
    assert all(store.get_step_result(step.id).data["accepted"] is True for step in plan.steps)
    assert [step.state for step in store.list_steps(plan.task.id)] == [
        StepState.SUCCEEDED,
        StepState.SUCCEEDED,
    ]
    store.close()


def test_runtime_retries_only_within_persisted_attempt_limit(tmp_path) -> None:
    store, runtime, runner, plan = make_runtime(
        tmp_path, planner=FixedPlanner(attempt_limit=2)
    )
    runner.failures_remaining = 2

    finished = runtime.run_task(plan.task.id)

    assert finished.state is TaskState.FAILED
    assert len([call for call in runner.calls if call[0] == "first"]) == 2
    assert store.get_step("first").attempt_count == 2
    assert [item.outcome.value for item in store.list_attempts("first")] == ["retryable_failure"] * 2
    store.close()


def test_untyped_pure_step_error_retries_within_the_attempt_limit(tmp_path) -> None:
    store, runtime, runner, plan = make_runtime(tmp_path)
    original_execute = runner.execute
    first_call = True

    def fail_once(step: Step, *, idempotency_key: str | None) -> StepResult:
        nonlocal first_call
        if first_call:
            first_call = False
            raise RuntimeError("unstable runner detail is not persisted")
        return original_execute(step, idempotency_key=idempotency_key)

    runner.execute = fail_once
    assert runtime.run_task(plan.task.id).state is TaskState.COMPLETED
    assert [attempt.outcome.value for attempt in store.list_attempts("first")] == [
        "outcome_unknown",
        "succeeded",
    ]
    assert all(
        "unstable runner detail is not persisted" not in event.metadata.model_dump_json()
        for event in store.list_events()
    )
    store.close()


def test_pause_and_resume_take_effect_at_step_boundaries(tmp_path) -> None:
    store, runtime, runner, plan = make_runtime(tmp_path)
    runner.on_execute = lambda: runtime.pause_task(plan.task.id)

    paused = runtime.run_task(plan.task.id)

    assert paused.state is TaskState.PAUSED
    assert store.get_step("first").state is StepState.SUCCEEDED
    assert store.get_step("second").state is StepState.PENDING
    runner.on_execute = None
    assert runtime.resume_task(plan.task.id).state is TaskState.COMPLETED
    store.close()


def test_cancel_request_finishes_current_step_then_cancels_dependants(tmp_path) -> None:
    store, runtime, runner, plan = make_runtime(tmp_path)
    runner.on_execute = lambda: runtime.cancel_task(plan.task.id)

    cancelled = runtime.run_task(plan.task.id)

    assert cancelled.state is TaskState.CANCELLED
    assert store.get_step("first").state is StepState.SUCCEEDED
    assert store.get_step("second").state is StepState.CANCELLED
    store.close()


def test_restart_finishes_persisted_cancellation_at_boundary(tmp_path) -> None:
    store, runtime, _, plan = make_runtime(tmp_path)
    store.transition_task(plan.task.id, TaskState.RUNNING)
    store.transition_task(plan.task.id, TaskState.CANCELLING)

    cancelled = runtime.run_task(plan.task.id)

    assert cancelled.state is TaskState.CANCELLED
    assert all(step.state is StepState.CANCELLED for step in store.list_steps(plan.task.id))
    store.close()


def test_failed_prerequisite_prevents_dependent_dispatch(tmp_path) -> None:
    store, runtime, runner, plan = make_runtime(tmp_path)
    runner.fail_permanently = True

    failed = runtime.run_task(plan.task.id)

    assert failed.state is TaskState.FAILED
    assert store.get_step("first").state is StepState.FAILED
    assert store.get_step("second").state is StepState.PENDING
    assert [call[0] for call in runner.calls] == ["first"]
    store.close()


def test_non_idempotent_step_requires_runner_deduplication_contract(tmp_path) -> None:
    runner = RecordingRunner()
    runner.deduplicates_idempotency_keys = False
    store, runtime, _, plan = make_runtime(
        tmp_path, planner=FixedPlanner(ReplaySafety.IDEMPOTENT), runner=runner
    )

    failed = runtime.run_task(plan.task.id)

    assert failed.state is TaskState.FAILED
    assert runner.calls == []
    assert store.get_step("first").attempt_count == 1
    assert store.list_attempts("first")[0].error_code is SafeErrorCode.PERMANENT
    store.close()


def test_terminal_task_is_never_dispatched_again(tmp_path) -> None:
    store, runtime, runner, plan = make_runtime(tmp_path)
    runtime.run_task(plan.task.id)
    count = len(runner.calls)

    assert runtime.run_task(plan.task.id).state is TaskState.COMPLETED
    assert len(runner.calls) == count
    store.close()


def test_process_wide_guard_prevents_duplicate_dispatch_across_runtime_instances(tmp_path) -> None:
    store, runtime, runner, plan = make_runtime(tmp_path)
    second_store = TaskStore(store.db_path)
    second_runner = RecordingRunner()
    second = TaskRuntime(second_store, FixedPlanner(), second_runner)
    runner.on_execute = lambda: pytest.raises(RuntimeBusyError, second.run_task, plan.task.id)

    assert runtime.run_task(plan.task.id).state is TaskState.COMPLETED
    assert second_runner.calls == []
    second_store.close()
    store.close()
