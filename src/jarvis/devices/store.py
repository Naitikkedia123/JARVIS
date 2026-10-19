"""Versioned SQLite persistence for paired devices and one-time pairing codes."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from jarvis.devices.models import Device, DevicePlatform, DeviceRole

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5_000
Clock = Callable[[], datetime]


class DeviceStoreError(RuntimeError):
    """A safe persistence failure for the local device registry."""


class DevicePairingUnavailableError(RuntimeError):
    """The pairing code is invalid, expired, consumed, or no longer authorized."""


class DeviceNotFoundError(RuntimeError):
    """No active device matched the requested identifier or credential."""


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone.")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


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

    def __init__(self, database: str | Path, *, clock: Clock | None = None) -> None:
        self.database = str(database)
        if self.database == ":memory:":
            raise ValueError("Use an explicit file-backed device database path.")
        self._clock = clock or (lambda: datetime.now(UTC))
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
                self._apply_v1()
                self._connection.execute(
                    "INSERT INTO device_schema_migrations(version, applied_at) VALUES (?, ?)",
                    (next_version, _timestamp(self._clock())),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

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
        now = _timestamp(self._clock())
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
            self._connection.execute(
                "INSERT INTO devices(id, name, platform, role, capabilities_json, token_digest, "
                "paired_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    device_id,
                    name.strip(),
                    platform.value,
                    role.value,
                    json.dumps(sorted(capabilities), separators=(",", ":")),
                    token_digest,
                    now,
                    now,
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
        )

    def authenticate(self, token_digest: str) -> Device:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT id, name, platform, role, capabilities_json, paired_at, last_seen_at, revoked_at "
                "FROM devices WHERE token_digest = ? AND revoked_at IS NULL",
                (token_digest,),
            ).fetchone()
            if row is None:
                self._connection.rollback()
                raise DeviceNotFoundError("Device authentication failed.")
            now = _timestamp(self._clock())
            self._connection.execute(
                "UPDATE devices SET last_seen_at = ? WHERE id = ?", (now, row["id"])
            )
            self._connection.commit()
            updated = dict(row)
            updated["last_seen_at"] = now
            return _device_from_row(updated)
        except DeviceNotFoundError:
            raise
        except (sqlite3.Error, ValidationError, TypeError, ValueError) as exc:
            self._connection.rollback()
            raise DeviceStoreError("Device authentication could not be completed.") from exc

    def get_device(self, device_id: str, *, active_only: bool = True) -> Device:
        query = (
            "SELECT id, name, platform, role, capabilities_json, paired_at, last_seen_at, revoked_at "
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
                "SELECT id, name, platform, role, capabilities_json, paired_at, last_seen_at, revoked_at "
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
