"""Owner-scoped task status API contract and result projection tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from pydantic import SecretStr

from jarvis.config import Settings
from jarvis.main import create_app
from jarvis.tasks.action import AuthorizedTaskAction
from jarvis.tasks.models import (
    AttemptAuthorization,
    AttemptOutcome,
    SafeErrorCode,
    StepState,
    TaskState,
)
from jarvis.tasks.store import TaskStore

BOOTSTRAP = "status-test-bootstrap-0123456789"
IDEMPOTENCY_KEY = "status-test-key-00001"
VALID_STATUS = {
    "component": "jarvis-control-plane",
    "state": "responding",
    "external_services_checked": False,
}


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def payload(key: str = IDEMPOTENCY_KEY) -> dict[str, object]:
    return {
        "idempotency_key": key,
        "title": "Read status",
        "action": {"tool_name": "jarvis.status", "arguments": {}},
    }


def make_status_client(tmp_path):
    config_path = tmp_path / "submission-grants.json"
    settings = Settings(
        device_database_path=tmp_path / "devices.sqlite3",
        task_database_path=tmp_path / "tasks.sqlite3",
        device_bootstrap_code=SecretStr(BOOTSTRAP),
        task_submission_grants_path=config_path,
    )
    client = TestClient(create_app(settings))
    client.__enter__()
    owner = client.post(
        "/devices/pair",
        json={"pairing_code": BOOTSTRAP, "name": "owner", "platform": "windows"},
    ).json()
    config_path.write_text(
        json.dumps({"schema_version": 1, "allowed_device_ids": [owner["device"]["id"]]}),
        encoding="utf-8",
    )
    return client, owner, settings.task_database_path


def submit(client: TestClient, token: str, key: str = IDEMPOTENCY_KEY):
    response = client.post("/tasks", headers=auth(token), json=payload(key))
    assert response.status_code == 201, response.text
    return response.json()


def test_status_is_owner_scoped_authenticated_and_read_only(tmp_path) -> None:
    client, owner, database = make_status_client(tmp_path)
    try:
        task = submit(client, owner["access_token"])
        task_id = task["task_id"]

        missing = client.get(f"/tasks/{task_id}")
        assert missing.status_code == 401
        assert missing.headers["www-authenticate"] == "Bearer"

        code = client.post(
            "/devices/pairing-codes",
            headers=auth(owner["access_token"]),
        ).json()["pairing_code"]
        companion = client.post(
            "/devices/pair",
            json={"pairing_code": code, "name": "companion", "platform": "android"},
        ).json()
        foreign = client.get(
            f"/tasks/{task_id}",
            headers=auth(companion["access_token"]),
        )
        unknown = client.get(
            "/tasks/unknown-task-id",
            headers=auth(owner["access_token"]),
        )
        assert foreign.status_code == unknown.status_code == 404
        assert foreign.json() == unknown.json()

        before = TaskStore(database)
        before_attempts = before.list_attempts(task["steps"][0]["step_id"])
        before.close()
        response = client.get(f"/tasks/{task_id}", headers=auth(owner["access_token"]))
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert body["status"] == body["state"] == "ready"
        assert body["created_at"] and body["updated_at"]
        assert body["progress"] == {"completed_steps": 0, "total_steps": 1}
        assert body["steps"][0]["state"] == "pending"
        assert body["steps"][0]["dependencies"] == []
        assert body["steps"][0]["attempts"] == []
        assert body["result"] is None
        assert before_attempts == []

        after = TaskStore(database)
        assert after.list_attempts(task["steps"][0]["step_id"]) == before_attempts
        after.close()
    finally:
        client.__exit__(None, None, None)


def test_top_level_result_requires_single_exact_status_step_and_valid_output(tmp_path) -> None:
    client, owner, database = make_status_client(tmp_path)
    try:
        task = submit(client, owner["access_token"])
        step_id = task["steps"][0]["step_id"]
        seed = TaskStore(database)
        seed._connection.execute(
            "UPDATE steps SET result_json = ? WHERE id = ?",
            (json.dumps({"data": VALID_STATUS}), step_id),
        )
        seed.close()

        response = client.get(
            f"/tasks/{task['task_id']}",
            headers=auth(owner["access_token"]),
        )
        assert response.json()["result"] == VALID_STATUS
        assert response.json()["steps"][0]["result"] == VALID_STATUS

        seed = TaskStore(database)
        seed._connection.execute(
            "UPDATE steps SET result_json = ? WHERE id = ?",
            (json.dumps({"data": {"unrecognized": "private"}}), step_id),
        )
        seed.close()
        invalid = client.get(
            f"/tasks/{task['task_id']}",
            headers=auth(owner["access_token"]),
        )
        assert invalid.status_code == 200
        assert invalid.json()["result"] is None
        assert invalid.json()["steps"][0]["result"] is None

        seed = TaskStore(database)
        action = AuthorizedTaskAction(
            tool_name="workspace.read_file",
            arguments={"path": "private.txt"},
        )
        seed._connection.execute(
            "UPDATE steps SET action_json = ?, result_json = ? WHERE id = ?",
            (action.model_dump_json(), json.dumps({"data": VALID_STATUS}), step_id),
        )
        seed.close()
        wrong_action = client.get(
            f"/tasks/{task['task_id']}",
            headers=auth(owner["access_token"]),
        )
        assert wrong_action.status_code == 200
        assert wrong_action.json()["result"] is None
        assert wrong_action.json()["steps"][0]["result"] is None
    finally:
        client.__exit__(None, None, None)


def test_multi_step_task_never_aggregates_top_level_result(tmp_path) -> None:
    client, owner, database = make_status_client(tmp_path)
    try:
        task = submit(client, owner["access_token"])
        first_step = task["steps"][0]["step_id"]
        seed = TaskStore(database)
        now = datetime.now(UTC).isoformat(timespec="microseconds")
        second_step = "zz-second-step"
        seed._connection.execute(
            """INSERT INTO steps
                (id, task_id, title, state, replay_safety, idempotency_key,
                 attempt_limit, attempt_count, created_at, updated_at, action_json)
                VALUES (?, ?, ?, 'pending', 'pure', NULL, 3, 0, ?, ?, NULL)""",
            (second_step, task["task_id"], "Second step", now, now),
        )
        seed._connection.execute(
            """INSERT INTO step_dependencies(task_id, step_id, depends_on_step_id)
                VALUES (?, ?, ?)""",
            (task["task_id"], second_step, first_step),
        )
        seed._connection.execute(
            "UPDATE steps SET result_json = ? WHERE id = ?",
            (json.dumps({"data": VALID_STATUS}), first_step),
        )
        seed.close()

        response = client.get(
            f"/tasks/{task['task_id']}",
            headers=auth(owner["access_token"]),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["progress"]["total_steps"] == 2
        assert body["result"] is None
        second = next(step for step in body["steps"] if step["step_id"] == second_step)
        assert second["dependencies"] == [first_step]
    finally:
        client.__exit__(None, None, None)


def test_status_projects_attempt_history_and_only_safe_error_codes(tmp_path) -> None:
    client, owner, database = make_status_client(tmp_path)
    try:
        task = submit(client, owner["access_token"])
        step_id = task["steps"][0]["step_id"]
        seed = TaskStore(database)
        plan = seed.get_task_plan_for_device(task["task_id"], owner["device"]["id"])
        action = plan.steps[0].action
        assert action is not None
        seed.transition_task(task["task_id"], TaskState.RUNNING)
        seed.transition_step(step_id, StepState.READY)
        seed.start_step_attempt(
            step_id,
            authorization=AttemptAuthorization(
                device_id=owner["device"]["id"],
                action_digest=action.digest(),
                authorization_fingerprint="a" * 64,
            ),
        )
        seed.finish_step_attempt(
            step_id,
            AttemptOutcome.PERMANENT_FAILURE,
            error_code=SafeErrorCode.PERMANENT,
        )
        seed.close()

        response = client.get(
            f"/tasks/{task['task_id']}",
            headers=auth(owner["access_token"]),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == body["state"] == "running"
        step = body["steps"][0]
        assert step["state"] == "failed"
        assert step["attempt_count"] == 1
        assert step["latest_attempt_outcome"] == "permanent_failure"
        assert step["latest_error_code"] == "permanent"
        assert len(step["attempts"]) == 1
        assert step["attempts"][0]["outcome"] == "permanent_failure"
        assert step["attempts"][0]["error_code"] == "permanent"
        assert step["attempts"][0]["started_at"]
        assert step["attempts"][0]["finished_at"]
        assert set(step["attempts"][0]) == {
            "attempt_number",
            "outcome",
            "error_code",
            "started_at",
            "finished_at",
        }
        assert "authorization" not in response.text
        assert "fingerprint" not in response.text
    finally:
        client.__exit__(None, None, None)

