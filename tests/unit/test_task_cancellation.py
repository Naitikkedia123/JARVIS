"""Transactional task-owner cancellation store coverage."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest

from jarvis.tasks.action import AuthorizedTaskAction
from jarvis.tasks.authorization import TaskOwnership, TaskSubmissionProposal
from jarvis.tasks.errors import InvalidTransitionError
from jarvis.tasks.models import AttemptAuthorization, AttemptOutcome, Goal, StepState, TaskState
from jarvis.tasks.planner import DeterministicPlanner
from jarvis.tasks.store import TaskStore

DEVICE_ID = "a" * 32
FINGERPRINT = "b" * 64


def owned_task(database, *, device_id: str = DEVICE_ID) -> str:
    store = TaskStore(database)
    try:
        action = AuthorizedTaskAction(tool_name="jarvis.status", arguments={})
        proposal = TaskSubmissionProposal(
            idempotency_key="task-cancel-unit-key-0001",
            title="Cancel an unstarted task",
            action=action,
        )
        goal = Goal(id=uuid4().hex, title=proposal.title, description=proposal.description)
        plan = DeterministicPlanner().plan(goal, action=action)
        ownership = TaskOwnership(
            device_id=device_id,
            idempotency_key=proposal.idempotency_key,
            request_digest=proposal.request_digest(),
        )
        saved, created = store.add_device_task_submission(
            goal=goal,
            plan=plan,
            ownership=ownership,
        )
        assert created
        return saved.task.id
    finally:
        store.close()


def cancel(store: TaskStore, task_id: str, device_id: str = DEVICE_ID):
    return store.cancel_device_task(
        task_id,
        device_id,
        precommit_validation=lambda: None,
    )


def test_cancel_rolls_back_when_task_event_insert_fails(tmp_path) -> None:
    database = tmp_path / "task-cancel-audit-failure.sqlite3"
    task_id = owned_task(database)
    store = TaskStore(database)
    store._connection.execute(
        """CREATE TRIGGER fail_task_cancel_event
            BEFORE INSERT ON task_events WHEN NEW.to_state = 'cancelled'
            BEGIN SELECT RAISE(ABORT, 'private db failure'); END"""
    )
    before_events = store.list_events(task_id=task_id)

    with pytest.raises(sqlite3.IntegrityError):
        cancel(store, task_id)

    assert store.get_task(task_id).state is TaskState.READY
    assert store.list_steps(task_id)[0].state.value == "pending"
    assert store.list_events(task_id=task_id) == before_events
    store.close()


def test_attempted_task_cannot_be_cancelled(tmp_path) -> None:
    database = tmp_path / "task-cancel-attempt.sqlite3"
    task_id = owned_task(database)
    store = TaskStore(database)
    store.transition_task(task_id, TaskState.RUNNING)
    step = store.transition_step(store.list_steps(task_id)[0].id, StepState.READY)
    attempt = store.start_step_attempt(
        step.id,
        authorization=AttemptAuthorization(
            device_id=DEVICE_ID,
            action_digest=step.action.digest(),
            authorization_fingerprint=FINGERPRINT,
        ),
    )
    before_events = store.list_events(task_id=task_id)

    with pytest.raises(InvalidTransitionError):
        cancel(store, task_id)

    assert store.get_task(task_id).state is TaskState.RUNNING
    assert store.list_attempts(step.id) == [attempt]
    assert store.list_events(task_id=task_id) == before_events
    store.close()


def test_outcome_unknown_attempt_is_never_rewritten_by_cancellation(tmp_path) -> None:
    database = tmp_path / "task-cancel-outcome-unknown.sqlite3"
    task_id = owned_task(database)
    store = TaskStore(database)
    store.transition_task(task_id, TaskState.RUNNING)
    step = store.transition_step(store.list_steps(task_id)[0].id, StepState.READY)
    attempt = store.start_step_attempt(
        step.id,
        authorization=AttemptAuthorization(
            device_id=DEVICE_ID,
            action_digest=step.action.digest(),
            authorization_fingerprint=FINGERPRINT,
        ),
    )
    store.finish_step_attempt(step.id, AttemptOutcome.OUTCOME_UNKNOWN)
    before_events = store.list_events(task_id=task_id)
    before_attempts = store.list_attempts(step.id)

    with pytest.raises(InvalidTransitionError):
        cancel(store, task_id)

    assert store.get_task(task_id).state is TaskState.RUNNING
    assert store.get_step(step.id).state is StepState.RECONCILIATION_REQUIRED
    assert store.list_attempts(step.id) == before_attempts
    assert before_attempts[0].id == attempt.id
    assert before_attempts[0].outcome is AttemptOutcome.OUTCOME_UNKNOWN
    assert store.list_events(task_id=task_id) == before_events
    store.close()


def test_cancel_wins_start_race_and_start_cannot_create_attempt(tmp_path) -> None:
    database = tmp_path / "task-cancel-wins.sqlite3"
    task_id = owned_task(database)
    cancel_in_transaction = Event()
    allow_cancel_commit = Event()
    start_called = Event()

    def cancel_worker():
        store = TaskStore(database)
        original_append = store._append_event

        def block_before_cancel_event(**kwargs):
            if kwargs.get("to_state") == "cancelled" and not cancel_in_transaction.is_set():
                cancel_in_transaction.set()
                if not allow_cancel_commit.wait(timeout=5):
                    raise TimeoutError("Cancellation test synchronization timed out.")
            original_append(**kwargs)

        store._append_event = block_before_cancel_event
        try:
            result = cancel(store, task_id)
            return result.state
        finally:
            store.close()

    def start_worker():
        store = TaskStore(database)
        try:
            start_called.set()
            return store.transition_task(task_id, TaskState.RUNNING)
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        cancellation = executor.submit(cancel_worker)
        assert cancel_in_transaction.wait(timeout=5)
        start = executor.submit(start_worker)
        assert start_called.wait(timeout=5)
        allow_cancel_commit.set()
        assert cancellation.result(timeout=5) is TaskState.CANCELLED
        with pytest.raises(InvalidTransitionError):
            start.result(timeout=5)

    reopened = TaskStore(database)
    try:
        assert reopened.get_task(task_id).state is TaskState.CANCELLED
        assert reopened.list_attempts(reopened.list_steps(task_id)[0].id) == []
    finally:
        reopened.close()


def test_start_wins_cancel_race_without_partial_cancellation(tmp_path) -> None:
    database = tmp_path / "task-start-wins.sqlite3"
    task_id = owned_task(database)
    start_in_transaction = Event()
    allow_start_commit = Event()
    cancel_called = Event()

    def start_worker():
        store = TaskStore(database)
        original_append = store._append_event

        def block_before_running_event(**kwargs):
            if kwargs.get("to_state") == "running" and not start_in_transaction.is_set():
                start_in_transaction.set()
                if not allow_start_commit.wait(timeout=5):
                    raise TimeoutError("Start test synchronization timed out.")
            original_append(**kwargs)

        store._append_event = block_before_running_event
        try:
            return store.transition_task(task_id, TaskState.RUNNING).state
        finally:
            store.close()

    def cancel_worker():
        store = TaskStore(database)
        try:
            cancel_called.set()
            return cancel(store, task_id)
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        start = executor.submit(start_worker)
        assert start_in_transaction.wait(timeout=5)
        cancellation = executor.submit(cancel_worker)
        assert cancel_called.wait(timeout=5)
        allow_start_commit.set()
        assert start.result(timeout=5) is TaskState.RUNNING
        with pytest.raises(InvalidTransitionError):
            cancellation.result(timeout=5)

    reopened = TaskStore(database)
    try:
        assert reopened.get_task(task_id).state is TaskState.RUNNING
        assert reopened.list_steps(task_id)[0].state.value == "pending"
        assert reopened.list_attempts(reopened.list_steps(task_id)[0].id) == []
        assert not any(
            event.to_state == "cancelled" for event in reopened.list_events(task_id=task_id)
        )
    finally:
        reopened.close()
