"""Persistent device pairing, credential and revocation tests."""

from __future__ import annotations

import hashlib
import io
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from pydantic import SecretStr, ValidationError

from jarvis.config import Settings
from jarvis.devices.admin import recover_owner_credential, run_recovery
from jarvis.devices.models import Device, DevicePlatform, DeviceRole, PairingMetadata
from jarvis.devices.service import (
    DeviceAuthenticationError,
    DeviceAuthorizationError,
    DeviceService,
    generate_bootstrap_code,
)
from jarvis.devices.store import (
    DevicePairingUnavailableError,
    DeviceStore,
    DeviceStoreError,
    OwnerCredentialRateLimitError,
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
    assert store.schema_version == 4
    assert device.credential_generation == 1
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


def test_generated_bootstrap_code_is_secret_wrapped_and_usable_once(tmp_path, monkeypatch) -> None:
    generated = "secure-generated-bootstrap-value-0123456789"
    calls = []

    def token_urlsafe(nbytes):
        calls.append(nbytes)
        return generated

    monkeypatch.setattr("jarvis.devices.service.secrets.token_urlsafe", token_urlsafe)
    bootstrap_code = generate_bootstrap_code()
    assert calls == [32]
    assert isinstance(bootstrap_code, SecretStr)
    assert repr(bootstrap_code) != generated

    database = tmp_path / "generated-bootstrap.sqlite3"
    store = DeviceStore(database)
    service = DeviceService(store, bootstrap_code=bootstrap_code, clock=lambda: NOW)
    owner = service.pair(
        bootstrap_code,
        name="owner",
        platform=DevicePlatform.WINDOWS,
    )
    assert owner.device.role is DeviceRole.OWNER
    digest = hashlib.sha256(generated.encode()).hexdigest()
    assert store._connection.execute(
        "SELECT code_digest FROM pairing_codes"
    ).fetchone()[0] == digest
    with pytest.raises(DevicePairingUnavailableError, match="invalid or unavailable"):
        service.pair(bootstrap_code, name="replay", platform=DevicePlatform.WEB)
    store.close()


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


def test_concurrent_companion_code_issuance_leaves_only_latest_code_usable(tmp_path) -> None:
    database = tmp_path / "concurrent-codes.sqlite3"
    store, service = make_service(database)
    owner = pair_owner(service)
    barrier = Barrier(2)

    def issue_concurrently() -> str:
        worker_store = DeviceStore(database, clock=lambda: NOW)
        worker_service = DeviceService(worker_store, bootstrap_code=None, clock=lambda: NOW)
        original_issue = worker_store.issue_pairing_code

        def wait_before_issue(owner_id, code_digest, expires_at):
            barrier.wait(timeout=5)
            return original_issue(owner_id, code_digest, expires_at)

        worker_store.issue_pairing_code = wait_before_issue
        try:
            return worker_service.issue_pairing_code(owner.access_token).get_secret_value()
        finally:
            worker_store.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        codes = list(executor.map(lambda _: issue_concurrently(), range(2)))

    assert store._connection.execute(
        "SELECT COUNT(*) FROM pairing_codes WHERE owner_device_id = ? AND used_at IS NULL",
        (owner.device.id,),
    ).fetchone()[0] == 1
    paired = []
    for index, code in enumerate(codes):
        try:
            paired.append(
                service.pair(
                    SecretStr(code),
                    name=f"companion-{index}",
                    platform=DevicePlatform.ANDROID,
                )
            )
        except DevicePairingUnavailableError:
            pass
    assert len(paired) == 1
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
    assert store.schema_version == DeviceStore.current_schema_version == 4
    assert [
        tuple(row)
        for row in store._connection.execute(
            "SELECT version FROM device_schema_migrations ORDER BY version"
        ).fetchall()
    ] == [(1,), (2,), (3,), (4,)]
    store.close()
    with pytest.raises(ValueError, match="file-backed"):
        DeviceStore(":memory:")


def test_migration_rejects_multiple_owners_before_adding_audit_schema(tmp_path) -> None:
    database = tmp_path / "multiple-owner-migration.sqlite3"
    store, service = make_service(database)
    owner = pair_owner(service)
    store.close()

    connection = sqlite3.connect(database)
    connection.execute("DROP INDEX one_device_owner")
    connection.execute("DROP TRIGGER credential_audit_no_update")
    connection.execute("DROP TRIGGER credential_audit_no_delete")
    connection.execute("DROP TABLE device_credential_audit_events")
    connection.execute("ALTER TABLE devices DROP COLUMN credential_generation")
    connection.execute("DELETE FROM device_schema_migrations WHERE version >= 3")
    connection.execute(
        "INSERT INTO devices "
        "(id, name, platform, role, capabilities_json, token_digest, paired_at, last_seen_at, "
        "revoked_at, owner_credential_expires_at) "
        "SELECT ?, ?, platform, role, capabilities_json, ?, paired_at, last_seen_at, revoked_at, "
        "owner_credential_expires_at FROM devices WHERE id = ?",
        ("b" * 32, "duplicate owner", "f" * 64, owner.device.id),
    )
    connection.commit()
    connection.close()

    with pytest.raises(DeviceStoreError, match="single-owner invariant"):
        DeviceStore(database)

    connection = sqlite3.connect(database)
    assert connection.execute(
        "SELECT COUNT(*) FROM devices WHERE role = 'owner'"
    ).fetchone()[0] == 2
    assert connection.execute(
        "SELECT MAX(version) FROM device_schema_migrations"
    ).fetchone()[0] == 2
    assert connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'device_credential_audit_events'"
    ).fetchone() is None
    connection.close()


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
    assert reopened.get_device(member.device.id, active_only=False).revoked_at == NOW
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


