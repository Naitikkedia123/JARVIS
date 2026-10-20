"""Authenticated, bounded and idempotent task submission API tests."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from jarvis.config import Settings
from jarvis.main import create_app
from jarvis.tasks.api import MAX_TASK_REQUEST_BYTES
from jarvis.tasks.store import TaskStore

BOOTSTRAP = "submission-test-bootstrap-0123456789"
OWNER_KEY = "submission-test-key-00001"
COMPANION_KEY = "submission-test-key-00002"


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def request_body(
    *,
    key: str = OWNER_KEY,
    title: str = "Read local status",
    description: str = "",
    tool_name: str = "jarvis.status",
    arguments: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "idempotency_key": key,
        "title": title,
        "description": description,
        "action": {
            "tool_name": tool_name,
            "arguments": {} if arguments is None else arguments,
        },
    }


@pytest.fixture
def submission_api(tmp_path):
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
        submission_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "allowed_device_ids": [owner["device"]["id"]],
                }
            ),
            encoding="utf-8",
        )
        yield client, owner, settings, submission_path


def test_new_and_identical_requests_are_idempotent_and_never_execute(submission_api) -> None:
    client, owner, _settings, submission_path = submission_api
    # A dispatch allowlist is a separate policy. Its absence must not prevent a
    # narrowly authorized task from being persisted as a ready plan.
    first = client.post("/tasks", headers=auth(owner["access_token"]), json=request_body())
    assert first.status_code == 201, first.text
    assert first.headers["location"] == f"/tasks/{first.json()['task_id']}"
    assert first.headers["cache-control"] == "no-store"
    assert first.json()["status"] == "ready"
    assert first.json()["steps"][0]["state"] == "pending"
    assert first.json()["steps"][0]["attempts"] == []

    replay = client.post("/tasks", headers=auth(owner["access_token"]), json=request_body())
    assert replay.status_code == 200, replay.text
    assert replay.json()["task_id"] == first.json()["task_id"]
    assert replay.json()["status"] == "ready"

    services = client.app.state._task_services
    runner = services.authorized_runner
    assert runner is not None
    assert runner.dispatcher._audit_sink.events == []
    stored = TaskStore(_settings.task_database_path)
    assert stored.get_task(first.json()["task_id"]).state.value == "ready"
    assert stored.list_attempts(first.json()["steps"][0]["step_id"]) == []
    stored.close()


def test_default_deny_and_dispatch_grants_do_not_authorize_submission(submission_api) -> None:
    client, owner, _settings, submission_path = submission_api
    submission_path.write_text(
        json.dumps({"schema_version": 1, "allowed_device_ids": []}),
        encoding="utf-8",
    )
    # A dispatch grant alone does not satisfy the independent submit policy.
    (submission_path.parent / "dispatch-grants.json").write_text(
        json.dumps({"schema_version": 1, "allowed_device_ids": [owner["device"]["id"]]}),
        encoding="utf-8",
    )
    denied = client.post("/tasks", headers=auth(owner["access_token"]), json=request_body())
    assert denied.status_code == 403
    assert denied.json() == {
        "error": {
            "code": "task_submission_denied",
            "message": "Task submission is not authorized.",
        }
    }

    submission_path.unlink()
    unavailable = client.post(
        "/tasks",
        headers=auth(owner["access_token"]),
        json=request_body(key="submission-test-key-00003"),
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "task_submission_unavailable"


def test_replay_rechecks_current_allowlist_before_idempotency(submission_api) -> None:
    client, owner, _settings, submission_path = submission_api
    first = client.post("/tasks", headers=auth(owner["access_token"]), json=request_body())
    assert first.status_code == 201

    # The same request is denied after local revocation; a prior success is not
    # cached as permission to replay.
    submission_path.write_text(
        json.dumps({"schema_version": 1, "allowed_device_ids": []}),
        encoding="utf-8",
    )
    revoked_replay = client.post(
        "/tasks",
        headers=auth(owner["access_token"]),
        json=request_body(),
    )
    assert revoked_replay.status_code == 403

    # Restoring an explicit grant allows the replay and the original task ID is
    # returned by the existing (device ID, idempotency key) storage contract.
    submission_path.write_text(
        json.dumps(
            {"schema_version": 1, "allowed_device_ids": [owner["device"]["id"]]}
        ),
        encoding="utf-8",
    )
    restored = client.post("/tasks", headers=auth(owner["access_token"]), json=request_body())
    assert restored.status_code == 200
    assert restored.json()["task_id"] == first.json()["task_id"]


@pytest.mark.parametrize(
    "changed",
    [
        {"title": "Changed title"},
        {"description": "Changed description"},
    ],
)
def test_changed_authorized_content_conflicts_for_same_device_key(
    submission_api,
    changed,
) -> None:
    client, owner, _settings, _submission_path = submission_api
    first = client.post("/tasks", headers=auth(owner["access_token"]), json=request_body())
    assert first.status_code == 201
    conflict = client.post(
        "/tasks",
        headers=auth(owner["access_token"]),
        json=request_body(**changed),
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "task_submission_conflict"


@pytest.mark.parametrize(
    "tool_name,arguments",
    [
        ("workspace.read_file", {}),
        ("jarvis.status", {"unexpected": True}),
    ],
)
def test_unsupported_action_is_denied_before_idempotency_conflict(
    submission_api,
    tool_name,
    arguments,
) -> None:
    client, owner, _settings, _submission_path = submission_api
    first = client.post("/tasks", headers=auth(owner["access_token"]), json=request_body())
    assert first.status_code == 201
    changed_action = client.post(
        "/tasks",
        headers=auth(owner["access_token"]),
        json=request_body(tool_name=tool_name, arguments=arguments),
    )
    assert changed_action.status_code == 403
    assert changed_action.json()["error"]["code"] == "task_submission_denied"


def test_same_key_is_scoped_per_authenticated_device(submission_api) -> None:
    client, owner, _settings, submission_path = submission_api
    first = client.post("/tasks", headers=auth(owner["access_token"]), json=request_body())
    code = client.post(
        "/devices/pairing-codes",
        headers=auth(owner["access_token"]),
    ).json()["pairing_code"]
    companion = client.post(
        "/devices/pair",
        json={"pairing_code": code, "name": "companion", "platform": "android"},
    ).json()
    submission_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "allowed_device_ids": [owner["device"]["id"], companion["device"]["id"]],
            }
        ),
        encoding="utf-8",
    )

    second = client.post(
        "/tasks",
        headers=auth(companion["access_token"]),
        json=request_body(),
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["task_id"] != second.json()["task_id"]

    client.delete(
        f"/devices/{companion['device']['id']}",
        headers=auth(owner["access_token"]),
    )
    revoked_retry = client.post(
        "/tasks",
        headers=auth(companion["access_token"]),
        json=request_body(),
    )
    assert revoked_retry.status_code == 401


def test_request_size_duplicate_keys_and_deep_nesting_are_rejected(submission_api) -> None:
    client, owner, _settings, _submission_path = submission_api
    oversized = json.dumps(
        {
            **request_body(key="submission-test-key-00004"),
            "ignored": "x" * MAX_TASK_REQUEST_BYTES,
        }
    )
    too_large = client.post(
        "/tasks",
        headers={**auth(owner["access_token"]), "Content-Type": "application/json"},
        content=oversized,
    )
    assert too_large.status_code == 413

    duplicate = (
        '{"idempotency_key":"submission-test-key-00005",'
        '"title":"first","title":"second",'
        '"action":{"tool_name":"jarvis.status","arguments":{}}}'
    )
    duplicate_response = client.post(
        "/tasks",
        headers={**auth(owner["access_token"]), "Content-Type": "application/json"},
        content=duplicate,
    )
    assert duplicate_response.status_code == 422
    assert "first" not in duplicate_response.text
    assert "second" not in duplicate_response.text

    nested_value = "[" * 34 + "0" + "]" * 34
    nested = (
        '{"idempotency_key":"submission-test-key-00006",'
        '"title":"nested",'
        '"action":{"tool_name":"jarvis.status",'
        '"arguments":{"nested":' + nested_value + "}}}"
    )
    nested_response = client.post(
        "/tasks",
        headers={**auth(owner["access_token"]), "Content-Type": "application/json"},
        content=nested,
    )
    assert nested_response.status_code == 422


def test_openapi_publishes_submit_and_owner_scoped_status_routes(submission_api) -> None:
    client, _owner, _settings, _submission_path = submission_api
    paths = client.get("/openapi.json").json()["paths"]
    assert set(paths["/tasks"]) == {"post"}
    assert set(paths["/tasks/{task_id}"]) == {"get"}

