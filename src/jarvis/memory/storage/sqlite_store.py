"""SQLite storage with transactional governance checks and versioned migrations."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from jarvis.memory.errors import (
    MemoryAuthorizationError,
    MemoryConflictError,
    MemoryRecordNotFoundError,
    MemoryStoreError,
)
from jarvis.memory.models import (
    SENSITIVITY_RANK,
    ManagementContext,
    MemoryFilters,
    MemoryRecord,
    PermissionState,
    Provenance,
    RecordState,
    RetrievalContext,
    Sensitivity,
)

SCHEMA_VERSION = 2
BUSY_TIMEOUT_MS = 5000
MAX_RESULTS = 100
_COLUMNS = (
    "id, record_type, logical_key, title, content, artifact_ref, source, source_timestamp, "
    "created_at, updated_at, confidence, provenance, scope, sensitivity, permission, "
    "valid_from, valid_until, expires_at, version, supersedes_id, state"
)
_QUALIFIED_COLUMNS = ", ".join(f"r.{column.strip()}" for column in _COLUMNS.split(","))
_SENSITIVITY_VALUES = tuple(s.value for s in Sensitivity)


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone.")
    return value.astimezone(UTC)


def _timestamp(value: datetime | None) -> str | None:
    return _utc(value).isoformat() if value is not None else None


def _model_from_row(row: sqlite3.Row) -> MemoryRecord:
    data: dict[str, Any] = dict(row)
    for name in ("source_timestamp", "created_at", "updated_at", "valid_from", "valid_until", "expires_at"):
        if data[name] is not None:
            data[name] = datetime.fromisoformat(data[name])
    return MemoryRecord.model_validate(data)


class MemoryStore:
    """Transactional SQLite store. Database location is always explicitly injected."""

    def __init__(self, database: str | Path, *, clock: Callable[[], datetime] = _now) -> None:
        self._path = str(database)
        if self._path == ":memory:":
            raise ValueError("Use an explicit file-backed database path.")
        self._clock = clock
        self._connection = sqlite3.connect(self._path, timeout=BUSY_TIMEOUT_MS / 1000)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        try:
            self._initialize()
        except MemoryStoreError:
            self._connection.close()
            raise
        except Exception as exc:
            self._connection.close()
            raise MemoryStoreError("Memory schema migration failed.") from exc

    @property
    def schema_version(self) -> int:
        return int(self._connection.execute("SELECT MAX(version) FROM memory_schema_migrations").fetchone()[0])

    @property
    def fts5_available(self) -> bool:
        return self._fts5_available

    def close(self) -> None:
        self._connection.close()

    def _clock_now(self) -> datetime:
        return _utc(self._clock())

    def __enter__(self) -> MemoryStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _initialize(self) -> None:
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS memory_schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        version = self._connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM memory_schema_migrations"
        ).fetchone()[0]
        if version > SCHEMA_VERSION:
            raise MemoryStoreError("Memory database schema is newer than this application supports.")
        migrations = {1: self._migrate_v1, 2: self._migrate_v2}
        for next_version in range(version + 1, SCHEMA_VERSION + 1):
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                migrations[next_version]()
                self._connection.execute(
                    "INSERT INTO memory_schema_migrations(version, applied_at) VALUES (?, ?)",
                    (next_version, _timestamp(self._clock())),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        fts = self._connection.execute(
            "SELECT type FROM sqlite_master WHERE name = 'memory_fts'"
        ).fetchone()
        self._fts5_available = bool(fts and fts[0] == "table")

    def _migrate_v1(self) -> None:
        self._connection.execute(
            """CREATE TABLE memory_records (
                id TEXT PRIMARY KEY,
                record_type TEXT NOT NULL CHECK (record_type IN
                    ('task_episode','user_preference','project_fact','artifact_reference')),
                logical_key TEXT,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                artifact_ref TEXT,
                source TEXT NOT NULL,
                source_timestamp TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
                version INTEGER NOT NULL CHECK (version >= 1),
                supersedes_id TEXT REFERENCES memory_records(id) ON DELETE SET NULL,
                state TEXT NOT NULL CHECK (state IN ('current','superseded')),
                UNIQUE(supersedes_id)
            )"""
        )

    def _migrate_v2(self) -> None:
        additions = (
            ("provenance", "TEXT NOT NULL DEFAULT 'legacy_unknown' CHECK (provenance IN ('user_confirmed','model_inferred','tool_observed','legacy_unknown'))"),
            ("scope", "TEXT NOT NULL DEFAULT 'legacy_inaccessible'"),
            ("sensitivity", "TEXT NOT NULL DEFAULT 'restricted' CHECK (sensitivity IN ('public','internal','sensitive','restricted'))"),
            ("permission", "TEXT NOT NULL DEFAULT 'unspecified' CHECK (permission IN ('allowed','denied','unspecified'))"),
            ("valid_from", "TEXT"),
            ("valid_until", "TEXT"),
            ("expires_at", "TEXT"),
        )
        for name, declaration in additions:
            self._connection.execute(f"ALTER TABLE memory_records ADD COLUMN {name} {declaration}")
        self._connection.execute(
            "CREATE INDEX memory_current_lookup ON memory_records(state, scope, sensitivity, permission)"
        )
        try:
            self._connection.execute(
                "CREATE VIRTUAL TABLE memory_fts USING fts5(record_id UNINDEXED, title, content)"
            )
            self._connection.execute(
                "INSERT INTO memory_fts(record_id, title, content) "
                "SELECT id, title, content FROM memory_records"
            )
        except sqlite3.OperationalError as exc:
            if "no such module: fts5" not in str(exc).lower():
                raise
            self._connection.execute(
                "CREATE TABLE memory_fts_fallback (record_id TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL)"
            )
            self._connection.execute(
                "INSERT INTO memory_fts_fallback(record_id, title, content) "
                "SELECT id, title, content FROM memory_records"
            )

    def _authorize_manage(self, scope: str, sensitivity: Sensitivity, context: ManagementContext) -> None:
        if scope not in context.scopes or SENSITIVITY_RANK[sensitivity] > SENSITIVITY_RANK[context.max_sensitivity]:
            raise MemoryAuthorizationError("Memory management is not authorized.")

    def _access_sql(self, context: RetrievalContext) -> tuple[str, list[object]]:
        if not context.scopes:
            return " AND 0", []
        scopes = sorted(context.scopes)
        sql = f" AND r.scope IN ({','.join('?' for _ in scopes)}) AND r.permission = ?"
        params: list[object] = [*scopes, PermissionState.ALLOWED.value]
        allowed_sensitivity = [
            level
            for level in _SENSITIVITY_VALUES
            if SENSITIVITY_RANK[Sensitivity(level)] <= SENSITIVITY_RANK[context.max_sensitivity]
        ]
        sql += f" AND r.sensitivity IN ({','.join('?' for _ in allowed_sensitivity)})"
        params.extend(allowed_sensitivity)
        if not context.include_inferred:
            sql += " AND r.provenance != ?"
            params.append(Provenance.MODEL_INFERRED.value)
        return sql, params

    def create(self, record: MemoryRecord, context: ManagementContext) -> MemoryRecord:
        self._authorize_manage(record.scope, record.sensitivity, context)
        now = self._clock_now()
        stored = record.model_copy(update={"created_at": now, "updated_at": now, "version": 1, "supersedes_id": None, "state": RecordState.CURRENT})
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._insert_record(stored)
            self._index_record(stored)
            self._connection.commit()
        except sqlite3.IntegrityError as exc:
            self._connection.rollback()
            raise MemoryConflictError("Memory record conflicts with existing data.") from exc
        except Exception as exc:
            self._connection.rollback()
            raise MemoryStoreError("Memory record could not be stored.") from exc
        return stored

    def get_by_id(self, record_id: str, context: RetrievalContext) -> MemoryRecord:
        access_sql, params = self._access_sql(context)
        now = _timestamp(self._clock())
        try:
            row = self._connection.execute(
                f"SELECT {_COLUMNS} FROM memory_records r WHERE r.id = ? AND r.state = 'current' "
                "AND (r.valid_from IS NULL OR r.valid_from <= ?) "
                "AND (r.valid_until IS NULL OR r.valid_until > ?) "
                "AND (r.expires_at IS NULL OR r.expires_at > ?)" + access_sql,
                [record_id, now, now, now, *params],
            ).fetchone()
        except sqlite3.Error as exc:
            raise MemoryStoreError("Memory retrieval failed.") from exc
        if row is None:
            raise MemoryRecordNotFoundError("Memory record was not found.")
        try:
            return _model_from_row(row)
        except ValidationError as exc:
            raise MemoryStoreError("Stored memory record is invalid.") from exc

    def correct(
        self,
        record_id: str,
        replacement: MemoryRecord,
        *,
        expected_version: int,
        context: ManagementContext,
    ) -> MemoryRecord:
        now = self._clock_now()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                f"SELECT {_COLUMNS} FROM memory_records WHERE id = ?",
                (record_id,),
            ).fetchone()
            if row is None:
                raise MemoryRecordNotFoundError("Memory record was not found.")
            old = _model_from_row(row)
            self._authorize_manage(old.scope, old.sensitivity, context)
            if old.state is not RecordState.CURRENT:
                raise MemoryConflictError("Only a current memory record can be corrected.")
            if expected_version != old.version:
                raise MemoryConflictError("Memory record changed; reload it before correcting.")
            new = replacement.model_copy(update={
                "id": replacement.id,
                "created_at": now,
                "updated_at": now,
                "scope": old.scope,
                "sensitivity": old.sensitivity,
                "permission": old.permission,
                "version": old.version + 1,
                "supersedes_id": old.id,
                "state": RecordState.CURRENT,
            })
            self._authorize_manage(new.scope, new.sensitivity, context)
            self._connection.execute(
                "UPDATE memory_records SET state = 'superseded', updated_at = ? "
                "WHERE id = ? AND state = 'current' AND version = ?",
                (_timestamp(now), old.id, expected_version),
            )
            self._insert_record(new)
            self._index_record(new)
            self._connection.commit()
            return new
        except (MemoryAuthorizationError, MemoryConflictError, MemoryRecordNotFoundError):
            self._connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            self._connection.rollback()
            raise MemoryConflictError("Memory correction conflicts with existing data.") from exc
        except Exception as exc:
            self._connection.rollback()
            raise MemoryStoreError("Memory record could not be corrected.") from exc

    def delete(self, record_id: str, *, expected_version: int, context: ManagementContext) -> None:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                f"SELECT {_COLUMNS} FROM memory_records WHERE id = ?",
                (record_id,),
            ).fetchone()
            if row is None:
                raise MemoryRecordNotFoundError("Memory record was not found.")
            record = _model_from_row(row)
            self._authorize_manage(record.scope, record.sensitivity, context)
            if record.version != expected_version:
                raise MemoryConflictError("Memory record changed; reload it before deleting.")
            self._delete_index(record_id)
            self._connection.execute("DELETE FROM memory_records WHERE id = ?", (record_id,))
            self._connection.commit()
        except (MemoryAuthorizationError, MemoryConflictError, MemoryRecordNotFoundError):
            self._connection.rollback()
            raise
        except Exception as exc:
            self._connection.rollback()
            raise MemoryStoreError("Memory record could not be deleted.") from exc

    def list_current(
        self,
        context: RetrievalContext,
        filters: MemoryFilters | None = None,
        *,
        limit: int = MAX_RESULTS,
    ) -> list[MemoryRecord]:
        if not 1 <= limit <= MAX_RESULTS:
            raise ValueError(f"limit must be between 1 and {MAX_RESULTS}.")
        return self._query_current(context, filters or MemoryFilters(), limit=limit)

    def search(
        self,
        query: str,
        context: RetrievalContext,
        filters: MemoryFilters | None = None,
        *,
        limit: int = MAX_RESULTS,
    ) -> list[MemoryRecord]:
        if not 1 <= limit <= MAX_RESULTS:
            raise ValueError(f"limit must be between 1 and {MAX_RESULTS}.")
        terms = re.findall(r"[\w]+", query, flags=re.UNICODE)
        if not terms:
            return []
        if len(query) > 500:
            raise ValueError("query is too long.")
        return self._query_current(context, filters or MemoryFilters(), limit=limit, terms=terms)

    def _query_current(
        self,
        context: RetrievalContext,
        filters: MemoryFilters,
        *,
        limit: int,
        terms: Sequence[str] | None = None,
    ) -> list[MemoryRecord]:
        access_sql, params = self._access_sql(context)
        now = _timestamp(self._clock())
        sql = (
            f"SELECT {_QUALIFIED_COLUMNS} FROM memory_records r WHERE r.state = 'current' "
            "AND (r.valid_from IS NULL OR r.valid_from <= ?) "
            "AND (r.valid_until IS NULL OR r.valid_until > ?) "
            "AND (r.expires_at IS NULL OR r.expires_at > ?)"
        )
        values: list[object] = [now, now, now]
        sql += access_sql
        values.extend(params)
        if filters.record_type is not None:
            sql += " AND r.record_type = ?"
            values.append(filters.record_type.value)
        if filters.logical_key is not None:
            sql += " AND r.logical_key = ?"
            values.append(filters.logical_key)
        if filters.provenance is not None:
            sql += " AND r.provenance = ?"
            values.append(filters.provenance.value)
        if terms:
            if self._fts5_available:
                match = " AND ".join(f'"{term}"*' for term in terms)
                sql = sql.replace(" WHERE r.state", " JOIN memory_fts f ON f.record_id = r.id WHERE r.state")
                sql += " AND memory_fts MATCH ?"
                values.append(match)
            else:
                for term in terms:
                    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    sql += " AND (r.title LIKE ? ESCAPE '\\' OR r.content LIKE ? ESCAPE '\\')"
                    values.extend((f"%{escaped}%", f"%{escaped}%"))
        sql += " ORDER BY r.updated_at DESC, r.id LIMIT ?"
        values.append(limit)
        try:
            return [_model_from_row(row) for row in self._connection.execute(sql, values).fetchall()]
        except (sqlite3.Error, ValidationError) as exc:
            raise MemoryStoreError("Memory retrieval failed.") from exc

    def _insert_record(self, record: MemoryRecord) -> None:
        values = record.model_dump(mode="json")
        self._connection.execute(
            f"INSERT INTO memory_records ({_COLUMNS}) VALUES ({','.join('?' for _ in range(21))})",
            tuple(values[name] for name in (
                "id", "record_type", "logical_key", "title", "content", "artifact_ref", "source",
                "source_timestamp", "created_at", "updated_at", "confidence", "provenance", "scope",
                "sensitivity", "permission", "valid_from", "valid_until", "expires_at", "version",
                "supersedes_id", "state",
            )),
        )

    def _index_record(self, record: MemoryRecord) -> None:
        table = "memory_fts" if self._fts5_available else "memory_fts_fallback"
        self._connection.execute(
            f"INSERT INTO {table}(record_id, title, content) VALUES (?, ?, ?)",
            (record.id, record.title, record.content),
        )

    def _delete_index(self, record_id: str) -> None:
        table = "memory_fts" if self._fts5_available else "memory_fts_fallback"
        self._connection.execute(f"DELETE FROM {table} WHERE record_id = ?", (record_id,))
