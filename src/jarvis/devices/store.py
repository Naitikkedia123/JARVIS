"""Versioned SQLite persistence for paired devices and one-time pairing codes."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from jarvis.devices.models import (
    Device,
    DeviceCredentialAuditEvent,
    DevicePlatform,
    DeviceRole,
)

SCHEMA_VERSION = 4
BUSY_TIMEOUT_MS = 5_000
DEFAULT_OWNER_CREDENTIAL_LIFETIME_DAYS = 30
MIN_OWNER_CREDENTIAL_LIFETIME_DAYS = 1
MAX_OWNER_CREDENTIAL_LIFETIME_DAYS = 365
OWNER_ROTATION_LIMIT = 5
OWNER_ROTATION_WINDOW = timedelta(hours=1)
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
Clock = Callable[[], datetime]


class DeviceStoreError(RuntimeError):
    """A safe persistence failure for the local device registry."""


class DevicePairingUnavailableError(RuntimeError):
    """The pairing code is invalid, expired, consumed, or no longer authorized."""


class DeviceNotFoundError(RuntimeError):
    """No active device matched the requested identifier or credential."""


class OwnerCredentialRateLimitError(RuntimeError):
    """Owner credential rotation exceeded its durable rolling limit."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("Owner credential rotation is temporarily rate limited.")
        self.retry_after_seconds = retry_after_seconds


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone.")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _owner_expiry(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        expiry = datetime.fromisoformat(value)
    except ValueError:
        return None
    if expiry.tzinfo is None or expiry.utcoffset() is None:
        return None
    return expiry.astimezone(UTC)


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST_PATTERN.fullmatch(value) is not None


def _device_from_row(row: sqlite3.Row) -> Device:
    data: dict[str, Any] = dict(row)
    data["reported_capabilities"] = json.loads(data.pop("capabilities_json"))
    for name in ("paired_at", "last_seen_at", "revoked_at"):
        if data[name] is not None:
            data[name] = datetime.fromisoformat(data[name])
    return Device.model_validate(data)


