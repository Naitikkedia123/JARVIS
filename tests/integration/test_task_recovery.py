"""File-backed restart and uncertain-effect recovery tests."""

from __future__ import annotations

from uuid import uuid4

import pytest

from jarvis.tasks.errors import EntityNotFoundError
from jarvis.tasks.models import (
    Goal,
    ReconciliationOutcome,
    ReplaySafety,
    Step,
    StepResult,
    Task,
    TaskPlan,
    TaskState,
)
from jarvis.tasks.runtime import TaskRuntime
from jarvis.tasks.store import TaskStore


class SimulatedProcessCrash(BaseException):
    """Test-only abrupt-stop signal that production runtime deliberately does not catch."""


class RecoveryPlanner:
    version = "recovery-test-v1"

    def __init__(self, safety: ReplaySafety, *, two_steps: bool = False) -> None:
        self.safety = safety
        self.two_steps = two_steps

    def plan(self, goal: Goal) -> TaskPlan:
        task = Task(id="recovery-task", goal_id=goal.id, title="Recovery task", planner_version=self.version)
        first = Step(
            id="effect-step",
            task_id=task.id,
            title="Effect",
            replay_safety=self.safety,
            idempotency_key="constant-operation-key"
            if self.safety is ReplaySafety.IDEMPOTENT
            else None,
        )
        steps = [first]
        if self.two_steps:
            steps.append(
                Step(
                    id="after-effect",
                    task_id=task.id,
                    title="After effect",
                    dependencies=[first.id],
                    replay_safety=ReplaySafety.PURE,
                )
            )
        return TaskPlan(task=task, steps=steps)


class CrashAfterEffectRunner:
    deduplicates_idempotency_keys = True

    def __init__(self, *, crash_step: str = "effect-step") -> None:
        self.crash_step = crash_step
        self.effects: list[str] = []
        self.calls: list[tuple[str, str | None]] = []
        self.crash = True

    def execute(self, step: Step, *, idempotency_key: str | None) -> StepResult:
        self.calls.append((step.id, idempotency_key))
        self.effects.append(step.id)
        if self.crash and step.id == self.crash_step:
            raise SimulatedProcessCrash
        return StepResult(data={"effect": step.id, "confirmed": True})


class SuccessRunner:
    def __init__(self, *, deduplicates: bool = True) -> None:
        self.deduplicates_idempotency_keys = deduplicates
        self.calls: list[tuple[str, str | None]] = []

    def execute(self, step: Step, *, idempotency_key: str | None) -> StepResult:
        self.calls.append((step.id, idempotency_key))
        return StepResult(data={"effect": step.id, "confirmed": True})


def create_runtime(database, safety, runner, *, two_steps=False):
    store = TaskStore(database)
    goal = Goal(id="recovery-goal", title="Recover safely")
    try:
        goal = store.get_goal(goal.id)
    except EntityNotFoundError:
        store.add_goal(goal)
    runtime = TaskRuntime(store, RecoveryPlanner(safety, two_steps=two_steps), runner)
    try:
        task = store.get_task("recovery-task")
        plan = TaskPlan(task=task, steps=store.list_steps(task.id))
    except EntityNotFoundError:
        plan = runtime.create_task(goal.id)
    return store, runtime, plan


def test_restart_resumes_from_checkpoint_without_repeating_completed_step(tmp_path) -> None:
    database = tmp_path / "checkpoint-restart.sqlite3"
    crashing = CrashAfterEffectRunner(crash_step="after-effect")
    store, runtime, plan = create_runtime(
        database, ReplaySafety.PURE, crashing, two_steps=True
    )
    with pytest.raises(SimulatedProcessCrash):
        runtime.run_task(plan.task.id)
    assert store.get_step("effect-step").state.value == "succeeded"
    assert len(store.list_checkpoints(plan.task.id)) == 1
    store.close()

    resumed = SuccessRunner()
    reopened, restarted, _ = create_runtime(
        database, ReplaySafety.PURE, resumed, two_steps=True
    )
    assert restarted.run_task(plan.task.id).state is TaskState.COMPLETED
    assert [call[0] for call in resumed.calls] == ["after-effect"]
    assert reopened.get_step("effect-step").attempt_count == 1
    reopened.close()


