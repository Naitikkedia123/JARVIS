"""Authenticated task-owner polling for durable task events."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from jarvis.config import Settings
from jarvis.main import create_app
from jarvis.tasks.models import TaskState
from jarvis.tasks.store import TaskStore

BOOTSTRAP = "task-events-bootstrap-0123456789"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def payload(key: str, *, title: str = "Read status") -> dict[str, object]:
    return {
        "idempotency_key": key,
        "title": title,
        "action": {"tool_name": "jarvis.status", "arguments": {}},
    }


def settings_for(tmp_path) -> Settings:
    return Settings(
        device_database_path=tmp_path / "devices.sqlite3",
        task_database_path=tmp_path / "tasks.sqlite3",
        task_submission_grants_path=tmp_path / "submission-grants.json",
        device_task_grants_path=tmp_path / "dispatch-grants.json",
        device_bootstrap_code=SecretStr(BOOTSTRAP),
    )


def pair_devices(client: TestClient, settings: Settings):
    owner_response = client.post(
        "/devices/pair",
        json={"pairing_code": BOOTSTRAP, "name": "owner", "platform": "windows"},
    )
    assert owner_response.status_code == 201, owner_response.text
    owner = owner_response.json()
    pairing_response = client.post(
        "/devices/pairing-codes", headers=auth(owner["access_token"])
    )
    assert pairing_response.status_code == 201, pairing_response.text
    companion_response = client.post(
        "/devices/pair",
        json={
            "pairing_code": pairing_response.json()["pairing_code"],
            "name": "companion",
            "platform": "android",
        },
    )
    assert companion_response.status_code == 201, companion_response.text
    companion = companion_response.json()
    settings.task_submission_grants_path.write_text(
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
    return owner, companion


def submit(client: TestClient, token: str, key: str, *, title: str = "Read status"):
    response = client.post("/tasks", headers=auth(token), json=payload(key, title=title))
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def task_event_api(tmp_path):
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings)) as client:
        owner, companion = pair_devices(client, settings)
        yield client, settings, owner, companion


def test_owner_polling_is_scoped_authenticated_safe_and_read_only(
    task_event_api, monkeypatch
) -> None:
    client, settings, owner, companion = task_event_api
    owned = submit(client, owner["access_token"], "task-events-owner-key-0001")
    foreign = submit(client, companion["access_token"], "task-events-companion-key-01")
    runtime = client.app.state._task_services.runtime
    runner = runtime.authorized_runner
    assert runner is not None
    execution_calls: list[object] = []

    async def unexpected_execution(*args, **kwargs):
        execution_calls.append((args, kwargs))
        raise AssertionError("event polling must not enter task execution")

    monkeypatch.setattr(runtime, "run_authorized_task_async", unexpected_execution)
    monkeypatch.setattr(runtime, "resume_approved_task_async", unexpected_execution)
    monkeypatch.setattr(runner, "execute_claimed", unexpected_execution)

    response = client.get(f"/tasks/{owned['task_id']}/events", headers=auth(owner["access_token"]))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["events"]
    assert [event["id"] for event in body["events"]] == sorted(
        event["id"] for event in body["events"]
    )
    assert set(body["events"][0]) == {
        "id",
        "event_type",
        "task_id",
        "step_id",
        "from_state",
        "to_state",
        "occurred_at",
    }
    assert all(event["task_id"] == owned["task_id"] for event in body["events"])
    assert body["next_cursor"] == body["events"][-1]["id"]
    assert body["has_more"] is False
    assert "metadata" not in response.text
    assert "goal_id" not in response.text
    assert "action_digest" not in response.text
    assert "authorization_fingerprint" not in response.text
    assert "arguments" not in response.text
    assert owner["access_token"] not in response.text
    assert companion["access_token"] not in response.text

    invalid = client.get(
        f"/tasks/{owned['task_id']}/events", headers=auth("invalid-task-events-token")
    )
    foreign_response = client.get(
        f"/tasks/{foreign['task_id']}/events", headers=auth(owner["access_token"])
    )
    unknown = client.get(
        "/tasks/unknown-task-id/events", headers=auth(owner["access_token"])
    )
    assert invalid.status_code == 401
    assert foreign_response.status_code == unknown.status_code == 404
    assert foreign_response.json() == unknown.json()

    store = TaskStore(settings.task_database_path)
    try:
        assert store.list_attempts(owned["steps"][0]["step_id"]) == []
        assert store.get_task(owned["task_id"]).state.value == "ready"
        assert store.list_attempts(foreign["steps"][0]["step_id"]) == []
    finally:
        store.close()
    assert execution_calls == []


def test_event_cursor_pagination_replay_empty_and_invalid_inputs(task_event_api) -> None:
    client, settings, owner, _companion = task_event_api
    task = submit(client, owner["access_token"], "task-events-pagination-key-01")
    store = TaskStore(settings.task_database_path)
    try:
        all_events = store.list_events(task_id=task["task_id"])
        expected_ids = [event.id for event in all_events]
    finally:
        store.close()
    assert len(expected_ids) > 1

    first = client.get(
        f"/tasks/{task['task_id']}/events?after=0&limit=1",
        headers=auth(owner["access_token"]),
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    first_ids = [event["id"] for event in first_body["events"]]
    assert first_ids == expected_ids[:1]
    assert first_body["next_cursor"] == first_ids[-1]
    assert first_body["has_more"] is True

    replay = client.get(
        f"/tasks/{task['task_id']}/events?after=0&limit=1",
        headers=auth(owner["access_token"]),
    )
    assert [event["id"] for event in replay.json()["events"]] == first_ids

    second = client.get(
        f"/tasks/{task['task_id']}/events?after={first_body['next_cursor']}&limit=1",
        headers=auth(owner["access_token"]),
    )
    assert second.status_code == 200, second.text
    second_body = second.json()
    second_ids = [event["id"] for event in second_body["events"]]
    assert second_ids == expected_ids[1:]
    assert first_ids + second_ids == expected_ids
    assert len(set(first_ids + second_ids)) == len(expected_ids)
    assert second_body["has_more"] is False
    assert second_body["next_cursor"] == expected_ids[-1]

    empty = client.get(
        f"/tasks/{task['task_id']}/events?after={expected_ids[-1]}&limit=1",
        headers=auth(owner["access_token"]),
    )
    assert empty.status_code == 200, empty.text
    assert empty.json() == {
        "events": [],
        "next_cursor": expected_ids[-1],
        "has_more": False,
    }

    for query in (
        "after=-1",
        "after=9223372036854775808",
        "after=not-an-integer",
        "limit=0",
        "limit=101",
    ):
        invalid = client.get(
            f"/tasks/{task['task_id']}/events?{query}",
            headers=auth(owner["access_token"]),
        )
        assert invalid.status_code == 422, (query, invalid.text)

    ahead = client.get(
        f"/tasks/{task['task_id']}/events?after={expected_ids[-1] + 1}",
        headers=auth(owner["access_token"]),
    )
    assert ahead.status_code == 422


def test_nonzero_event_cursor_must_belong_to_requested_task(task_event_api) -> None:
    client, settings, owner, _companion = task_event_api
    task_a = submit(client, owner["access_token"], "task-events-cursor-a-0001")
    task_b = submit(client, owner["access_token"], "task-events-cursor-b-0001")

    store = TaskStore(settings.task_database_path)
    try:
        task_a_before = store.list_events(task_id=task_a["task_id"])
        task_b_events = store.list_events(task_id=task_b["task_id"])
        store.transition_task(task_a["task_id"], TaskState.RUNNING)
        task_a_events = store.list_events(task_id=task_a["task_id"])
    finally:
        store.close()

    assert task_a_before[0].id < task_b_events[-1].id < task_a_events[-1].id
    foreign_cursor = client.get(
        f"/tasks/{task_a['task_id']}/events?after={task_b_events[-1].id}",
        headers=auth(owner["access_token"]),
    )
    assert foreign_cursor.status_code == 422

    legitimate_cursor = client.get(
        f"/tasks/{task_a['task_id']}/events?after={task_a_before[0].id}&limit=1",
        headers=auth(owner["access_token"]),
    )
    assert legitimate_cursor.status_code == 200, legitimate_cursor.text
    body = legitimate_cursor.json()
    assert [event["id"] for event in body["events"]] == [task_a_before[1].id]
    assert body["next_cursor"] == task_a_before[1].id
    assert body["has_more"] is True


def test_events_are_pollable_after_task_store_reopen(tmp_path) -> None:
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings)) as client:
        owner, _companion = pair_devices(client, settings)
        task = submit(client, owner["access_token"], "task-events-reopen-key-0001")
        first = client.get(
            f"/tasks/{task['task_id']}/events", headers=auth(owner["access_token"])
        )
        assert first.status_code == 200, first.text
        first_ids = [event["id"] for event in first.json()["events"]]

    with TestClient(create_app(settings)) as reopened_client:
        reopened = reopened_client.get(
            f"/tasks/{task['task_id']}/events", headers=auth(owner["access_token"])
        )
        assert reopened.status_code == 200, reopened.text
        assert [event["id"] for event in reopened.json()["events"]] == first_ids