def test_owner_credential_expiry_is_persisted_and_checked_at_the_boundary(tmp_path) -> None:
    now = [NOW]
    database = tmp_path / "owner-expiry.sqlite3"
    store = DeviceStore(
        database,
        clock=lambda: now[0],
        owner_credential_lifetime_days=1,
    )
    service = DeviceService(
        store,
        bootstrap_code=SecretStr(BOOTSTRAP_CODE),
        clock=lambda: now[0],
    )
    owner = pair_owner(service)
    owner_token = owner.access_token.get_secret_value()
    expires_at = NOW + timedelta(days=1)
    assert datetime.fromisoformat(
        store._connection.execute(
            "SELECT owner_credential_expires_at FROM devices WHERE id = ?",
            (owner.device.id,),
        ).fetchone()[0]
    ) == expires_at

    companion = service.pair(
        service.issue_pairing_code(owner.access_token),
        name="companion",
        platform=DevicePlatform.ANDROID,
    )
    now[0] = expires_at - timedelta(microseconds=1)
    assert service.authenticate(owner.access_token).role is DeviceRole.OWNER
    assert service.authenticate(companion.access_token).role is DeviceRole.COMPANION

    now[0] = expires_at
    with pytest.raises(DeviceAuthenticationError, match="authentication failed"):
        service.authenticate(owner.access_token)
    assert service.authenticate(companion.access_token).role is DeviceRole.COMPANION
    store.close()

    reopened = DeviceStore(database, clock=lambda: now[0], owner_credential_lifetime_days=1)
    restored = DeviceService(reopened, bootstrap_code=None, clock=lambda: now[0])
    with pytest.raises(DeviceAuthenticationError, match="authentication failed"):
        restored.authenticate(SecretStr(owner_token))
    assert restored.authenticate(companion.access_token).role is DeviceRole.COMPANION
    reopened.close()


@pytest.mark.parametrize(
    "stored_expiry",
    [None, "invalid", "2026-10-20T08:30:00"],
    ids=["missing", "invalid", "naive"],
)
def test_malformed_or_missing_owner_expiry_fails_closed(tmp_path, stored_expiry) -> None:
    suffix = "missing" if stored_expiry is None else "invalid" if stored_expiry == "invalid" else "naive"
    store, service = make_service(tmp_path / f"bad-expiry-{suffix}.sqlite3")
    owner = pair_owner(service)
    store._connection.execute(
        "UPDATE devices SET owner_credential_expires_at = ? WHERE id = ?",
        (stored_expiry, owner.device.id),
    )
    with pytest.raises(DeviceAuthenticationError, match="authentication failed"):
        service.authenticate(owner.access_token)
    store.close()


