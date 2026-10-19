"""Persistent device pairing, credential and revocation tests."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr, ValidationError

from jarvis.devices.models import Device, DevicePlatform, DeviceRole, PairingMetadata
from jarvis.devices.service import (
    DeviceAuthenticationError,
    DeviceAuthorizationError,
    DeviceService,
)
from jarvis.devices.store import (
    DevicePairingUnavailableError,
    DeviceStore,
    DeviceStoreError,
)

BOOTSTRAP_CODE = "0a92b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f80"
NOW = datetime(2026, 10, 19, 8, 30, tzinfo=UTC)


def make_service(path, *, clock=lambda: NOW, bootstrap_code=BOOTSTRAP_CODE):
    store = DeviceStore(path, clock=clock)
    return store, DeviceService(
        store,
        bootstrap_code=SecretStr(bootstrap_code) if bootstrap_code is not None else None,
        clock=clock,
    )


def pair_owner(service: DeviceService):
    return service.pair(
        SecretStr(BOOTSTRAP_CODE),
        name="JARVIS workstation",
        platform=DevicePlatform.WINDOWS,
        reported_capabilities=frozenset({"task.submit", "memory.context"}),
    )


def test_bootstrap_pairing_persists_identity_and_only_token_digest(tmp_path) -> None:
    database = tmp_path / "devices.sqlite3"
    store, service = make_service(database)

    with pytest.raises(DevicePairingUnavailableError, match="invalid or unavailable"):
        service.pair(SecretStr("wrong"), name="untrusted", platform=DevicePlatform.WEB)
    assert store._connection.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 0
    assert store._connection.execute("SELECT used_at FROM pairing_codes").fetchone()[0] is None

    receipt = pair_owner(service)
    token = receipt.access_token.get_secret_value()
    device = service.authenticate(receipt.access_token)
    assert device.id == receipt.device.id
    assert device.role is DeviceRole.OWNER
    assert device.reported_capabilities == frozenset({"task.submit", "memory.context"})
    assert store.schema_version == 1
    digest = hashlib.sha256(token.encode()).hexdigest()
    persisted_digest = store._connection.execute(
        "SELECT token_digest FROM devices WHERE id = ?", (device.id,)
    ).fetchone()[0]
    assert persisted_digest == digest
    stored_record = tuple(
        store._connection.execute(
            "SELECT name, platform, role, capabilities_json, token_digest FROM devices"
        ).fetchone()
    )
    assert token not in repr(stored_record)
    store.close()

    reopened, restored_service = make_service(database)
    assert restored_service.authenticate(SecretStr(token)).id == device.id
    with pytest.raises(DevicePairingUnavailableError, match="invalid or unavailable"):
        pair_owner(restored_service)
    reopened.close()


def test_owner_can_pair_member_issue_code_and_revoke_member(tmp_path) -> None:
    store, service = make_service(tmp_path / "lifecycle.sqlite3")
    owner = pair_owner(service)
    owner_token = owner.access_token
    stale_code = service.issue_pairing_code(owner_token)
    pairing_code = service.issue_pairing_code(owner_token)
    with pytest.raises(DevicePairingUnavailableError, match="invalid or unavailable"):
        service.pair(
            stale_code,
            name="superseded code",
            platform=DevicePlatform.ANDROID,
        )
    member = service.pair(
        pairing_code,
        name="JARVIS Android",
        platform=DevicePlatform.ANDROID,
        reported_capabilities=frozenset({"notifications"}),
    )

    assert member.device.role is DeviceRole.COMPANION
    assert [device.id for device in service.list_devices(owner_token)] == [
        owner.device.id,
        member.device.id,
    ]
    with pytest.raises(DevicePairingUnavailableError, match="invalid or unavailable"):
        service.pair(
            pairing_code,
            name="replay",
            platform=DevicePlatform.ANDROID,
        )
    with pytest.raises(DeviceAuthorizationError, match="not authorized"):
        service.issue_pairing_code(member.access_token)
    with pytest.raises(DeviceAuthorizationError, match="not authorized"):
        service.revoke_device(member.access_token, owner.device.id)

    revoked = service.revoke_device(owner_token, member.device.id)
    assert revoked.revoked_at == NOW
    with pytest.raises(DeviceAuthenticationError, match="authentication failed"):
        service.authenticate(member.access_token)
    store.close()


def test_pairing_transaction_rolls_back_before_retry(tmp_path) -> None:
    store, service = make_service(tmp_path / "pair-rollback.sqlite3")
    owner = pair_owner(service)
    pairing_code = service.issue_pairing_code(owner.access_token)
    store._connection.execute(
        "CREATE TRIGGER fail_companion_insert BEFORE INSERT ON devices "
        "WHEN NEW.role = 'companion' "
        "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
    )

    with pytest.raises(DeviceStoreError, match="could not be completed"):
        service.pair(
            pairing_code,
            name="JARVIS Android",
            platform=DevicePlatform.ANDROID,
        )
    assert store._connection.execute(
        "SELECT COUNT(*) FROM devices WHERE role = 'companion'"
    ).fetchone()[0] == 0
    assert store._connection.execute(
        "SELECT used_at FROM pairing_codes WHERE owner_device_id = ?",
        (owner.device.id,),
    ).fetchone()[0] is None

    store._connection.execute("DROP TRIGGER fail_companion_insert")
    paired = service.pair(
        pairing_code,
        name="JARVIS Android",
        platform=DevicePlatform.ANDROID,
    )
    assert paired.device.role is DeviceRole.COMPANION
    store.close()


def test_capability_report_is_validated_and_does_not_change_device_role(tmp_path) -> None:
    store, service = make_service(tmp_path / "capabilities.sqlite3")
    owner = pair_owner(service)

    updated = service.update_reported_capabilities(
        owner.access_token, frozenset({"android.notifications", "browser.research"})
    )

    assert updated.role is DeviceRole.OWNER
    assert updated.reported_capabilities == frozenset(
        {"android.notifications", "browser.research"}
    )
    with pytest.raises(ValidationError):
        service.update_reported_capabilities(
            owner.access_token, frozenset({"can execute PowerShell"})
        )
    store.close()


def test_pairing_codes_expire_and_bootstrap_code_has_minimum_length(tmp_path) -> None:
    now = [NOW]

    def clock():
        return now[0]

    store, service = make_service(tmp_path / "expiry.sqlite3", clock=clock)
    owner = pair_owner(service)
    code = service.issue_pairing_code(owner.access_token)
    now[0] += timedelta(minutes=5)

    with pytest.raises(DevicePairingUnavailableError, match="invalid or unavailable"):
        service.pair(code, name="late device", platform=DevicePlatform.ANDROID)
    store.close()

    bad_store = DeviceStore(tmp_path / "short-code.sqlite3")
    with pytest.raises(ValueError, match="at least 32 characters"):
        DeviceService(bad_store, bootstrap_code=SecretStr("too-short"), clock=clock)
    bad_store.close()


def test_database_never_persists_pairing_code_or_plaintext_access_token(tmp_path) -> None:
    store, service = make_service(tmp_path / "secrets.sqlite3")
    receipt = pair_owner(service)
    code_digest = hashlib.sha256(BOOTSTRAP_CODE.encode()).hexdigest()
    token = receipt.access_token.get_secret_value()
    token_digest = hashlib.sha256(token.encode()).hexdigest()

    assert store._connection.execute(
        "SELECT code_digest FROM pairing_codes"
    ).fetchone()[0] == code_digest
    assert store._connection.execute(
        "SELECT token_digest FROM devices"
    ).fetchone()[0] == token_digest
    assert store._connection.execute(
        "SELECT COUNT(*) FROM pairing_codes WHERE code_digest = ?", (BOOTSTRAP_CODE,)
    ).fetchone()[0] == 0
    assert store._connection.execute(
        "SELECT COUNT(*) FROM devices WHERE token_digest = ?", (token,)
    ).fetchone()[0] == 0
    store.close()


def test_models_require_aware_timestamps_and_bounded_capabilities() -> None:
    with pytest.raises(ValidationError):
        PairingMetadata(name="   ", platform=DevicePlatform.ANDROID)
    with pytest.raises(ValidationError):
        PairingMetadata(
            name="phone",
            platform=DevicePlatform.ANDROID,
            reported_capabilities=frozenset({"can execute PowerShell"}),
        )
    with pytest.raises(ValidationError):
        Device(
            id="a" * 32,
            name="phone",
            platform=DevicePlatform.ANDROID,
            role=DeviceRole.COMPANION,
            paired_at=datetime(2026, 10, 19),
            last_seen_at=NOW,
        )


def test_schema_migration_is_versioned_and_database_path_is_file_backed(tmp_path) -> None:
    store = DeviceStore(tmp_path / "schema.sqlite3")
    assert store.schema_version == DeviceStore.current_schema_version == 1
    assert [
        tuple(row)
        for row in store._connection.execute(
            "SELECT version FROM device_schema_migrations ORDER BY version"
        ).fetchall()
    ] == [(1,)]
    store.close()
    with pytest.raises(ValueError, match="file-backed"):
        DeviceStore(":memory:")


def test_revocation_survives_database_reopen_and_owner_cannot_revoke_itself(tmp_path) -> None:
    database = tmp_path / "revoke-reopen.sqlite3"
    store, service = make_service(database)
    owner = pair_owner(service)
    member = service.pair(
        service.issue_pairing_code(owner.access_token),
        name="companion",
        platform=DevicePlatform.OTHER,
    )
    with pytest.raises(DeviceAuthorizationError, match="owner device"):
        service.revoke_device(owner.access_token, owner.device.id)
    service.revoke_device(owner.access_token, member.device.id)
    store.close()

    reopened, restored = make_service(database)
    with pytest.raises(DeviceAuthenticationError, match="authentication failed"):
        restored.authenticate(member.access_token)
    assert restored.authenticate(owner.access_token).role is DeviceRole.OWNER
    reopened.close()


def test_invalid_database_schema_is_normalized(tmp_path) -> None:
    database = tmp_path / "newer-schema.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE device_schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO device_schema_migrations VALUES (99, '2026-01-01T00:00:00+00:00')"
    )
    connection.commit()
    connection.close()

    with pytest.raises(DeviceStoreError, match="newer than this application supports"):
        DeviceStore(database)
