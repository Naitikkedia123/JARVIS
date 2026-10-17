from fastapi.testclient import TestClient

from jarvis.main import app


def test_health_endpoint_returns_expected_payload() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "jarvis"}


def test_readiness_endpoint_returns_deterministic_payload() -> None:
    with TestClient(app) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "service": "jarvis"}