def test_owner_credential_lifetime_setting_is_bounded() -> None:
    assert Settings(owner_credential_lifetime_days=1).owner_credential_lifetime_days == 1
    assert Settings(owner_credential_lifetime_days=365).owner_credential_lifetime_days == 365
    for days in (0, 366):
        with pytest.raises(ValidationError):
            Settings(owner_credential_lifetime_days=days)


def test_schema_v1_owner_receives_finite_migration_grace(tmp_path) -> None:
    database = tmp_path / "legacy-owner.sqlite3"
    initial, service = make_service(database)
    owner = pair_owner(service)
    initial.close()

    connection = sqlite3.connect(database)
    connection.execute("DROP TRIGGER credential_audit_no_update")
    connection.execute("DROP TRIGGER credential_audit_no_delete")
    connection.execute("DROP TABLE device_credential_audit_events")
    connection.execute("ALTER TABLE devices DROP COLUMN owner_credential_expires_at")
    connection.execute("ALTER TABLE devices DROP COLUMN credential_generation")
    connection.execute("DELETE FROM device_schema_migrations WHERE version IN (2, 3, 4)")
    connection.commit()
    connection.close()

    migrated_at = NOW + timedelta(days=4)
    clock = [migrated_at]
    migrated = DeviceStore(
        database,
        clock=lambda: clock[0],
        owner_credential_lifetime_days=2,
    )
    assert migrated.schema_version == 4
    assert [
        tuple(row)
        for row in migrated._connection.execute(
            "SELECT version FROM device_schema_migrations ORDER BY version"
        )
    ] == [(1,), (2,), (3,), (4,)]
    expiry = migrated_at + timedelta(days=2)
    assert datetime.fromisoformat(
        migrated._connection.execute(
            "SELECT owner_credential_expires_at FROM devices WHERE id = ?",
            (owner.device.id,),
        ).fetchone()[0]
    ) == expiry
    assert migrated.get_device(owner.device.id).credential_generation == 1
    migrated_service = DeviceService(migrated, bootstrap_code=None, clock=lambda: clock[0])
    assert migrated_service.authenticate(owner.access_token).role is DeviceRole.OWNER
    clock[0] = expiry
    with pytest.raises(DeviceAuthenticationError, match="authentication failed"):
        migrated_service.authenticate(owner.access_token)
    migrated.close()


def test_owner_rotation_replaces_digest_and_invalidates_previous_token(tmp_path) -> None:
    store = DeviceStore(
        tmp_path / "owner-rotation.sqlite3",
        clock=lambda: NOW,
        owner_credential_lifetime_days=2,
    )
    service = DeviceService(
        store,
        bootstrap_code=SecretStr(BOOTSTRAP_CODE),
        clock=lambda: NOW,
    )
    owner = pair_owner(service)
    old_token = owner.access_token.get_secret_value()
    old_digest = hashlib.sha256(old_token.encode()).hexdigest()

    receipt = service.rotate_owner_credential(owner.access_token)
    new_token = receipt.access_token.get_secret_value()
    new_digest = hashlib.sha256(new_token.encode()).hexdigest()
    row = store._connection.execute(
        "SELECT token_digest, owner_credential_expires_at, credential_generation "
        "FROM devices WHERE id = ?",
        (owner.device.id,),
    ).fetchone()
    assert new_token != old_token
    assert row["token_digest"] == new_digest
    assert row["credential_generation"] == 2
    assert receipt.device.credential_generation == 2
    assert old_digest != new_digest
    assert old_token not in repr(tuple(row))
    assert datetime.fromisoformat(row["owner_credential_expires_at"]) == NOW + timedelta(days=2)
    assert new_token not in repr(receipt)
    assert service.authenticate(receipt.access_token).id == owner.device.id
    with pytest.raises(DeviceAuthenticationError, match="authentication failed"):
        service.authenticate(owner.access_token)
    store.close()

    reopened = DeviceStore(tmp_path / "owner-rotation.sqlite3", clock=lambda: NOW)
    restored = DeviceService(reopened, bootstrap_code=None, clock=lambda: NOW)
    assert restored.authenticate(receipt.access_token).id == owner.device.id
    with pytest.raises(DeviceAuthenticationError, match="authentication failed"):
        restored.authenticate(owner.access_token)
    reopened.close()


