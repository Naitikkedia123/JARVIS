from fastapi.testclient import TestClient

from jarvis.config import Settings
from jarvis.main import create_app


def test_health_endpoint_returns_expected_payload(tmp_path) -> None:
    application = create_app(Settings(device_database_path=tmp_path / "health.sqlite3"))
    with TestClient(application) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "jarvis"}


def test_readiness_endpoint_returns_deterministic_payload(tmp_path) -> None:
    application = create_app(Settings(device_database_path=tmp_path / "readiness.sqlite3"))
    with TestClient(application) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "service": "jarvis"}