def test_crash_after_non_idempotent_effect_requires_reconciliation_and_never_replays(
    tmp_path,
) -> None:
    database = tmp_path / "uncertain-effect.sqlite3"
    first_runner = CrashAfterEffectRunner()
    store, runtime, plan = create_runtime(database, ReplaySafety.NON_IDEMPOTENT, first_runner)
    with pytest.raises(SimulatedProcessCrash):
        runtime.run_task(plan.task.id)
    assert first_runner.effects == ["effect-step"]
    store.close()

    no_replay_runner = SuccessRunner()
    reopened, restarted, _ = create_runtime(
        database, ReplaySafety.NON_IDEMPOTENT, no_replay_runner
    )
    assert restarted.run_task(plan.task.id).state is TaskState.RECONCILIATION_REQUIRED
    assert no_replay_runner.calls == []
    assert reopened.list_attempts("effect-step")[0].outcome.value == "outcome_unknown"

    completed = restarted.reconcile_step(
        "effect-step",
        ReconciliationOutcome.CONFIRMED_SUCCEEDED,
        confirmation_id=uuid4(),
        result=StepResult(data={"confirmed_by": "test-observation"}),
    )
    assert completed.state is TaskState.COMPLETED
    assert no_replay_runner.calls == []
    assert reopened.get_step_result("effect-step").data == {
        "confirmed_by": "test-observation"
    }
    reopened.close()


def test_confirmed_not_applied_reconciliation_can_resume_within_attempt_limit(tmp_path) -> None:
    database = tmp_path / "not-applied.sqlite3"
    crashed = CrashAfterEffectRunner()
    store, runtime, plan = create_runtime(database, ReplaySafety.NON_IDEMPOTENT, crashed)
    with pytest.raises(SimulatedProcessCrash):
        runtime.run_task(plan.task.id)
    store.close()

    success = SuccessRunner()
    reopened, restarted, _ = create_runtime(
        database, ReplaySafety.NON_IDEMPOTENT, success
    )
    ready = restarted.reconcile_step(
        "effect-step",
        ReconciliationOutcome.CONFIRMED_NOT_APPLIED,
        confirmation_id=uuid4(),
    )
    assert ready.state is TaskState.READY
    assert restarted.run_task(plan.task.id).state is TaskState.COMPLETED
    assert success.calls == [("effect-step", None)]
    assert [attempt.attempt_number for attempt in reopened.list_attempts("effect-step")] == [1, 2]
    reopened.close()


@pytest.mark.parametrize(
    ("safety", "deduplicates", "expected_state", "expected_calls"),
    [
        (ReplaySafety.PURE, True, TaskState.COMPLETED, 1),
        (ReplaySafety.IDEMPOTENT, True, TaskState.COMPLETED, 1),
        (ReplaySafety.IDEMPOTENT, False, TaskState.RECONCILIATION_REQUIRED, 0),
    ],
)
def test_interrupted_safe_replay_obeys_runner_contract_and_keeps_key(
    tmp_path, safety, deduplicates, expected_state, expected_calls
) -> None:
    database = tmp_path / f"safe-replay-{safety.value}-{deduplicates}.sqlite3"
    crashed = CrashAfterEffectRunner()
    store, runtime, plan = create_runtime(database, safety, crashed)
    with pytest.raises(SimulatedProcessCrash):
        runtime.run_task(plan.task.id)
    store.close()

    resumed = SuccessRunner(deduplicates=deduplicates)
    reopened, restarted, _ = create_runtime(database, safety, resumed)
    assert restarted.run_task(plan.task.id).state is expected_state
    assert len(resumed.calls) == expected_calls
    if safety is ReplaySafety.IDEMPOTENT and deduplicates:
        attempts = reopened.list_attempts("effect-step")
        assert [attempt.idempotency_key for attempt in attempts] == [
            "constant-operation-key",
            "constant-operation-key",
        ]
    reopened.close()


def test_completed_step_is_not_redispatched_after_restart(tmp_path) -> None:
    database = tmp_path / "completed.sqlite3"
    first_runner = SuccessRunner()
    store, runtime, plan = create_runtime(database, ReplaySafety.NON_IDEMPOTENT, first_runner)
    assert runtime.run_task(plan.task.id).state is TaskState.COMPLETED
    store.close()

    second_runner = SuccessRunner()
    reopened, restarted, _ = create_runtime(
        database, ReplaySafety.NON_IDEMPOTENT, second_runner
    )
    assert restarted.run_task(plan.task.id).state is TaskState.COMPLETED
    assert second_runner.calls == []
    reopened.close()