def test_owner_rotation_limit_is_durable_and_boundary_uses_injected_clock(tmp_path) -> None:
    now = [NOW]
    store = DeviceStore(tmp_path / "owner-rotation-limit.sqlite3", clock=lambda: now[0])
    service = DeviceService(
        store,
        bootstrap_code=SecretStr(BOOTSTRAP_CODE),
        clock=lambda: now[0],
    )
    owner = pair_owner(service)
    current_token = owner.access_token

    for _ in range(5):
        current_token = service.rotate_owner_credential(current_token).access_token

    with pytest.raises(OwnerCredentialRateLimitError) as limited:
        service.rotate_owner_credential(current_token)
    assert limited.value.retry_after_seconds == 60 * 60
    assert service.authenticate(current_token).role is DeviceRole.OWNER
    assert len(store.list_credential_audit_events()) == 5

    # The rolling window excludes an event exactly one hour old.
    now[0] += timedelta(hours=1)
    next_rotation = service.rotate_owner_credential(current_token)
    assert service.authenticate(next_rotation.access_token).role is DeviceRole.OWNER
    assert len(store.list_credential_audit_events()) == 6
    store.close()


def test_credential_audit_persists_safe_metadata_and_rejects_mutation(tmp_path) -> None:
    database = tmp_path / "credential-audit.sqlite3"
    now = [NOW]
    store = DeviceStore(
        database,
        clock=lambda: now[0],
        owner_credential_lifetime_days=1,
    )
    service = DeviceService(
        store,
        bootstrap_code=SecretStr(BOOTSTRAP_CODE),
        clock=lambda: now[0],
    )
    owner = pair_owner(service)
    original_token = owner.access_token.get_secret_value()
    rotated = service.rotate_owner_credential(owner.access_token)
    rotated_token = rotated.access_token.get_secret_value()
    events = store.list_credential_audit_events()
    assert len(events) == 1
    assert events[0].event_type == "owner_credential_rotated"
    assert events[0].device_id == owner.device.id
    assert events[0].actor == "authenticated_owner"
    assert events[0].origin == "owner_rotation_api"
    assert events[0].outcome == "success"
    assert events[0].occurred_at == NOW
    store.close()

    now[0] = rotated.expires_at
    recovered = recover_owner_credential(database, lifetime_days=1)
    recovered_token = recovered.access_token.get_secret_value()
    reopened = DeviceStore(database, clock=lambda: now[0], owner_credential_lifetime_days=1)
    persisted_events = reopened.list_credential_audit_events()
    assert [event.event_type for event in persisted_events] == [
        "owner_credential_rotated",
        "owner_credential_recovered",
    ]
    assert persisted_events[1].actor == "host_local_operator"
    assert persisted_events[1].origin == "host_local_recovery_cli"
    assert persisted_events[1].outcome == "success"
    assert reopened.get_device(owner.device.id).credential_generation == 3
    audit_text = repr(persisted_events)
    for credential in (BOOTSTRAP_CODE, original_token, rotated_token, recovered_token):
        assert credential not in audit_text
        assert hashlib.sha256(credential.encode()).hexdigest() not in audit_text

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        reopened._connection.execute("DELETE FROM device_credential_audit_events")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        reopened._connection.execute(
            "UPDATE device_credential_audit_events SET outcome = 'failure'"
        )
    assert reopened.list_credential_audit_events() == persisted_events
    reopened.close()