class DeviceStore:
    """Synchronous SQLite store with an explicit file-backed database path."""

    current_schema_version = SCHEMA_VERSION

    def __init__(
        self,
        database: str | Path,
        *,
        clock: Clock | None = None,
        owner_credential_lifetime_days: int = DEFAULT_OWNER_CREDENTIAL_LIFETIME_DAYS,
    ) -> None:
        self.database = str(database)
        if self.database == ":memory:":
            raise ValueError("Use an explicit file-backed device database path.")
        if (
            type(owner_credential_lifetime_days) is not int
            or not MIN_OWNER_CREDENTIAL_LIFETIME_DAYS
            <= owner_credential_lifetime_days
            <= MAX_OWNER_CREDENTIAL_LIFETIME_DAYS
        ):
            raise ValueError("Owner credential lifetime must be between 1 and 365 days.")
        self._clock = clock or (lambda: datetime.now(UTC))
        self._owner_credential_lifetime = timedelta(days=owner_credential_lifetime_days)
        try:
            self._connection = sqlite3.connect(
                self.database, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None
            )
        except sqlite3.Error as exc:
            raise DeviceStoreError("Device database could not be opened.") from exc
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        try:
            self._migrate()
        except Exception as exc:
            self._connection.close()
            if isinstance(exc, DeviceStoreError):
                raise
            raise DeviceStoreError("Device schema migration failed.") from exc

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> DeviceStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM device_schema_migrations"
        ).fetchone()
        return int(row[0])

    def _migrate(self) -> None:
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS device_schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        version = self.schema_version
        if version > SCHEMA_VERSION:
            raise DeviceStoreError("Device database schema is newer than this application supports.")
        for next_version in range(version + 1, SCHEMA_VERSION + 1):
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                if next_version >= 2:
                    self._assert_single_owner()
                if next_version == 1:
                    self._apply_v1()
                elif next_version == 2:
                    self._apply_v2()
                elif next_version == 3:
                    self._apply_v3()
                elif next_version == 4:
                    self._apply_v4()
                self._connection.execute(
                    "INSERT INTO device_schema_migrations(version, applied_at) VALUES (?, ?)",
                    (next_version, _timestamp(self._clock())),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        self._assert_single_owner()

    def _assert_single_owner(self) -> None:
        table = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'devices'"
        ).fetchone()
        if table is None:
            return
        owner_count = self._connection.execute(
            "SELECT COUNT(*) FROM devices WHERE role = 'owner'"
        ).fetchone()[0]
        if owner_count > 1:
            raise DeviceStoreError("Device database violates the single-owner invariant.")

    def _apply_v2(self) -> None:
        """Give existing owner credentials a finite migration grace period."""
        self._connection.execute(
            "ALTER TABLE devices ADD COLUMN owner_credential_expires_at TEXT"
        )
        now = self._clock()
        expiry = _timestamp(now + self._owner_credential_lifetime)
        self._connection.execute(
            "UPDATE devices SET owner_credential_expires_at = ? WHERE role = 'owner'",
            (expiry,),
        )

    def _apply_v3(self) -> None:
        """Add a metadata-only credential audit ledger with append-only guards."""
        self._connection.execute(
            """CREATE TABLE device_credential_audit_events (
                event_id TEXT PRIMARY KEY CHECK(length(event_id) = 32),
                occurred_at TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK(event_type IN (
                    'owner_credential_rotated', 'owner_credential_recovered'
                )),
                device_id TEXT NOT NULL,
                actor TEXT NOT NULL CHECK(actor IN (
                    'authenticated_owner', 'host_local_operator'
                )),
                origin TEXT NOT NULL CHECK(origin IN (
                    'owner_rotation_api', 'host_local_recovery_cli'
                )),
                outcome TEXT NOT NULL CHECK(outcome = 'success'),
                FOREIGN KEY(device_id) REFERENCES devices(id)
            )"""
        )
        self._connection.execute(
            """CREATE TRIGGER credential_audit_no_update
                BEFORE UPDATE ON device_credential_audit_events
                BEGIN SELECT RAISE(ABORT, 'credential audit events are append-only'); END"""
        )
        self._connection.execute(
            """CREATE TRIGGER credential_audit_no_delete
                BEFORE DELETE ON device_credential_audit_events
                BEGIN SELECT RAISE(ABORT, 'credential audit events are append-only'); END"""
        )

    def _apply_v4(self) -> None:
        """Start a monotonic owner credential generation for lifecycle changes."""
        self._connection.execute(
            "ALTER TABLE devices ADD COLUMN credential_generation "
            "INTEGER NOT NULL DEFAULT 0 CHECK(credential_generation >= 0)"
        )
        # Existing credentials begin at generation one. Companions do not use
        # the owner generation and retain the neutral zero value.
        self._connection.execute(
            "UPDATE devices SET credential_generation = 1 WHERE role = 'owner'"
        )

    def _apply_v1(self) -> None:
        self._connection.execute(
            """CREATE TABLE devices (
                id TEXT PRIMARY KEY CHECK(length(id) = 32),
                name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
                platform TEXT NOT NULL CHECK(platform IN ('android','windows','web','other')),
                role TEXT NOT NULL CHECK(role IN ('owner','companion')),
                capabilities_json TEXT NOT NULL,
                token_digest TEXT NOT NULL UNIQUE CHECK(length(token_digest) = 64),
                paired_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                revoked_at TEXT,
                CHECK(revoked_at IS NULL OR revoked_at >= paired_at)
            )"""
        )
        self._connection.execute(
            """CREATE TABLE pairing_codes (
                code_digest TEXT PRIMARY KEY CHECK(length(code_digest) = 64),
                owner_device_id TEXT,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                used_at TEXT,
                CHECK(expires_at IS NULL OR expires_at > created_at),
                FOREIGN KEY(owner_device_id) REFERENCES devices(id) ON DELETE CASCADE
            )"""
        )
        self._connection.execute(
            "CREATE UNIQUE INDEX one_device_owner ON devices(role) WHERE role = 'owner'"
        )
        self._connection.execute(
            """CREATE TRIGGER device_cannot_be_unrevoked
                BEFORE UPDATE OF revoked_at ON devices
                WHEN OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NULL
                BEGIN SELECT RAISE(ABORT, 'revoked devices cannot be reactivated'); END"""
        )

    def add_bootstrap_code(self, code_digest: str) -> None:
        """Persist only a digest; never replace a consumed bootstrap code."""
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            has_device = self._connection.execute("SELECT 1 FROM devices LIMIT 1").fetchone()
            has_code = self._connection.execute("SELECT 1 FROM pairing_codes LIMIT 1").fetchone()
            if has_device is None and has_code is None:
                self._connection.execute(
                    "INSERT INTO pairing_codes(code_digest, owner_device_id, created_at, expires_at) "
                    "VALUES (?, NULL, ?, NULL)",
                    (code_digest, _timestamp(self._clock())),
                )
            self._connection.commit()
        except sqlite3.Error as exc:
            self._connection.rollback()
            raise DeviceStoreError("Pairing configuration could not be stored.") from exc

    def issue_pairing_code(self, owner_id: str, code_digest: str, expires_at: datetime) -> None:
        try:
            now = self._clock()
            self._connection.execute("BEGIN IMMEDIATE")
            owner = self._connection.execute(
                "SELECT role, revoked_at FROM devices WHERE id = ?", (owner_id,)
            ).fetchone()
            if owner is None or owner["role"] != DeviceRole.OWNER.value or owner["revoked_at"] is not None:
                raise DevicePairingUnavailableError("Device is not authorized to pair companions.")
            self._connection.execute(
                "DELETE FROM pairing_codes WHERE owner_device_id = ?", (owner_id,)
            )
            self._connection.execute(
                "INSERT INTO pairing_codes(code_digest, owner_device_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (code_digest, owner_id, _timestamp(now), _timestamp(expires_at)),
            )
            self._connection.commit()
        except DevicePairingUnavailableError:
            self._connection.rollback()
            raise
        except sqlite3.Error as exc:
            self._connection.rollback()
            raise DeviceStoreError("Pairing code could not be stored.") from exc

    def pair(
        self,
        *,
        code_digest: str,
        token_digest: str,
        name: str,
        platform: DevicePlatform,
        capabilities: frozenset[str],
    ) -> Device:
        now_datetime = self._clock()
        now = _timestamp(now_datetime)
        device_id = uuid4().hex
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            code = self._connection.execute(
                "SELECT owner_device_id, expires_at, used_at FROM pairing_codes "
                "WHERE code_digest = ?",
                (code_digest,),
            ).fetchone()
            if (
                code is None
                or code["used_at"] is not None
                or (code["expires_at"] is not None and code["expires_at"] <= now)
            ):
                raise DevicePairingUnavailableError("Pairing code is invalid or unavailable.")
            if code["owner_device_id"] is None:
                has_owner = self._connection.execute(
                    "SELECT 1 FROM devices WHERE role = 'owner' LIMIT 1"
                ).fetchone()
                if has_owner is not None:
                    raise DevicePairingUnavailableError("Pairing code is invalid or unavailable.")
                role = DeviceRole.OWNER
            else:
                owner = self._connection.execute(
                    "SELECT role, revoked_at FROM devices WHERE id = ?",
                    (code["owner_device_id"],),
                ).fetchone()
                if owner is None or owner["role"] != DeviceRole.OWNER.value or owner["revoked_at"]:
                    raise DevicePairingUnavailableError("Pairing code is invalid or unavailable.")
                role = DeviceRole.COMPANION
            owner_expiry = (
                _timestamp(now_datetime + self._owner_credential_lifetime)
                if role is DeviceRole.OWNER
                else None
            )
            self._connection.execute(
                "INSERT INTO devices(id, name, platform, role, capabilities_json, token_digest, "
                "paired_at, last_seen_at, owner_credential_expires_at, credential_generation) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    device_id,
                    name.strip(),
                    platform.value,
                    role.value,
                    json.dumps(sorted(capabilities), separators=(",", ":")),
                    token_digest,
                    now,
                    now,
                    owner_expiry,
                    1 if role is DeviceRole.OWNER else 0,
                ),
            )
            self._connection.execute(
                "UPDATE pairing_codes SET used_at = ? WHERE code_digest = ? AND used_at IS NULL",
                (now, code_digest),
            )
            self._connection.commit()
        except DevicePairingUnavailableError:
            self._connection.rollback()
            raise
        except sqlite3.Error as exc:
            self._connection.rollback()
            raise DeviceStoreError("Device pairing could not be completed.") from exc
        return Device(
            id=device_id,
            name=name.strip(),
            platform=platform,
            role=role,
            reported_capabilities=capabilities,
            paired_at=now,
            last_seen_at=now,
            credential_generation=1 if role is DeviceRole.OWNER else 0,
        )

    def authenticate(self, token_digest: str) -> Device:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT id, name, platform, role, capabilities_json, paired_at, last_seen_at, "
                "revoked_at, owner_credential_expires_at, credential_generation "
                "FROM devices WHERE token_digest = ? AND revoked_at IS NULL",
                (token_digest,),
            ).fetchone()
            if row is None:
                self._connection.rollback()
                raise DeviceNotFoundError("Device authentication failed.")
            now = _timestamp(self._clock())
            now_datetime = datetime.fromisoformat(now).astimezone(UTC)
            if row["role"] == DeviceRole.OWNER.value:
                expiry = _owner_expiry(row["owner_credential_expires_at"])
                if expiry is None or now_datetime >= expiry:
                    self._connection.rollback()
                    raise DeviceNotFoundError("Device authentication failed.")
            self._connection.execute(
                "UPDATE devices SET last_seen_at = ? WHERE id = ?", (now, row["id"])
            )
            self._connection.commit()
            updated = dict(row)
            updated["last_seen_at"] = now
            updated.pop("owner_credential_expires_at", None)
            return _device_from_row(updated)
        except DeviceNotFoundError:
            raise
        except (sqlite3.Error, ValidationError, TypeError, ValueError) as exc:
            self._connection.rollback()
            raise DeviceStoreError("Device authentication could not be completed.") from exc

    @contextmanager
    def reserve_owner_credential(
        self,
        *,
        owner_id: str,
        expected_generation: int,
        token_digest: str | None = None,
    ) -> Iterator[Device]:
        """Hold a cross-connection write reservation while another store commits.

        SQLite's ``BEGIN IMMEDIATE`` serializes this connection with rotations,
        recovery, and other writers to this file, including other processes.
        This is a lock protocol, not a cross-database transaction. No DeviceStore
        row is changed; the reservation is rolled back when released.
        """
        try:
            self._connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise DeviceStoreError("Owner credential reservation is unavailable.") from exc
        try:
            device = self.validate_reserved_owner_credential(
                owner_id=owner_id,
                expected_generation=expected_generation,
                token_digest=token_digest,
            )
        except DeviceNotFoundError:
            self._connection.rollback()
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            self._connection.rollback()
            raise DeviceStoreError("Owner credential reservation is unavailable.") from exc

        try:
            yield device
        except BaseException:
            self._connection.rollback()
            raise
        else:
            try:
                # The transaction only holds a lock; authorization state is not
                # mutated just to keep the reservation alive.
                self._connection.rollback()
            except sqlite3.Error as exc:
                raise DeviceStoreError("Owner credential reservation could not be released.") from exc

    @contextmanager
    def reserve_approval_resume(
        self,
        *,
        approval_owner_id: str,
        approval_owner_generation: int,
        task_owner_id: str,
        task_owner_token_digest: str,
    ) -> Iterator[tuple[Device, Device]]:
        """Reserve both approval and task-owner credentials through a task commit."""
        try:
            self._connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise DeviceStoreError("Owner credential reservation is unavailable.") from exc
        try:
            approval_owner = self.validate_reserved_owner_credential(
                owner_id=approval_owner_id,
                expected_generation=approval_owner_generation,
            )
            task_owner = self.validate_reserved_task_device(
                device_id=task_owner_id,
                token_digest=task_owner_token_digest,
            )
        except DeviceNotFoundError:
            self._connection.rollback()
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            self._connection.rollback()
            raise DeviceStoreError("Owner credential reservation is unavailable.") from exc

        try:
            yield approval_owner, task_owner
        except BaseException:
            self._connection.rollback()
            raise
        else:
            try:
                self._connection.rollback()
            except sqlite3.Error as exc:
                raise DeviceStoreError("Owner credential reservation could not be released.") from exc

    def validate_reserved_task_device(self, *, device_id: str, token_digest: str) -> Device:
        """Validate a task device bearer and active role inside the held reservation."""
        if not self._connection.in_transaction:
            raise DeviceStoreError("Task device validation requires a reservation.")
        if not _is_digest(token_digest):
            raise DeviceNotFoundError("Device authentication failed.")
        row = self._connection.execute(
            "SELECT id, name, platform, role, capabilities_json, paired_at, last_seen_at, "
            "revoked_at, owner_credential_expires_at, credential_generation "
            "FROM devices WHERE id = ? AND token_digest = ? AND revoked_at IS NULL",
            (device_id, token_digest),
        ).fetchone()
        if row is None:
            raise DeviceNotFoundError("Device authentication failed.")
        if row["role"] == DeviceRole.OWNER.value:
            expiry = _owner_expiry(row["owner_credential_expires_at"])
            now = datetime.fromisoformat(_timestamp(self._clock())).astimezone(UTC)
            if expiry is None or now >= expiry:
                raise DeviceNotFoundError("Device authentication failed.")
        elif row["role"] != DeviceRole.COMPANION.value:
            raise DeviceNotFoundError("Device authentication failed.")
        device_row = dict(row)
        device_row.pop("owner_credential_expires_at", None)
        return _device_from_row(device_row)

    def validate_reserved_owner_credential(
        self,
        *,
        owner_id: str,
        expected_generation: int,
        token_digest: str | None = None,
    ) -> Device:
        """Validate owner state while the caller holds this connection's reservation."""
        if not self._connection.in_transaction:
            raise DeviceStoreError("Owner credential validation requires a reservation.")
        if token_digest is not None and not _is_digest(token_digest):
            raise DeviceNotFoundError("Device authentication failed.")
        row = self._connection.execute(
            "SELECT id, name, platform, role, capabilities_json, token_digest, paired_at, "
            "last_seen_at, revoked_at, owner_credential_expires_at, credential_generation "
            "FROM devices WHERE id = ? AND role = 'owner' AND revoked_at IS NULL",
            (owner_id,),
        ).fetchone()
        if row is None:
            raise DeviceNotFoundError("Device authentication failed.")
        expiry = _owner_expiry(row["owner_credential_expires_at"])
        now = datetime.fromisoformat(_timestamp(self._clock())).astimezone(UTC)
        if (
            expiry is None
            or now >= expiry
            or not _is_digest(row["token_digest"])
            or row["credential_generation"] != expected_generation
            or (token_digest is not None and row["token_digest"] != token_digest)
        ):
            raise DeviceNotFoundError("Device authentication failed.")
        device_row = dict(row)
        device_row.pop("token_digest", None)
        device_row.pop("owner_credential_expires_at", None)
        return _device_from_row(device_row)

    def rotate_owner_credential(
        self,
        owner_id: str,
        current_digest: str,
        replacement_digest: str,
    ) -> datetime:
        """Atomically replace an unexpired owner credential after revalidation."""
        if (
            not _is_digest(current_digest)
            or not _is_digest(replacement_digest)
            or current_digest == replacement_digest
        ):
            raise DeviceNotFoundError("Device authentication failed.")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT token_digest, owner_credential_expires_at, credential_generation "
                "FROM devices "
                "WHERE id = ? AND role = 'owner' AND revoked_at IS NULL",
                (owner_id,),
            ).fetchone()
            now = datetime.fromisoformat(_timestamp(self._clock())).astimezone(UTC)
            expiry = None if row is None else _owner_expiry(row["owner_credential_expires_at"])
            if row is None or expiry is None or now >= expiry or row["token_digest"] != current_digest:
                self._connection.rollback()
                raise DeviceNotFoundError("Device authentication failed.")
            cutoff = _timestamp(now - OWNER_ROTATION_WINDOW)
            recent_rotations = self._connection.execute(
                "SELECT COUNT(*) AS rotation_count, MIN(occurred_at) AS oldest_rotation "
                "FROM device_credential_audit_events "
                "WHERE device_id = ? AND event_type = 'owner_credential_rotated' "
                "AND occurred_at > ?",
                (owner_id, cutoff),
            ).fetchone()
            if recent_rotations["rotation_count"] >= OWNER_ROTATION_LIMIT:
                oldest_rotation = datetime.fromisoformat(
                    recent_rotations["oldest_rotation"]
                ).astimezone(UTC)
                retry_after = max(
                    1,
                    math.ceil(
                        (oldest_rotation + OWNER_ROTATION_WINDOW - now).total_seconds()
                    ),
                )
                self._connection.rollback()
                raise OwnerCredentialRateLimitError(retry_after)
            replacement_expiry = now + self._owner_credential_lifetime
            cursor = self._connection.execute(
                "UPDATE devices SET token_digest = ?, owner_credential_expires_at = ?, "
                "credential_generation = credential_generation + 1 "
                "WHERE id = ? AND role = 'owner' AND revoked_at IS NULL AND token_digest = ?",
                (
                    replacement_digest,
                    _timestamp(replacement_expiry),
                    owner_id,
                    current_digest,
                ),
            )
            if cursor.rowcount != 1:
                self._connection.rollback()
                raise DeviceNotFoundError("Device authentication failed.")
            self._append_credential_audit_event(
                event_type="owner_credential_rotated",
                device_id=owner_id,
                actor="authenticated_owner",
                origin="owner_rotation_api",
            )
            self._connection.commit()
            return replacement_expiry
        except DeviceNotFoundError:
            raise
        except OwnerCredentialRateLimitError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            self._connection.rollback()
            raise DeviceStoreError("Owner credential rotation could not be completed.") from exc

    def recover_owner_credential(self, replacement_digest: str) -> datetime:
        """Host-local recovery: replace an active owner's expired or lost credential."""
        if not _is_digest(replacement_digest):
            raise DeviceStoreError("Owner credential recovery could not be completed.")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT id, token_digest FROM devices WHERE role = 'owner' AND revoked_at IS NULL"
            ).fetchone()
            if row is None or row["token_digest"] == replacement_digest:
                self._connection.rollback()
                raise DeviceNotFoundError("Owner device was not found.")
            now = datetime.fromisoformat(_timestamp(self._clock())).astimezone(UTC)
            expiry = now + self._owner_credential_lifetime
            cursor = self._connection.execute(
                "UPDATE devices SET token_digest = ?, owner_credential_expires_at = ?, "
                "credential_generation = credential_generation + 1 "
                "WHERE id = ? AND role = 'owner' AND revoked_at IS NULL",
                (replacement_digest, _timestamp(expiry), row["id"]),
            )
            if cursor.rowcount != 1:
                self._connection.rollback()
                raise DeviceNotFoundError("Owner device was not found.")
            self._append_credential_audit_event(
                event_type="owner_credential_recovered",
                device_id=row["id"],
                actor="host_local_operator",
                origin="host_local_recovery_cli",
            )
            self._connection.commit()
            return expiry
        except DeviceNotFoundError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            self._connection.rollback()
            raise DeviceStoreError("Owner credential recovery could not be completed.") from exc

    def _append_credential_audit_event(
        self,
        *,
        event_type: str,
        device_id: str,
        actor: str,
        origin: str,
    ) -> None:
        self._connection.execute(
            "INSERT INTO device_credential_audit_events "
            "(event_id, occurred_at, event_type, device_id, actor, origin, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?, 'success')",
            (
                uuid4().hex,
                _timestamp(self._clock()),
                event_type,
                device_id,
                actor,
                origin,
            ),
        )

    def list_credential_audit_events(self) -> tuple[DeviceCredentialAuditEvent, ...]:
        """Return audit metadata without any credential material."""
        try:
            rows = self._connection.execute(
                "SELECT event_id, occurred_at, event_type, device_id, actor, origin, outcome "
                "FROM device_credential_audit_events ORDER BY occurred_at, event_id"
            ).fetchall()
            events = []
            for row in rows:
                data = dict(row)
                data["occurred_at"] = datetime.fromisoformat(data["occurred_at"])
                events.append(DeviceCredentialAuditEvent.model_validate(data))
            return tuple(events)
        except (sqlite3.Error, ValidationError, TypeError, ValueError) as exc:
            raise DeviceStoreError("Credential audit events could not be read.") from exc

    def get_device(self, device_id: str, *, active_only: bool = True) -> Device:
        query = (
            "SELECT id, name, platform, role, capabilities_json, paired_at, last_seen_at, "
            "revoked_at, credential_generation "
            "FROM devices WHERE id = ?"
        )
        if active_only:
            query += " AND revoked_at IS NULL"
        try:
            row = self._connection.execute(query, (device_id,)).fetchone()
        except sqlite3.Error as exc:
            raise DeviceStoreError("Device could not be retrieved.") from exc
        if row is None:
            raise DeviceNotFoundError("Device was not found.")
        try:
            return _device_from_row(row)
        except (ValidationError, ValueError, TypeError) as exc:
            raise DeviceStoreError("Stored device record is invalid.") from exc

    def list_devices(self) -> tuple[Device, ...]:
        try:
            rows = self._connection.execute(
                "SELECT id, name, platform, role, capabilities_json, paired_at, last_seen_at, "
                "revoked_at, credential_generation "
                "FROM devices ORDER BY CASE role WHEN 'owner' THEN 0 ELSE 1 END, paired_at, id"
            ).fetchall()
            return tuple(_device_from_row(row) for row in rows)
        except (sqlite3.Error, ValidationError, ValueError, TypeError) as exc:
            raise DeviceStoreError("Stored device record is invalid.") from exc

    def update_capabilities(self, device_id: str, capabilities: frozenset[str]) -> Device:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            cursor = self._connection.execute(
                "UPDATE devices SET capabilities_json = ? WHERE id = ? AND revoked_at IS NULL",
                (json.dumps(sorted(capabilities), separators=(",", ":")), device_id),
            )
            if cursor.rowcount != 1:
                self._connection.rollback()
                raise DeviceNotFoundError("Device was not found.")
            self._connection.commit()
        except DeviceNotFoundError:
            raise
        except sqlite3.Error as exc:
            self._connection.rollback()
            raise DeviceStoreError("Device capabilities could not be updated.") from exc
        return self.get_device(device_id)

    def revoke(self, device_id: str) -> Device:
        now = _timestamp(self._clock())
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT role FROM devices WHERE id = ? AND revoked_at IS NULL", (device_id,)
            ).fetchone()
            if row is None:
                self._connection.rollback()
                raise DeviceNotFoundError("Device was not found.")
            if row["role"] == DeviceRole.OWNER.value:
                self._connection.rollback()
                raise DevicePairingUnavailableError("The owner device cannot be revoked here.")
            self._connection.execute(
                "UPDATE devices SET revoked_at = ? WHERE id = ?", (now, device_id)
            )
            self._connection.commit()
        except (DeviceNotFoundError, DevicePairingUnavailableError):
            raise
        except sqlite3.Error as exc:
            self._connection.rollback()
            raise DeviceStoreError("Device revocation could not be completed.") from exc
        return self.get_device(device_id, active_only=False)
