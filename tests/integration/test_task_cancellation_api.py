"""Authenticated task-owner cancellation API coverage."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from jarvis.config import Settings
from jarvis.devices.service import DeviceService
from jarvis.devices.store import DeviceStore
from jarvis.main import create_app
from jarvis.tasks.models import (
    ApprovalAuditEventType,
    ApprovalRequestRecord,
    ApprovalStatus,
    TaskState,
)
from jarvis.tasks.store import TaskStore

BOOTSTRAP = "task-cancel-bootstrap-0123456789"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def payload(key: str) -> dict[str, object]:
    return {
        "idempotency_key": key,
        "title": "Read local status",
        "action": {"tool_name": "jarvis.status", "arguments": {}},
    }


@pytest.fixture
def cancellation_api(tmp_path):
    submission_path = tmp_path / "submission-grants.json"
    settings = Settings(
        device_database_path=tmp_path / "devices.sqlite3",
        task_database_path=tmp_path / "tasks.sqlite3",
        device_task_grants_path=tmp_path / "dispatch-grants.json",
        task_submission_grants_path=submission_path,
        device_bootstrap_code=SecretStr(BOOTSTRAP),
    )
    with TestClient(create_app(settings)) as client:
        owner_response = client.post(
            "/devices/pair",
            json={"pairing_code": BOOTSTRAP, "name": "owner", "platform": "windows"},
        )
        assert owner_response.status_code == 201
        owner = owner_response.json()
        code = client.post(
            "/devices/pairing-codes", headers=auth(owner["access_token"])
        ).json()["pairing_code"]
        companion_response = client.post(
            "/devices/pair",
            json={"pairing_code": code, "name": "task device", "platform": "android"},
        )
        assert companion_response.status_code == 201
        companion = companion_response.json()
        submission_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "allowed_device_ids": [
                        owner["device"]["id"],
                        companion["device"]["id"],
                    ],
                }
            ),
            encoding="utf-8",
        )
        settings.device_task_grants_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "allowed_device_ids": [
                        owner["device"]["id"],
                        companion["device"]["id"],
                    ],
                }
            ),
            encoding="utf-8",
        )
        yield client, owner, companion, settings


def submit(client: TestClient, token: str, key: str = "task-cancel-test-key-0001"):
    response = client.post("/tasks", headers=auth(token), json=payload(key))
    assert response.status_code == 201, response.text
    return response.json()


def create_waiting_approval(settings: Settings, task_id: str, owner: dict, companion: dict):
    device_store = DeviceStore(settings.device_database_path)
    owner_device = device_store.get_device(owner["device"]["id"])
    device_store.close()
    store = TaskStore(settings.task_database_path)
    try:
        step = store.list_steps(task_id)[0]
        now = store.current_time()
        request = ApprovalRequestRecord(
            request_id=uuid4(),
            task_id=task_id,
            step_id=step.id,
            requesting_device_id=companion["device"]["id"],
            owner_device_id=owner_device.id,
            owner_credential_generation=owner_device.credential_generation,
            action_digest=step.action.digest(),
            tool_definition_digest="0" * 64,
            status=ApprovalStatus.PENDING,
            created_at=now,
            expires_at=now + timedelta(minutes=5),
        )
        return store.create_approval_request(request)
    finally:
        store.close()


def test_repeat_cancellation_conflicts_without_mutating_cancellation_state(
    cancellation_api,
) -> None:
    client, owner, _companion, settings = cancellation_api
    submitted = submit(client, owner["access_token"])
    task_id = submitted["task_id"]
    runner = client.app.state._task_services.runtime.authorized_runner
    assert runner is not None
    executor_calls = []

    async def unexpected_dispatch(*args, **kwargs):
        del args, kwargs
        executor_calls.append("called")
        raise AssertionError("Cancellation must never invoke an executor.")

    runner.execute_claimed = unexpected_dispatch

    first = client.post(
        f"/tasks/{task_id}/cancel", headers=auth(owner["access_token"])
    )
    assert first.status_code == 200, first.text
    assert first.headers["cache-control"] == "no-store"
    assert first.json()["status"] == "cancelled"
    assert first.json()["steps"][0]["state"] == "cancelled"

    store = TaskStore(settings.task_database_path)
    try:
        events_before_retry = store.list_events(task_id=task_id)
        assert store.list_attempts(submitted["steps"][0]["step_id"]) == []
    finally:
        store.close()

    retry = client.post(
        f"/tasks/{task_id}/cancel", headers=auth(owner["access_token"])
    )
    assert retry.status_code == 409
    assert executor_calls == []

    reopened = TaskStore(settings.task_database_path)
    try:
        assert reopened.get_task(task_id).state is TaskState.CANCELLED
        assert reopened.list_steps(task_id)[0].state.value == "cancelled"
        assert reopened.list_events(task_id=task_id) == events_before_retry
        assert reopened.list_attempts(submitted["steps"][0]["step_id"]) == []
    finally:
        reopened.close()


def test_unauthenticated_foreign_and_unknown_cancellation_responses(cancellation_api) -> None:
    client, owner, companion, _settings = cancellation_api
    submitted = submit(client, owner["access_token"])
    task_id = submitted["task_id"]

    unauthenticated = client.post(f"/tasks/{task_id}/cancel")
    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["www-authenticate"] == "Bearer"

    foreign = client.post(
        f"/tasks/{task_id}/cancel", headers=auth(companion["access_token"])
    )
    unknown = client.post(
        "/tasks/unknown-task-id/cancel", headers=auth(owner["access_token"])
    )
    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == unknown.json()


@pytest.mark.parametrize("approve_first", [False, True])
def test_approval_owner_cannot_cancel_foreign_task_but_task_owner_can(
    cancellation_api, approve_first: bool
) -> None:
    client, owner, companion, settings = cancellation_api
    submitted = submit(client, companion["access_token"], "task-cancel-companion-key-1")
    task_id = submitted["task_id"]
    request = create_waiting_approval(settings, task_id, owner, companion)
    if approve_first:
        decision = client.post(
            f"/tasks/approvals/{request.request_id}/decision",
            headers=auth(owner["access_token"]),
            json={"decision": "approve"},
        )
        assert decision.status_code == 200, decision.text

    store = TaskStore(settings.task_database_path)
    try:
        runtime = client.app.state._task_services.runtime
        runner = runtime.authorized_runner
        assert runner is not None
        calls = []

        async def unexpected_dispatch(*args, **kwargs):
            del args, kwargs
            calls.append("called")
            raise AssertionError("Cancellation must never invoke an executor.")

        runner.execute_claimed = unexpected_dispatch
        owner_attempt = client.post(
            f"/tasks/{task_id}/cancel", headers=auth(owner["access_token"])
        )
        assert owner_attempt.status_code == 404
        assert store.get_task(task_id).state is TaskState.WAITING_APPROVAL

        owner_cancel = client.post(
            f"/tasks/{task_id}/cancel", headers=auth(companion["access_token"])
        )
        assert owner_cancel.status_code == 200, owner_cancel.text
        assert owner_cancel.json()["status"] == "cancelled"
        assert calls == []
        assert store.get_task(task_id).state is TaskState.CANCELLED
        step = store.list_steps(task_id)[0]
        assert step.state.value == "cancelled"
        assert store.list_attempts(step.id) == []
        approval = store.get_approval_request(request.request_id)
        assert approval.status is ApprovalStatus.REVOKED
        assert approval.revoked_by_device_id == companion["device"]["id"]
        events = store.list_approval_audit_events(request.request_id)
        assert events[-1].event_type is ApprovalAuditEventType.REVOKED
        assert events[-1].actor_device_id == companion["device"]["id"]
        assert events[-1].reason_code == "task_cancelled"
        if approve_first:
            resume = client.post(
                f"/tasks/{task_id}/resume",
                headers=auth(companion["access_token"]),
            )
            assert resume.status_code == 409
            assert store.get_approval_request(request.request_id).status is ApprovalStatus.REVOKED
            assert [
                event.event_type
                for event in store.list_approval_audit_events(request.request_id)
            ] == [
                ApprovalAuditEventType.REQUESTED,
                ApprovalAuditEventType.APPROVED,
                ApprovalAuditEventType.REVOKED,
            ]
            assert store.list_attempts(step.id) == []
            assert calls == []
    finally:
        store.close()


def test_cancellation_wins_resume_reservation_race_without_attempt(
    cancellation_api, monkeypatch
) -> None:
    client, owner, companion, settings = cancellation_api
    assert owner["device"]["id"] != companion["device"]["id"]
    submitted = submit(client, companion["access_token"], "task-cancel-resume-win-key-01")
    task_id = submitted["task_id"]
    request = create_waiting_approval(settings, task_id, owner, companion)
    decision = client.post(
        f"/tasks/approvals/{request.request_id}/decision",
        headers=auth(owner["access_token"]),
        json={"decision": "approve"},
    )
    assert decision.status_code == 200, decision.text

    cancel_service = client.app.state.device_service
    assert cancel_service is not None
    cancel_reserve = cancel_service.reserve_task_device
    cancel_reservation_acquired = Event()
    cancel_reservation_released = Event()
    allow_cancel_to_continue = Event()

    @contextmanager
    def hold_cancel_reservation(access_token, *, expected_device_id):
        try:
            with cancel_reserve(
                access_token,
                expected_device_id=expected_device_id,
            ) as device:
                assert cancel_service._store._connection.in_transaction
                cancel_reservation_acquired.set()
                if not allow_cancel_to_continue.wait(timeout=5):
                    raise TimeoutError("Cancellation reservation synchronization timed out.")
                yield device
        finally:
            assert not cancel_service._store._connection.in_transaction
            cancel_reservation_released.set()

    monkeypatch.setattr(cancel_service, "reserve_task_device", hold_cancel_reservation)

    cancel_runner = client.app.state._task_services.authorized_runner
    assert cancel_runner is not None
    executor_calls = []
    original_cancel_executor = cancel_runner.dispatcher._executors["jarvis.status"].execute

    async def observe_cancel_executor(arguments):
        executor_calls.append("cancel")
        return await original_cancel_executor(arguments)

    monkeypatch.setattr(
        cancel_runner.dispatcher._executors["jarvis.status"],
        "execute",
        observe_cancel_executor,
    )

    resume_authentication_attempted = Event()
    resume_authentication_completed = Event()
    resume_reservation_attempted = Event()
    resume_reservation_acquired = Event()
    resume_finished = Event()
    resume_responses = []
    resume_errors = []
    cancel_finished = Event()
    cancel_responses = []
    cancel_errors = []

    with TestClient(create_app(settings)) as resume_client:
        resume_service = resume_client.app.state.device_service
        assert resume_service is not None
        resume_authenticate = resume_service.authenticate
        resume_reserve = resume_service.reserve_approval_resume

        def observe_resume_authentication(access_token):
            resume_authentication_attempted.set()
            device = resume_authenticate(access_token)
            resume_authentication_completed.set()
            return device

        monkeypatch.setattr(
            resume_service,
            "authenticate",
            observe_resume_authentication,
        )

        @contextmanager
        def observe_resume_reservation(access_token, **kwargs):
            resume_reservation_attempted.set()
            with resume_reserve(access_token, **kwargs) as devices:
                resume_reservation_acquired.set()
                yield devices

        monkeypatch.setattr(
            resume_service,
            "reserve_approval_resume",
            observe_resume_reservation,
        )
        resume_runner = resume_client.app.state._task_services.authorized_runner
        assert resume_runner is not None
        original_resume_executor = resume_runner.dispatcher._executors[
            "jarvis.status"
        ].execute

        async def observe_resume_executor(arguments):
            executor_calls.append("resume")
            return await original_resume_executor(arguments)

        monkeypatch.setattr(
            resume_runner.dispatcher._executors["jarvis.status"],
            "execute",
            observe_resume_executor,
        )

        def send_cancel():
            try:
                cancel_responses.append(
                    client.post(
                        f"/tasks/{task_id}/cancel",
                        headers=auth(companion["access_token"]),
                    )
                )
            except BaseException as exc:
                cancel_errors.append(exc)
            finally:
                cancel_finished.set()

        def send_resume():
            try:
                resume_responses.append(
                    resume_client.post(
                        f"/tasks/{task_id}/resume",
                        headers=auth(companion["access_token"]),
                    )
                )
            except BaseException as exc:
                resume_errors.append(exc)
            finally:
                resume_finished.set()

        cancel_thread = Thread(target=send_cancel)
        resume_thread = Thread(target=send_resume)
        cancel_thread.start()
        try:
            assert cancel_reservation_acquired.wait(timeout=5)
            resume_thread.start()
            assert resume_authentication_attempted.wait(timeout=5)
            assert not resume_authentication_completed.wait(timeout=0.05)
            assert not resume_finished.is_set()
        finally:
            allow_cancel_to_continue.set()
            cancel_thread.join(timeout=5)
            if resume_thread.ident is not None:
                resume_thread.join(timeout=5)

        assert not cancel_thread.is_alive()
        assert not resume_thread.is_alive()
        assert cancel_errors == []
        assert resume_errors == []
        assert len(cancel_responses) == len(resume_responses) == 1
        assert cancel_responses[0].status_code == 200, cancel_responses[0].text
        assert resume_responses[0].status_code == 409, resume_responses[0].text
        assert resume_authentication_completed.is_set()
        assert not resume_reservation_attempted.is_set()
        assert not resume_reservation_acquired.is_set()
        assert executor_calls == []

    store = TaskStore(settings.task_database_path)
    try:
        assert store.get_task(task_id).state is TaskState.CANCELLED
        step = store.list_steps(task_id)[0]
        assert step.state.value == "cancelled"
        assert store.list_attempts(step.id) == []
        assert store.get_approval_request(request.request_id).status is ApprovalStatus.REVOKED
        approval_events = store.list_approval_audit_events(request.request_id)
        assert [event.event_type for event in approval_events] == [
            ApprovalAuditEventType.REQUESTED,
            ApprovalAuditEventType.APPROVED,
            ApprovalAuditEventType.REVOKED,
        ]
        assert sum(event.event_type is ApprovalAuditEventType.REVOKED for event in approval_events) == 1
        assert approval_events[-1].actor_device_id == companion["device"]["id"]
        assert executor_calls == []
    finally:
        store.close()
    assert cancel_reservation_released.is_set()


def test_resume_wins_cancellation_reservation_race_without_partial_cancel(
    cancellation_api, monkeypatch
) -> None:
    client, owner, companion, settings = cancellation_api
    assert owner["device"]["id"] != companion["device"]["id"]
    submitted = submit(client, companion["access_token"], "task-resume-cancel-win-key-01")
    task_id = submitted["task_id"]
    request = create_waiting_approval(settings, task_id, owner, companion)
    decision = client.post(
        f"/tasks/approvals/{request.request_id}/decision",
        headers=auth(owner["access_token"]),
        json={"decision": "approve"},
    )
    assert decision.status_code == 200, decision.text

    cancel_service = client.app.state.device_service
    assert cancel_service is not None
    cancel_authenticate = cancel_service.authenticate
    cancel_reserve = cancel_service.reserve_task_device
    cancel_authentication_attempted = Event()
    cancel_authentication_completed = Event()
    cancel_reservation_attempted = Event()
    cancel_reservation_acquired = Event()

    def observe_cancel_authentication(access_token):
        cancel_authentication_attempted.set()
        device = cancel_authenticate(access_token)
        cancel_authentication_completed.set()
        return device

    monkeypatch.setattr(cancel_service, "authenticate", observe_cancel_authentication)

    @contextmanager
    def observe_cancel_reservation(access_token, *, expected_device_id):
        cancel_reservation_attempted.set()
        with cancel_reserve(
            access_token,
            expected_device_id=expected_device_id,
        ) as device:
            cancel_reservation_acquired.set()
            yield device

    monkeypatch.setattr(cancel_service, "reserve_task_device", observe_cancel_reservation)

    cancel_runner = client.app.state._task_services.authorized_runner
    assert cancel_runner is not None
    executor_calls = []
    original_cancel_executor = cancel_runner.dispatcher._executors["jarvis.status"].execute

    async def observe_cancel_executor(arguments):
        executor_calls.append("cancel")
        return await original_cancel_executor(arguments)

    monkeypatch.setattr(
        cancel_runner.dispatcher._executors["jarvis.status"],
        "execute",
        observe_cancel_executor,
    )

    resume_reservation_acquired = Event()
    resume_reservation_released = Event()
    allow_resume_to_continue = Event()
    resume_finished = Event()
    resume_responses = []
    resume_errors = []
    cancel_finished = Event()
    cancel_responses = []
    cancel_errors = []

    with TestClient(create_app(settings)) as resume_client:
        resume_service = resume_client.app.state.device_service
        assert resume_service is not None
        resume_reserve = resume_service.reserve_approval_resume

        @contextmanager
        def hold_resume_reservation(access_token, **kwargs):
            try:
                with resume_reserve(access_token, **kwargs) as devices:
                    assert resume_service._store._connection.in_transaction
                    resume_reservation_acquired.set()
                    if not allow_resume_to_continue.wait(timeout=5):
                        raise TimeoutError("Resume reservation synchronization timed out.")
                    yield devices
            finally:
                assert not resume_service._store._connection.in_transaction
                resume_reservation_released.set()

        monkeypatch.setattr(resume_service, "reserve_approval_resume", hold_resume_reservation)
        resume_runner = resume_client.app.state._task_services.authorized_runner
        assert resume_runner is not None
        original_resume_executor = resume_runner.dispatcher._executors[
            "jarvis.status"
        ].execute

        async def observe_resume_executor(arguments):
            executor_calls.append("resume")
            return await original_resume_executor(arguments)

        monkeypatch.setattr(
            resume_runner.dispatcher._executors["jarvis.status"],
            "execute",
            observe_resume_executor,
        )

        def send_cancel():
            try:
                cancel_responses.append(
                    client.post(
                        f"/tasks/{task_id}/cancel",
                        headers=auth(companion["access_token"]),
                    )
                )
            except BaseException as exc:
                cancel_errors.append(exc)
            finally:
                cancel_finished.set()

        def send_resume():
            try:
                resume_responses.append(
                    resume_client.post(
                        f"/tasks/{task_id}/resume",
                        headers=auth(companion["access_token"]),
                    )
                )
            except BaseException as exc:
                resume_errors.append(exc)
            finally:
                resume_finished.set()

        resume_thread = Thread(target=send_resume)
        cancel_thread = Thread(target=send_cancel)
        resume_thread.start()
        try:
            assert resume_reservation_acquired.wait(timeout=5)
            cancel_thread.start()
            assert cancel_authentication_attempted.wait(timeout=5)
            assert not cancel_authentication_completed.wait(timeout=0.05)
            assert not cancel_finished.is_set()
        finally:
            allow_resume_to_continue.set()
            resume_thread.join(timeout=5)
            if cancel_thread.ident is not None:
                cancel_thread.join(timeout=5)

        assert not resume_thread.is_alive()
        assert not cancel_thread.is_alive()
        assert resume_errors == []
        assert cancel_errors == []
        assert len(resume_responses) == len(cancel_responses) == 1
        assert resume_responses[0].status_code == 200, resume_responses[0].text
        assert resume_responses[0].json()["status"] == "completed"
        assert cancel_responses[0].status_code == 409, cancel_responses[0].text
        assert cancel_authentication_completed.is_set()
        assert cancel_reservation_attempted.is_set()
        assert cancel_reservation_acquired.is_set()
        assert resume_finished.is_set()
        assert executor_calls == ["resume"]

    store = TaskStore(settings.task_database_path)
    try:
        assert store.get_task(task_id).state is TaskState.COMPLETED
        step = store.list_steps(task_id)[0]
        assert step.state.value == "succeeded"
        attempts = store.list_attempts(step.id)
        assert len(attempts) == 1
        assert attempts[0].dispatch_claimed_at is not None
        assert store.get_approval_request(request.request_id).status is ApprovalStatus.CONSUMED
        approval_events = store.list_approval_audit_events(request.request_id)
        assert [event.event_type for event in approval_events] == [
            ApprovalAuditEventType.REQUESTED,
            ApprovalAuditEventType.APPROVED,
            ApprovalAuditEventType.CONSUMED,
        ]
        assert sum(event.event_type is ApprovalAuditEventType.CONSUMED for event in approval_events) == 1
        assert not any(event.to_state == "cancelled" for event in store.list_events(task_id=task_id))
        assert executor_calls == ["resume"]
    finally:
        store.close()
    assert resume_reservation_released.is_set()


def test_equal_generation_foreign_device_cannot_cancel_task_or_touch_approvals(
    cancellation_api,
) -> None:
    client, owner, companion, settings = cancellation_api
    submitted = submit(client, companion["access_token"], "task-cancel-same-gen-key-01")
    task_id = submitted["task_id"]
    request = create_waiting_approval(settings, task_id, owner, companion)

    pairing_code = client.post(
        "/devices/pairing-codes", headers=auth(owner["access_token"])
    ).json()["pairing_code"]
    foreign_response = client.post(
        "/devices/pair",
        json={
            "pairing_code": pairing_code,
            "name": "same-generation foreign device",
            "platform": "android",
        },
    )
    assert foreign_response.status_code == 201, foreign_response.text
    foreign = foreign_response.json()

    device_store = DeviceStore(settings.device_database_path)
    try:
        task_owner_device = device_store.get_device(companion["device"]["id"])
        foreign_device = device_store.get_device(foreign["device"]["id"])
        assert foreign_device.id != task_owner_device.id
        assert foreign_device.credential_generation == task_owner_device.credential_generation
    finally:
        device_store.close()

    runner = client.app.state._task_services.runtime.authorized_runner
    assert runner is not None
    executor_calls = []

    async def unexpected_dispatch(*args, **kwargs):
        del args, kwargs
        executor_calls.append("called")
        raise AssertionError("Rejected cancellation must never invoke an executor.")

    runner.execute_claimed = unexpected_dispatch
    store = TaskStore(settings.task_database_path)
    try:
        before_task = store.get_task(task_id)
        before_steps = store.list_steps(task_id)
        before_task_events = store.list_events(task_id=task_id)
        before_approval = store.get_approval_request(request.request_id)
        before_approval_events = store.list_approval_audit_events(request.request_id)
        before_attempts = [store.list_attempts(step.id) for step in before_steps]

        response = client.post(
            f"/tasks/{task_id}/cancel", headers=auth(foreign["access_token"])
        )

        assert response.status_code == 404
        assert executor_calls == []
        assert store.get_task(task_id) == before_task
        assert store.list_steps(task_id) == before_steps
        assert store.list_events(task_id=task_id) == before_task_events
        assert store.get_approval_request(request.request_id) == before_approval
        assert store.list_approval_audit_events(request.request_id) == before_approval_events
        assert [store.list_attempts(step.id) for step in before_steps] == before_attempts
        assert all(not attempts for attempts in before_attempts)
    finally:
        store.close()


def test_started_task_conflicts_without_partial_changes(cancellation_api) -> None:
    client, owner, _companion, settings = cancellation_api
    submitted = submit(client, owner["access_token"])
    task_id = submitted["task_id"]
    store = TaskStore(settings.task_database_path)
    try:
        store.transition_task(task_id, TaskState.RUNNING)
        before_events = store.list_events(task_id=task_id)
    finally:
        store.close()

    response = client.post(
        f"/tasks/{task_id}/cancel", headers=auth(owner["access_token"])
    )
    assert response.status_code == 409

    reopened = TaskStore(settings.task_database_path)
    try:
        assert reopened.get_task(task_id).state is TaskState.RUNNING
        assert reopened.list_events(task_id=task_id) == before_events
        assert reopened.list_attempts(submitted["steps"][0]["step_id"]) == []
    finally:
        reopened.close()


@pytest.mark.parametrize("approve_first", [False, True])
def test_cancellation_audit_failure_rolls_back_task_and_approval(
    cancellation_api, approve_first: bool
) -> None:
    client, owner, companion, settings = cancellation_api
    submitted = submit(client, companion["access_token"], "task-cancel-failure-key-01")
    request = create_waiting_approval(settings, submitted["task_id"], owner, companion)
    if approve_first:
        decision = client.post(
            f"/tasks/approvals/{request.request_id}/decision",
            headers=auth(owner["access_token"]),
            json={"decision": "approve"},
        )
        assert decision.status_code == 200, decision.text
    runner = client.app.state._task_services.runtime.authorized_runner
    assert runner is not None
    executor_calls = []

    async def unexpected_dispatch(*args, **kwargs):
        del args, kwargs
        executor_calls.append("called")
        raise AssertionError("Failed cancellation must never invoke an executor.")

    runner.execute_claimed = unexpected_dispatch
    store = TaskStore(settings.task_database_path)
    try:
        before_events = store.list_events(task_id=submitted["task_id"])
        before_approval_events = store.list_approval_audit_events(request.request_id)
        store._connection.execute(
            """CREATE TRIGGER fail_cancel_revoke_audit
                BEFORE INSERT ON approval_audit_events
                WHEN NEW.event_type = 'revoked'
                BEGIN SELECT RAISE(ABORT, 'private database detail'); END"""
        )
    finally:
        store.close()

    response = client.post(
        f"/tasks/{submitted['task_id']}/cancel",
        headers=auth(companion["access_token"]),
    )
    assert response.status_code == 503
    assert "private database detail" not in response.text

    reopened = TaskStore(settings.task_database_path)
    try:
        assert reopened.get_task(submitted["task_id"]).state is TaskState.WAITING_APPROVAL
        step = reopened.list_steps(submitted["task_id"])[0]
        assert step.state.value == "waiting_approval"
        assert reopened.list_attempts(step.id) == []
        expected_status = ApprovalStatus.APPROVED if approve_first else ApprovalStatus.PENDING
        assert reopened.get_approval_request(request.request_id).status is expected_status
        assert reopened.list_events(task_id=submitted["task_id"]) == before_events
        assert (
            reopened.list_approval_audit_events(request.request_id)
            == before_approval_events
        )
        assert executor_calls == []
    finally:
        reopened.close()


def test_stale_authentication_after_revocation_cannot_cancel(cancellation_api, monkeypatch) -> None:
    client, owner, companion, settings = cancellation_api
    submitted = submit(client, companion["access_token"], "task-cancel-revoked-key-01")
    service = client.app.state.device_service
    assert service is not None
    reserve = service.reserve_task_device

    def revoke_then_reserve(access_token, *, expected_device_id):
        service.revoke_device(
            SecretStr(owner["access_token"]),
            expected_device_id,
        )
        return reserve(access_token, expected_device_id=expected_device_id)

    monkeypatch.setattr(service, "reserve_task_device", revoke_then_reserve)
    response = client.post(
        f"/tasks/{submitted['task_id']}/cancel",
        headers=auth(companion["access_token"]),
    )
    assert response.status_code == 404

    store = TaskStore(settings.task_database_path)
    try:
        assert store.get_task(submitted["task_id"]).state is TaskState.READY
        assert store.list_attempts(submitted["steps"][0]["step_id"]) == []
    finally:
        store.close()


def test_owner_expiry_between_authentication_and_reservation_cannot_cancel(
    cancellation_api, monkeypatch
) -> None:
    client, owner, _companion, settings = cancellation_api
    submitted = submit(client, owner["access_token"], "task-cancel-expiry-key-0001")
    service = client.app.state.device_service
    assert service is not None
    reserve = service.reserve_task_device

    def expire_then_reserve(access_token, *, expected_device_id):
        expired_time = datetime.now(UTC) + timedelta(days=3650)
        service._clock = lambda: expired_time
        service._store._clock = lambda: expired_time
        return reserve(access_token, expected_device_id=expected_device_id)

    monkeypatch.setattr(service, "reserve_task_device", expire_then_reserve)
    response = client.post(
        f"/tasks/{submitted['task_id']}/cancel",
        headers=auth(owner["access_token"]),
    )
    assert response.status_code == 404

    store = TaskStore(settings.task_database_path)
    try:
        assert store.get_task(submitted["task_id"]).state is TaskState.READY
        assert store.list_attempts(submitted["steps"][0]["step_id"]) == []
    finally:
        store.close()


def test_task_owner_revocation_waits_for_cancellation_commit(
    cancellation_api, monkeypatch
) -> None:
    client, owner, companion, settings = cancellation_api
    submitted = submit(client, companion["access_token"], "task-cancel-race-key-0001")
    service = client.app.state.device_service
    assert service is not None
    runtime = client.app.state._task_services.runtime
    task_store = runtime.store
    reserve = service.reserve_task_device
    cancel = task_store.cancel_device_task
    precommit_entered = Event()
    allow_commit = Event()
    task_committed = Event()
    allow_reservation_release = Event()
    revocation_started = Event()
    revocation_finished = Event()
    response_holder = []

    @contextmanager
    def hold_reservation_until_released(access_token, *, expected_device_id):
        with reserve(access_token, expected_device_id=expected_device_id) as device:
            yield device
            task_committed.set()
            if not allow_reservation_release.wait(timeout=5):
                raise TimeoutError("Device reservation test synchronization timed out.")

    def pause_before_commit(task_id, device_id, *, precommit_validation):
        def synchronized_validation():
            precommit_validation()
            precommit_entered.set()
            if not allow_commit.wait(timeout=5):
                raise TimeoutError("Cancellation test synchronization timed out.")

        return cancel(
            task_id,
            device_id,
            precommit_validation=synchronized_validation,
        )

    monkeypatch.setattr(service, "reserve_task_device", hold_reservation_until_released)
    monkeypatch.setattr(task_store, "cancel_device_task", pause_before_commit)

    cancellation_thread = Thread(
        target=lambda: response_holder.append(
            client.post(
                f"/tasks/{submitted['task_id']}/cancel",
                headers=auth(companion["access_token"]),
            )
        )
    )
    cancellation_thread.start()
    assert precommit_entered.wait(timeout=5)

    def revoke_companion():
        device_store = DeviceStore(settings.device_database_path)
        try:
            revocation_started.set()
            revoker = DeviceService(device_store, bootstrap_code=None)
            revoker.revoke_device(
                SecretStr(owner["access_token"]),
                companion["device"]["id"],
            )
            revocation_finished.set()
        finally:
            device_store.close()

    revocation_thread = Thread(target=revoke_companion)
    revocation_thread.start()
    assert revocation_started.wait(timeout=5)
    assert not revocation_finished.wait(timeout=0.05)

    allow_commit.set()
    assert task_committed.wait(timeout=5)
    assert not revocation_finished.wait(timeout=0.05)
    allow_reservation_release.set()
    cancellation_thread.join(timeout=5)
    revocation_thread.join(timeout=5)

    assert not cancellation_thread.is_alive()
    assert not revocation_thread.is_alive()
    assert len(response_holder) == 1
    assert response_holder[0].status_code == 200
    assert response_holder[0].json()["status"] == "cancelled"
    assert revocation_finished.is_set()

    store = TaskStore(settings.task_database_path)
    try:
        assert store.get_task(submitted["task_id"]).state is TaskState.CANCELLED
        assert store.list_attempts(submitted["steps"][0]["step_id"]) == []
    finally:
        store.close()