def test_audit_insert_failure_rolls_back_rotation_and_recovery(tmp_path) -> None:
    database = tmp_path / "credential-audit-rollback.sqlite3"
    now = [NOW]
    store = DeviceStore(database, clock=lambda: now[0], owner_credential_lifetime_days=1)
    service = DeviceService(
        store,
        bootstrap_code=SecretStr(BOOTSTRAP_CODE),
        clock=lambda: now[0],
    )
    owner = pair_owner(service)
    store._connection.execute(
        "CREATE TRIGGER fail_credential_audit BEFORE INSERT ON device_credential_audit_events "
        "BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END"
    )
    with pytest.raises(DeviceStoreError, match="rotation could not be completed"):
        service.rotate_owner_credential(owner.access_token)
    assert service.authenticate(owner.access_token).role is DeviceRole.OWNER
    assert store.get_device(owner.device.id).credential_generation == 1
    assert store.list_credential_audit_events() == ()

    store._connection.execute("DROP TRIGGER fail_credential_audit")
    rotated = service.rotate_owner_credential(owner.access_token)
    store.close()

    now[0] = rotated.expires_at
    reopened = DeviceStore(database, clock=lambda: now[0], owner_credential_lifetime_days=1)
    reopened._connection.execute(
        "CREATE TRIGGER fail_credential_audit BEFORE INSERT ON device_credential_audit_events "
        "BEGIN SELECT RAISE(ABORT, 'injected audit failure'); END"
    )
    with pytest.raises(DeviceStoreError, match="recovery could not be completed"):
        recover_owner_credential(database, lifetime_days=1)
    assert reopened.get_device(owner.device.id).credential_generation == 2
    assert [event.event_type for event in reopened.list_credential_audit_events()] == [
        "owner_credential_rotated"
    ]
    with pytest.raises(DeviceAuthenticationError, match="authentication failed"):
        DeviceService(reopened, bootstrap_code=None, clock=lambda: now[0]).authenticate(
            rotated.access_token
        )
    reopened.close()


def test_device_v3_migration_initializes_generation_and_rolls_back_on_failure(tmp_path) -> None:
    database = tmp_path / "device-generation-migration.sqlite3"
    original, service = make_service(database)
    owner = pair_owner(service)
    companion = service.pair(
        service.issue_pairing_code(owner.access_token),
        name="generation companion",
        platform=DevicePlatform.ANDROID,
    )
    original.close()

    connection = sqlite3.connect(database)
    connection.execute("ALTER TABLE devices DROP COLUMN credential_generation")
    connection.execute("DELETE FROM device_schema_migrations WHERE version = 4")
    connection.commit()
    connection.close()

    with pytest.raises(DeviceStoreError, match="migration failed"):
        DeviceStore(database, clock=lambda: datetime(2026, 10, 20, 8, 30))

    connection = sqlite3.connect(database)
    assert connection.execute(
        "SELECT MAX(version) FROM device_schema_migrations"
    ).fetchone()[0] == 3
    assert "credential_generation" not in {
        row[1] for row in connection.execute("PRAGMA table_info(devices)")
    }
    connection.close()

    migrated = DeviceStore(database, clock=lambda: NOW)
    assert migrated.schema_version == 4
    assert migrated.get_device(owner.device.id).credential_generation == 1
    assert migrated.get_device(companion.device.id).credential_generation == 0
    assert migrated.authenticate(hashlib.sha256(
        owner.access_token.get_secret_value().encode()
    ).hexdigest()).credential_generation == 1
    migrated.close()


