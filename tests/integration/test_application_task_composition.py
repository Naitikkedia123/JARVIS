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

