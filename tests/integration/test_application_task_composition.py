"""Application wiring tests for private task services and public task routes."""

from __future__ import annotations

from fastapi.testclient import TestClient
from pydantic import SecretStr

from jarvis.config import Settings
from jarvis.main import _build_task_application_services, create_app
from jarvis.policy.allowlist import DeviceTaskToolPolicy
from jarvis.policy.device_grants import LocalDeviceTaskGrantPolicy
from jarvis.policy.task_submission import LocalTaskSubmissionPolicy
from jarvis.tasks.runner import DispatcherBackedTaskRunner
from jarvis.tools.dispatcher import ToolDispatcher

BOOTSTRAP = "composition-test-bootstrap-0123456789"


def test_task_services_exist_without_constructing_authorized_runner(tmp_path) -> None:
    settings = Settings(
        device_database_path=tmp_path / "devices.sqlite3",
        task_database_path=tmp_path / "tasks.sqlite3",
        device_task_grants_path=tmp_path / "dispatch-grants.json",
        task_submission_grants_path=tmp_path / "submission-grants.json",
    )
    with TestClient(create_app(settings)) as client:
        services = client.app.state._task_services
        assert services is not None
        assert services.runtime.store is services.store
        assert services.authorized_runner is None
        assert services.runtime.authorized_runner is None
        assert isinstance(services.submission_policy, LocalTaskSubmissionPolicy)

        paths = client.get("/openapi.json").json()["paths"]
        assert set(paths["/tasks"]) == {"post"}
        assert set(paths["/tasks/{task_id}"]) == {"get"}
        assert set(paths["/tasks/{task_id}/resume"]) == {"post"}
        assert set(paths["/tasks/approvals/{approval_id}/decision"]) == {"post"}


def test_device_service_composition_keeps_submission_and_dispatch_policies_separate(
    tmp_path,
) -> None:
    settings = Settings(
        device_database_path=tmp_path / "devices.sqlite3",
        task_database_path=tmp_path / "tasks.sqlite3",
        device_task_grants_path=tmp_path / "dispatch-grants.json",
        task_submission_grants_path=tmp_path / "submission-grants.json",
        device_bootstrap_code=SecretStr(BOOTSTRAP),
    )
    with TestClient(create_app(settings)) as client:
        services = client.app.state._task_services
        runner = services.authorized_runner
        assert isinstance(runner, DispatcherBackedTaskRunner)
        assert services.runtime.authorized_runner is runner
        assert isinstance(runner.policy, LocalDeviceTaskGrantPolicy)
        assert isinstance(services.submission_policy, LocalTaskSubmissionPolicy)
        assert runner.policy is not services.submission_policy
        assert type(runner.dispatcher) is ToolDispatcher
        assert type(runner.dispatcher._policy) is DeviceTaskToolPolicy
        assert runner.dispatcher._registry.is_sealed
        assert runner.dispatcher._registry.get("jarvis.status").name == "jarvis.status"
        assert set(runner.dispatcher._executors) == {"jarvis.status"}


def test_service_builder_uses_the_injected_task_store_and_policy_paths(tmp_path) -> None:
    from jarvis.tasks.store import TaskStore

    settings = Settings(
        device_database_path=tmp_path / "devices.sqlite3",
        task_database_path=tmp_path / "tasks.sqlite3",
        device_task_grants_path=tmp_path / "dispatch-grants.json",
        task_submission_grants_path=tmp_path / "submission-grants.json",
    )
    store = TaskStore(settings.task_database_path)
    try:
        services = _build_task_application_services(settings, None, store)
        assert services.store is store
        assert services.runtime.store is store
        assert services.submission_policy._config_path == settings.task_submission_grants_path
    finally:
        store.close()


def test_approval_routes_require_authenticated_owner_and_hide_unknown_ids(tmp_path) -> None:
    settings = Settings(
        device_database_path=tmp_path / "devices.sqlite3",
        task_database_path=tmp_path / "tasks.sqlite3",
        device_task_grants_path=tmp_path / "dispatch-grants.json",
        task_submission_grants_path=tmp_path / "submission-grants.json",
        device_bootstrap_code=SecretStr(BOOTSTRAP),
    )
    with TestClient(create_app(settings)) as client:
        owner_pair = client.post(
            "/devices/pair",
            json={
                "pairing_code": BOOTSTRAP,
                "name": "owner",
                "platform": "windows",
            },
        )
        assert owner_pair.status_code == 201
        owner_token = owner_pair.json()["access_token"]
        pairing_response = client.post(
            "/devices/pairing-codes",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert pairing_response.status_code == 201
        companion_pair = client.post(
            "/devices/pair",
            json={
                "pairing_code": pairing_response.json()["pairing_code"],
                "name": "companion",
                "platform": "android",
            },
        )
        assert companion_pair.status_code == 201
        companion_token = companion_pair.json()["access_token"]

        approval_path = "/tasks/approvals/00000000-0000-0000-0000-000000000001/decision"
        unauthenticated = client.post(approval_path, json={"decision": "approve"})
        assert unauthenticated.status_code == 401
        companion_result = client.post(
            approval_path,
            json={"decision": "approve"},
            headers={"Authorization": f"Bearer {companion_token}"},
        )
        assert companion_result.status_code == 404
        owner_result = client.post(
            approval_path,
            json={"decision": "approve"},
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert owner_result.status_code == 404
        assert "request" in owner_result.json()["error"]["message"].lower()

        resume_path = "/tasks/unknown-task/resume"
        unauthenticated_resume = client.post(resume_path)
        assert unauthenticated_resume.status_code == 401
        foreign_resume = client.post(
            resume_path,
            headers={"Authorization": f"Bearer {companion_token}"},
        )
        assert foreign_resume.status_code == 404
        for response in (
            unauthenticated,
            companion_result,
            owner_result,
            unauthenticated_resume,
            foreign_resume,
        ):
            assert owner_token not in response.text
            assert companion_token not in response.text