def test_concurrent_owner_rotation_allows_exactly_one_replacement(tmp_path) -> None:
    database = tmp_path / "concurrent-owner-rotation.sqlite3"
    initial, service = make_service(database)
    owner = pair_owner(service)
    initial.close()
    barrier = Barrier(2)

    def rotate_concurrently() -> str | None:
        store = DeviceStore(database, clock=lambda: NOW)
        worker = DeviceService(store, bootstrap_code=None, clock=lambda: NOW)
        original_rotate = store.rotate_owner_credential

        def synchronized_rotate(owner_id, current_digest, replacement_digest):
            barrier.wait(timeout=5)
            return original_rotate(owner_id, current_digest, replacement_digest)

        store.rotate_owner_credential = synchronized_rotate
        try:
            return worker.rotate_owner_credential(owner.access_token).access_token.get_secret_value()
        except DeviceAuthenticationError:
            return None
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        replacements = list(executor.map(lambda _index: rotate_concurrently(), range(2)))
    winners = [token for token in replacements if token is not None]
    assert len(winners) == 1

    check_store = DeviceStore(database, clock=lambda: NOW)
    check_service = DeviceService(check_store, bootstrap_code=None, clock=lambda: NOW)
    assert check_service.authenticate(SecretStr(winners[0])).role is DeviceRole.OWNER
    with pytest.raises(DeviceAuthenticationError, match="authentication failed"):
        check_service.authenticate(owner.access_token)
    check_store.close()


def test_host_local_recovery_requires_confirmation_and_replaces_expired_owner(tmp_path) -> None:
    database = tmp_path / "owner-recovery.sqlite3"
    now = [NOW]
    store = DeviceStore(database, clock=lambda: now[0], owner_credential_lifetime_days=1)
    service = DeviceService(
        store,
        bootstrap_code=SecretStr(BOOTSTRAP_CODE),
        clock=lambda: now[0],
    )
    owner = pair_owner(service)
    now[0] += timedelta(days=1)
    with pytest.raises(DeviceAuthenticationError, match="authentication failed"):
        service.authenticate(owner.access_token)
    store.close()

    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    settings = Settings(device_database_path=database, owner_credential_lifetime_days=3)
    output = Terminal()
    result = run_recovery(
        settings=settings,
        stdin=Terminal("RECOVER OWNER\n"),
        stdout=output,
        stderr=Terminal(),
    )
    assert result == 0
    assert "terminal is private and not being recorded" in output.getvalue()
    assert "shell transcripts, logs, screenshots, or shared recordings" in output.getvalue()
    replacement = output.getvalue().splitlines()[-1]
    assert replacement
    assert output.getvalue().count(replacement) == 1

    recovered = DeviceStore(database, clock=lambda: now[0], owner_credential_lifetime_days=3)
    recovered_service = DeviceService(recovered, bootstrap_code=None, clock=lambda: now[0])
    assert recovered_service.authenticate(SecretStr(replacement)).role is DeviceRole.OWNER
    with pytest.raises(DeviceAuthenticationError, match="authentication failed"):
        recovered_service.authenticate(owner.access_token)
    stored = recovered._connection.execute("SELECT token_digest FROM devices").fetchone()[0]
    assert stored == hashlib.sha256(replacement.encode()).hexdigest()
    assert replacement not in stored
    recovered.close()


def test_host_local_recovery_rejects_noninteractive_invocation(tmp_path) -> None:
    class NonTerminal(io.StringIO):
        def isatty(self) -> bool:
            return False

    store, service = make_service(tmp_path / "recovery-noninteractive.sqlite3")
    owner = pair_owner(service)
    settings = Settings(device_database_path=store.database)
    error = NonTerminal()
    result = run_recovery(
        settings=settings,
        stdin=NonTerminal("RECOVER OWNER\n"),
        stdout=NonTerminal(),
        stderr=error,
    )
    assert result == 2
    assert "interactive local terminal" in error.getvalue()
    assert service.authenticate(owner.access_token).role is DeviceRole.OWNER
    store.close()


def test_host_local_recovery_requires_exact_confirmation(tmp_path) -> None:
    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    store, service = make_service(tmp_path / "recovery-cancel.sqlite3")
    owner = pair_owner(service)
    output = Terminal()
    result = run_recovery(
        settings=Settings(device_database_path=store.database),
        stdin=Terminal("recover owner\n"),
        stdout=output,
        stderr=Terminal(),
    )
    assert result == 1
    assert "cancelled" in output.getvalue()
    assert service.authenticate(owner.access_token).role is DeviceRole.OWNER
    store.close()
