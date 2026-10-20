"""Authenticated device API tests with isolated local SQLite databases."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from jarvis.config import Settings
from jarvis.devices.api import DeviceRequestSizeLimitMiddleware
from jarvis.main import create_app

BOOTSTRAP = "test-bootstrap-code-0123456789-0123456789"


@pytest.fixture
def device_api(tmp_path, monkeypatch):
    token_numbers = itertools.count()
    monkeypatch.setattr(
        "jarvis.devices.service.secrets.token_urlsafe",
        lambda _nbytes: f"test-issued-device-credential-{next(token_numbers)}",
    )
    settings = Settings(
        device_database_path=tmp_path / "devices.sqlite3",
        device_bootstrap_code=SecretStr(BOOTSTRAP),
    )
    application = create_app(settings)
    with TestClient(application) as client:
        yield client, application, settings.device_database_path


def pair(client: TestClient, code: str, name: str = "phone") -> dict[str, object]:
    response = client.post(
        "/devices/pair",
        json={"pairing_code": code, "name": name, "platform": "android"},
    )
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_owner_and_companion_routes_enforce_role_and_revoke_persistently(device_api) -> None:
    client, _application, _database = device_api
    owner_receipt = pair(client, BOOTSTRAP, "owner")
    assert set(owner_receipt) == {"device", "access_token"}
    owner = owner_receipt["device"]
    owner_token = owner_receipt["access_token"]

    missing = client.get("/devices/me")
    assert missing.headers["www-authenticate"] == "Bearer"
    unknown = client.get("/devices/me", headers=auth("unknown-token"))
    assert unknown.status_code == 401
    assert unknown.json() == missing.json()
    malformed = client.get("/devices/me", headers={"Authorization": "Basic invalid"})
    assert malformed.status_code == 401
    assert malformed.json() == missing.json()

    current = client.get("/devices/me", headers=auth(owner_token))
    assert current.status_code == 200
    assert current.json()["id"] == owner["id"]
    assert "token_digest" not in current.text
    assert "code_digest" not in current.text

    issued = client.post("/devices/pairing-codes", headers=auth(owner_token))
    assert issued.status_code == 201
    assert issued.headers["cache-control"] == "no-store"
    pairing_code = issued.json()["pairing_code"]
    companion_receipt = pair(client, pairing_code, "companion")
    companion = companion_receipt["device"]
    companion_token = companion_receipt["access_token"]

    listing = client.get("/devices", headers=auth(owner_token))
    assert listing.status_code == 200
    assert {item["role"] for item in listing.json()["devices"]} == {"owner", "companion"}
    assert client.get("/devices", headers=auth(companion_token)).status_code == 403
    assert client.post("/devices/pairing-codes", headers=auth(companion_token)).status_code == 403

    capabilities = client.patch(
        "/devices/me/capabilities",
        headers=auth(companion_token),
        json={"reported_capabilities": ["notifications"]},
    )
    assert capabilities.status_code == 200
    assert capabilities.json()["role"] == "companion"
    assert capabilities.json()["reported_capabilities"] == ["notifications"]

    revoked = client.delete(f"/devices/{companion['id']}", headers=auth(owner_token))
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None
    assert client.get("/devices/me", headers=auth(companion_token)).status_code == 401
    assert client.delete(f"/devices/{owner['id']}", headers=auth(owner_token)).status_code == 403


def test_pairing_codes_are_one_time_and_expire_over_the_api(device_api) -> None:
    client, application, _database = device_api
    owner = pair(client, BOOTSTRAP, "owner")
    owner_token = owner["access_token"]

    bootstrap_replay = client.post(
        "/devices/pair",
        json={"pairing_code": BOOTSTRAP, "name": "bootstrap-replay", "platform": "android"},
    )
    assert bootstrap_replay.status_code == 409

    service = application.state.device_service
    now = [datetime.now(UTC)]
    service._clock = lambda: now[0]
    service._store._clock = lambda: now[0]
    first = client.post("/devices/pairing-codes", headers=auth(owner_token)).json()["pairing_code"]
    second = client.post("/devices/pairing-codes", headers=auth(owner_token)).json()["pairing_code"]
    stale = client.post(
        "/devices/pair",
        json={"pairing_code": first, "name": "stale", "platform": "android"},
    )
    assert stale.status_code == 409

    current = client.post(
        "/devices/pair",
        json={"pairing_code": second, "name": "expiring", "platform": "android"},
    )
    assert current.status_code == 201
    replay = client.post(
        "/devices/pair",
        json={"pairing_code": second, "name": "replay", "platform": "android"},
    )
    assert replay.status_code == 409

    next_code = client.post("/devices/pairing-codes", headers=auth(owner_token)).json()["pairing_code"]
    now[0] += timedelta(minutes=5)
    expired = client.post(
        "/devices/pair",
        json={"pairing_code": next_code, "name": "expired", "platform": "android"},
    )
    assert expired.status_code == 409
    assert expired.json() == {
        "error": {"code": "pairing_unavailable", "message": "The pairing operation could not be completed."}
    }


def test_validation_and_response_safety_do_not_echo_credentials(device_api) -> None:
    client, _application, _database = device_api
    secret = "invalid-bootstrap-secret-that-must-not-echo"
    invalid = client.post(
        "/devices/pair",
        json={"pairing_code": secret, "name": "bad", "platform": "not-a-platform"},
    )
    assert invalid.status_code == 422
    assert invalid.json() == {
        "error": {"code": "invalid_request", "message": "Request validation failed."}
    }
    assert secret not in invalid.text
    invalid_name = client.post(
        "/devices/pair",
        json={"pairing_code": secret, "name": "   ", "platform": "windows"},
    )
    assert invalid_name.status_code == 422
    assert secret not in invalid_name.text

    oversized = client.post("/devices/pair", content=b"{" + b"x" * (16 * 1024) + b"}")
    assert oversized.status_code == 413
    assert "x" * 32 not in oversized.text

    result = pair(client, BOOTSTRAP, "owner")
    token = result["access_token"]
    issued = client.post("/devices/pairing-codes", headers=auth(token))
    assert issued.headers["cache-control"] == "no-store"
    pairing_code = issued.json()["pairing_code"]
    assert pairing_code.startswith("test-issued-device-credential-")
    assert pairing_code != token
    assert "access_token" not in issued.json()
    public_json = client.get("/devices", headers=auth(token)).text
    assert token not in public_json
    assert "token_digest" not in public_json
    assert "code_digest" not in public_json


def test_revoked_credential_remains_invalid_after_app_restart(tmp_path, monkeypatch) -> None:
    token_numbers = itertools.count()
    monkeypatch.setattr(
        "jarvis.devices.service.secrets.token_urlsafe",
        lambda _nbytes: f"restart-test-credential-{next(token_numbers)}",
    )
    database = tmp_path / "restarted-devices.sqlite3"
    first_settings = Settings(
        device_database_path=database,
        device_bootstrap_code=SecretStr(BOOTSTRAP),
    )
    with TestClient(create_app(first_settings)) as client:
        owner = pair(client, BOOTSTRAP, "owner")
        code = client.post(
            "/devices/pairing-codes", headers=auth(owner["access_token"])
        ).json()["pairing_code"]
        companion = pair(client, code, "companion")
        revoked = client.delete(
            f"/devices/{companion['device']['id']}", headers=auth(owner["access_token"])
        )
        assert revoked.status_code == 200
        companion_token = companion["access_token"]

    restarted_settings = Settings(device_database_path=database, device_bootstrap_code=None)
    with TestClient(create_app(restarted_settings)) as restarted:
        response = restarted.get("/devices/me", headers=auth(companion_token))
        assert response.status_code == 401
        assert response.json() == {
            "error": {"code": "authentication_required", "message": "Authentication is required."}
        }


def test_streamed_device_request_body_limit_applies_without_content_length() -> None:
    async def exercise() -> list[dict[str, object]]:
        async def app(scope, receive, send):
            await receive()

        sent: list[dict[str, object]] = []

        async def receive():
            return {"type": "http.request", "body": b"x" * 10, "more_body": False}

        async def send(message):
            sent.append(message)

        middleware = DeviceRequestSizeLimitMiddleware(app, max_bytes=8)
        await middleware(
            {"type": "http", "path": "/devices/pair", "headers": []},
            receive,
            send,
        )
        return sent

    sent = asyncio.run(exercise())
    assert sent[0]["status"] == 413
    assert b"request_too_large" in sent[1]["body"]


def test_pairing_fails_closed_without_explicit_bootstrap_configuration(tmp_path) -> None:
    database = tmp_path / "not-provisioned.sqlite3"
    application = create_app(Settings(device_database_path=database, device_bootstrap_code=None))
    with TestClient(application) as client:
        response = client.post(
            "/devices/pair",
            json={"pairing_code": BOOTSTRAP, "name": "owner", "platform": "windows"},
        )
    assert response.status_code == 503
    assert not database.exists()


def test_pairing_storage_failure_rolls_back_code_consumption(device_api) -> None:
    client, _application, database = device_api
    owner = pair(client, BOOTSTRAP, "owner")
    code = client.post(
        "/devices/pairing-codes", headers=auth(owner["access_token"])
    ).json()["pairing_code"]
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TRIGGER fail_companion_insert BEFORE INSERT ON devices "
        "WHEN NEW.role = 'companion' BEGIN SELECT RAISE(ABORT, 'private sqlite detail'); END"
    )
    connection.commit()

    failed = client.post(
        "/devices/pair",
        json={"pairing_code": code, "name": "phone", "platform": "android"},
    )
    assert failed.status_code == 503
    assert "private sqlite detail" not in failed.text
    assert connection.execute(
        "SELECT used_at FROM pairing_codes WHERE owner_device_id IS NOT NULL"
    ).fetchone()[0] is None

    connection.execute("DROP TRIGGER fail_companion_insert")
    connection.commit()
    connection.close()
    retried = client.post(
        "/devices/pair",
        json={"pairing_code": code, "name": "phone", "platform": "android"},
    )
    assert retried.status_code == 201
    assert "private sqlite detail" not in retried.text


def test_owner_credential_rotation_is_owner_only_uncached_and_invalidates_old_token(
    device_api,
) -> None:
    client, _application, database = device_api
    owner = pair(client, BOOTSTRAP, "owner")
    owner_token = owner["access_token"]
    pairing_code = client.post(
        "/devices/pairing-codes", headers=auth(owner_token)
    ).json()["pairing_code"]
    companion = pair(client, pairing_code, "companion")

    denied = client.post(
        "/devices/owner-credential/rotate",
        headers=auth(companion["access_token"]),
    )
    assert denied.status_code == 403
    assert owner_token not in denied.text
    assert companion["access_token"] not in denied.text

    rotated = client.post(
        "/devices/owner-credential/rotate",
        headers=auth(owner_token),
    )
    assert rotated.status_code == 200
    assert rotated.headers["cache-control"] == "no-store"
    assert rotated.headers["pragma"] == "no-cache"
    payload = rotated.json()
    assert set(payload) == {"device", "access_token", "expires_at"}
    replacement = payload["access_token"]
    assert replacement != owner_token
    assert owner_token not in rotated.text
    audit_connection = sqlite3.connect(database)
    audit_event = audit_connection.execute(
        "SELECT event_type, device_id, actor, origin, outcome "
        "FROM device_credential_audit_events"
    ).fetchone()
    audit_connection.close()
    assert audit_event == (
        "owner_credential_rotated",
        owner["device"]["id"],
        "authenticated_owner",
        "owner_rotation_api",
        "success",
    )

    old_auth = client.get("/devices/me", headers=auth(owner_token))
    assert old_auth.status_code == 401
    assert owner_token not in old_auth.text
    assert client.get("/devices/me", headers=auth(replacement)).status_code == 200

    connection = sqlite3.connect(database)
    stored_digest, stored_expiry = connection.execute(
        "SELECT token_digest, owner_credential_expires_at FROM devices WHERE role = 'owner'"
    ).fetchone()
    connection.close()
    assert stored_digest == hashlib.sha256(replacement.encode()).hexdigest()
    assert replacement not in stored_digest
    assert datetime.fromisoformat(stored_expiry) == datetime.fromisoformat(payload["expires_at"])


def test_owner_rotation_limit_rejects_without_mutation_or_success_audit(device_api) -> None:
    client, _application, database = device_api
    owner = pair(client, BOOTSTRAP, "owner")
    current_token = owner["access_token"]

    for _ in range(5):
        rotated = client.post(
            "/devices/owner-credential/rotate",
            headers=auth(current_token),
        )
        assert rotated.status_code == 200
        current_token = rotated.json()["access_token"]

    before = sqlite3.connect(database)
    digest_before, = before.execute(
        "SELECT token_digest FROM devices WHERE role = 'owner'"
    ).fetchone()
    audit_count_before, = before.execute(
        "SELECT COUNT(*) FROM device_credential_audit_events "
        "WHERE event_type = 'owner_credential_rotated'"
    ).fetchone()
    before.close()

    limited = client.post(
        "/devices/owner-credential/rotate",
        headers={**auth(current_token), "X-Forwarded-For": "203.0.113.77"},
    )
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "3600"
    assert client.get("/devices/me", headers=auth(current_token)).status_code == 200

    after = sqlite3.connect(database)
    digest_after, = after.execute(
        "SELECT token_digest FROM devices WHERE role = 'owner'"
    ).fetchone()
    audit_count_after, = after.execute(
        "SELECT COUNT(*) FROM device_credential_audit_events "
        "WHERE event_type = 'owner_credential_rotated'"
    ).fetchone()
    after.close()
    assert digest_after == digest_before
    assert audit_count_after == audit_count_before == 5
