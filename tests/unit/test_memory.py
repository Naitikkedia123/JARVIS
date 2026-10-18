"""Deterministic memory validation, privacy, migration, and persistence tests."""

import sqlite3
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from jarvis.memory import (
    ManagementContext,
    MemoryAuthorizationError,
    MemoryConflictError,
    MemoryFilters,
    MemoryRecord,
    MemoryRecordNotFoundError,
    MemoryRecordType,
    MemoryStore,
    MemoryStoreError,
    PermissionState,
    Provenance,
    RetrievalContext,
    Sensitivity,
    SQLiteMemoryRetriever,
)


def make_record(record_id: str = "record-1", **overrides: object) -> MemoryRecord:
    values: dict[str, object] = {
        "id": record_id,
        "record_type": MemoryRecordType.PROJECT_FACT,
        "logical_key": "project.language",
        "title": "Project language",
        "content": "The project uses Python.",
        "source": "owner note",
        "source_timestamp": datetime(2026, 10, 18, 10, tzinfo=UTC),
        "confidence": 0.9,
        "provenance": Provenance.USER_CONFIRMED,
        "scope": "project",
        "sensitivity": Sensitivity.INTERNAL,
        "permission": PermissionState.ALLOWED,
    }
    values.update(overrides)
    return MemoryRecord.model_validate(values)


def manager(*, scopes: frozenset[str] = frozenset({"project"})) -> ManagementContext:
    return ManagementContext(scopes=scopes, max_sensitivity=Sensitivity.RESTRICTED)


def reader(
    *, scopes: frozenset[str] = frozenset({"project"}),
    sensitivity: Sensitivity = Sensitivity.RESTRICTED,
    include_inferred: bool = False,
) -> RetrievalContext:
    return RetrievalContext(
        scopes=scopes, max_sensitivity=sensitivity, include_inferred=include_inferred
    )


def test_record_source_provenance_and_aware_timestamps_are_returned_in_utc(tmp_path) -> None:
    fixed = datetime(2026, 10, 18, 12, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    store = MemoryStore(tmp_path / "time.sqlite3", clock=lambda: fixed)
    record = make_record(source_timestamp=fixed)
    created = store.create(record, manager())
    retrieved = store.get_by_id(created.id, reader())

    assert retrieved.source == "owner note"
    assert retrieved.source_timestamp == datetime(2026, 10, 18, 6, 30, tzinfo=UTC)
    assert retrieved.created_at == datetime(2026, 10, 18, 6, 30, tzinfo=UTC)
    assert retrieved.provenance is Provenance.USER_CONFIRMED
    store.close()


def test_injected_clock_must_be_timezone_aware(tmp_path) -> None:
    clock_values = iter([
        datetime(2026, 10, 18, tzinfo=UTC),
        datetime(2026, 10, 18, tzinfo=UTC),
        datetime(2026, 10, 18),
    ])
    store = MemoryStore(tmp_path / "naive-clock.sqlite3", clock=lambda: next(clock_values))
    with pytest.raises(ValueError, match="Timestamp must include a timezone"):
        store.create(make_record(), manager())
    store.close()


def test_model_inference_is_distinct_and_excluded_unless_enabled(tmp_path) -> None:
    store = MemoryStore(tmp_path / "provenance.sqlite3")
    store.create(make_record("user", content="A confirmed fact."), manager())
    store.create(
        make_record("inferred", content="A guessed fact.", provenance=Provenance.MODEL_INFERRED),
        manager(),
    )

    assert [row.id for row in store.list_current(reader())] == ["user"]
    assert {row.id for row in store.list_current(reader(include_inferred=True))} == {"user", "inferred"}
    assert store.get_by_id("inferred", reader(include_inferred=True)).provenance is Provenance.MODEL_INFERRED
    store.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"source_timestamp": datetime(2026, 1, 1)},
        {"valid_from": datetime(2026, 2, 1, tzinfo=UTC), "valid_until": datetime(2026, 1, 1, tzinfo=UTC)},
        {"confidence": 1.1},
        {"confidence": -0.1},
        {"confidence": True},
        {"content": 123},
        {"extra_field": "unexpected"},
    ],
)
def test_invalid_record_metadata_is_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_record(**changes)


def test_correction_supersedes_old_version_and_returns_current_replacement(tmp_path) -> None:
    store = MemoryStore(tmp_path / "correct.sqlite3")
    old = store.create(make_record(), manager())
    replacement = store.correct(
        old.id,
        make_record("record-2", content="The project uses Python 3.12."),
        expected_version=old.version,
        context=manager(),
    )

    assert replacement.version == 2
    assert replacement.supersedes_id == old.id
    assert store._connection.execute("SELECT state FROM memory_records WHERE id = ?", (old.id,)).fetchone()[0] == "superseded"
    assert store.get_by_id(replacement.id, reader()).content.endswith("Python 3.12.")
    with pytest.raises(MemoryRecordNotFoundError):
        store.get_by_id(old.id, reader())
    with pytest.raises(MemoryConflictError):
        store.correct(old.id, make_record("record-3"), expected_version=1, context=manager())
    store.close()


def test_deleting_replacement_does_not_resurrect_superseded_predecessor(tmp_path) -> None:
    store = MemoryStore(tmp_path / "no-resurrection.sqlite3")
    old = store.create(make_record(), manager())
    new = store.correct(old.id, make_record("record-2", content="Corrected."), expected_version=1, context=manager())
    store.delete(new.id, expected_version=new.version, context=manager())

    assert store._connection.execute("SELECT state FROM memory_records WHERE id = ?", (old.id,)).fetchone()[0] == "superseded"
    assert store._connection.execute("SELECT COUNT(*) FROM memory_records WHERE id = ?", (new.id,)).fetchone()[0] == 0
    assert store.list_current(reader()) == []
    store.close()


def test_delete_removes_content_and_full_text_index_entry(tmp_path) -> None:
    store = MemoryStore(tmp_path / "delete.sqlite3")
    retriever = SQLiteMemoryRetriever(store)
    record = store.create(make_record(content="Unique deletion phrase."), manager())
    assert retriever.search("deletion phrase", reader()) == [record]

    store.delete(record.id, expected_version=1, context=manager())

    assert retriever.search("deletion phrase", reader()) == []
    assert store._connection.execute("SELECT COUNT(*) FROM memory_records WHERE id = ?", (record.id,)).fetchone()[0] == 0
    store.close()


def test_expired_and_not_yet_valid_records_are_not_current(tmp_path) -> None:
    now = datetime(2026, 10, 18, tzinfo=UTC)
    store = MemoryStore(tmp_path / "validity.sqlite3", clock=lambda: now)
    store.create(make_record("expired", expires_at=now - timedelta(seconds=1)), manager())
    store.create(make_record("future", valid_from=now + timedelta(days=1)), manager())
    store.create(make_record("live", valid_from=now - timedelta(days=1), valid_until=now + timedelta(days=1)), manager())

    assert [item.id for item in store.list_current(reader())] == ["live"]
    with pytest.raises(MemoryRecordNotFoundError):
        store.get_by_id("expired", reader())
    store.close()


def test_scope_sensitivity_permission_and_management_are_enforced_in_store(tmp_path) -> None:
    store = MemoryStore(tmp_path / "policy.sqlite3")
    store.create(make_record("internal"), manager())
    store.create(make_record("restricted", sensitivity=Sensitivity.RESTRICTED), manager())
    store.create(make_record("denied", permission=PermissionState.DENIED), manager())
    store.create(make_record("unspecified", permission=PermissionState.UNSPECIFIED), manager())

    assert [item.id for item in store.list_current(reader(sensitivity=Sensitivity.INTERNAL))] == ["internal"]
    assert store.list_current(reader(scopes=frozenset({"other"}))) == []
    with pytest.raises(MemoryRecordNotFoundError):
        store.get_by_id("restricted", reader(sensitivity=Sensitivity.INTERNAL))
    with pytest.raises(MemoryRecordNotFoundError):
        store.get_by_id("denied", reader())
    with pytest.raises(MemoryRecordNotFoundError):
        store.get_by_id("unspecified", reader())
    with pytest.raises(MemoryAuthorizationError):
        store.create(make_record("outside"), manager(scopes=frozenset({"other"})))
    store.close()


def test_denied_create_writes_neither_record_nor_search_index(tmp_path) -> None:
    store = MemoryStore(tmp_path / "denied-create.sqlite3")
    index_table = "memory_fts" if store.fts5_available else "memory_fts_fallback"

    with pytest.raises(MemoryAuthorizationError):
        store.create(make_record("outside", scope="private"), manager(scopes=frozenset({"project"})))

    assert store._connection.execute(
        "SELECT COUNT(*) FROM memory_records WHERE id = ?", ("outside",)
    ).fetchone()[0] == 0
    assert store._connection.execute(
        f"SELECT COUNT(*) FROM {index_table} WHERE record_id = ?", ("outside",)
    ).fetchone()[0] == 0
    store.close()


def test_missing_and_inaccessible_id_have_same_not_found_result(tmp_path) -> None:
    store = MemoryStore(tmp_path / "id-access.sqlite3")
    store.create(make_record(), manager())
    with pytest.raises(MemoryRecordNotFoundError) as inaccessible:
        store.get_by_id("record-1", reader(scopes=frozenset({"other"})))
    with pytest.raises(MemoryRecordNotFoundError) as missing:
        store.get_by_id("missing", reader())
    assert str(inaccessible.value) == str(missing.value)
    store.close()


def test_empty_scope_fails_closed_for_search_list_and_id_lookup(tmp_path) -> None:
    store = MemoryStore(tmp_path / "empty-scope.sqlite3")
    store.create(make_record(), manager())
    empty_scope = reader(scopes=frozenset())
    retriever = SQLiteMemoryRetriever(store)

    assert store.list_current(empty_scope) == []
    assert retriever.search("Python", empty_scope) == []
    with pytest.raises(MemoryRecordNotFoundError) as inaccessible:
        store.get_by_id("record-1", empty_scope)
    with pytest.raises(MemoryRecordNotFoundError) as missing:
        store.get_by_id("missing", empty_scope)
    assert str(inaccessible.value) == str(missing.value)
    store.close()


def test_full_text_and_metadata_retrieval_both_apply_access_filters(tmp_path) -> None:
    store = MemoryStore(tmp_path / "retrieval.sqlite3")
    retriever = SQLiteMemoryRetriever(store)
    store.create(make_record(content="Needle phrase in accessible content."), manager())
    store.create(make_record("hidden", content="Needle phrase is private.", permission=PermissionState.DENIED), manager())

    assert [r.id for r in retriever.search("needle phrase", reader())] == ["record-1"]
    assert [r.id for r in retriever.filter_current(reader(), MemoryFilters(logical_key="project.language"))] == ["record-1"]
    with pytest.raises(ValueError):
        retriever.search("needle", reader(), limit=101)
    store.close()


@pytest.mark.parametrize(
    "query",
    [
        "alpha OR beta",
        "alpha NOT gamma",
        '"alpha" * (OR)',
        "alpha OR (beta NOT",
        'alpha *** OR ) "beta',
    ],
)
def test_fts_search_treats_operators_and_punctuation_as_literal_terms(tmp_path, query: str) -> None:
    now = datetime(2026, 10, 18, tzinfo=UTC)
    store = MemoryStore(tmp_path / "literal-fts.sqlite3", clock=lambda: now)
    retriever = SQLiteMemoryRetriever(store)
    store.create(make_record("plain", content="alpha beta"), manager())
    store.create(make_record("literal", content="alpha OR beta NOT gamma"), manager())
    store.create(
        make_record(
            "denied",
            content="alpha OR beta NOT gamma",
            permission=PermissionState.DENIED,
        ),
        manager(),
    )
    store.create(
        make_record("expired", content="alpha OR beta NOT gamma", expires_at=now - timedelta(seconds=1)),
        manager(),
    )
    store.create(
        make_record("old", content="alpha OR beta NOT gamma superseded"),
        manager(),
    )
    store.correct(
        "old",
        make_record("replacement", content="new current content"),
        expected_version=1,
        context=manager(),
    )

    assert [record.id for record in retriever.search(query, reader())] == ["literal"]
    assert retriever.search('alpha OR "secret', reader()) == []
    store.close()


def test_database_reopen_preserves_records_and_migration_is_idempotent(tmp_path) -> None:
    database = tmp_path / "reopen.sqlite3"
    store = MemoryStore(database)
    record = store.create(make_record(), manager())
    assert store.schema_version == 2
    store.close()

    reopened = MemoryStore(database)
    assert reopened.get_by_id(record.id, reader()) == record
    assert [tuple(row) for row in reopened._connection.execute(
        "SELECT version FROM memory_schema_migrations ORDER BY version"
    )] == [(1,), (2,)]
    assert reopened._connection.execute("SELECT COUNT(*) FROM memory_schema_migrations").fetchone()[0] == 2
    reopened.close()


def test_v1_migration_preserves_data_with_restrictive_governance_defaults(tmp_path) -> None:
    database = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE memory_schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    connection.execute("CREATE TABLE memory_records (id TEXT PRIMARY KEY, record_type TEXT NOT NULL, logical_key TEXT, title TEXT NOT NULL, content TEXT NOT NULL, artifact_ref TEXT, source TEXT NOT NULL, source_timestamp TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, confidence REAL NOT NULL, version INTEGER NOT NULL, supersedes_id TEXT, state TEXT NOT NULL, UNIQUE(supersedes_id))")
    connection.execute("INSERT INTO memory_schema_migrations VALUES (1, '2026-01-01T00:00:00+00:00')")
    connection.execute(
        "INSERT INTO memory_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("legacy-1", "project_fact", "legacy", "Legacy title", "Legacy data preserved", None,
         "old import", "2025-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00",
         "2025-01-01T00:00:00+00:00", 0.7, 1, None, "current"),
    )
    connection.commit()
    connection.close()

    store = MemoryStore(database)
    row = store._connection.execute(
        "SELECT content, provenance, scope, sensitivity, permission FROM memory_records WHERE id = ?",
        ("legacy-1",),
    ).fetchone()
    assert tuple(row) == ("Legacy data preserved", "legacy_unknown", "legacy_inaccessible", "restricted", "unspecified")
    with pytest.raises(MemoryRecordNotFoundError):
        store.get_by_id("legacy-1", reader())
    assert store._connection.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0] == 1
    assert store.schema_version == 2
    store.close()


def test_failed_correction_rolls_back_record_state_and_search_index(tmp_path) -> None:
    store = MemoryStore(tmp_path / "rollback.sqlite3")
    old = store.create(make_record(), manager())
    store._connection.execute(
        "CREATE TRIGGER force_insert_failure BEFORE INSERT ON memory_records "
        "BEGIN SELECT RAISE(ABORT, 'injected index failure'); END"
    )

    with pytest.raises(MemoryConflictError):
        store.correct(old.id, make_record("record-2", content="Replacement content."), expected_version=1, context=manager())

    assert store._connection.execute("SELECT state FROM memory_records WHERE id = ?", (old.id,)).fetchone()[0] == "current"
    assert store._connection.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0] == 1
    assert SQLiteMemoryRetriever(store).search("Python", reader())[0].id == old.id
    store.close()


def test_failed_schema_migration_rolls_back_added_columns(tmp_path) -> None:
    database = tmp_path / "migration-rollback.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE memory_schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    connection.execute("CREATE TABLE memory_records (id TEXT PRIMARY KEY, record_type TEXT NOT NULL, logical_key TEXT, title TEXT NOT NULL, content TEXT NOT NULL, artifact_ref TEXT, source TEXT NOT NULL, source_timestamp TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, confidence REAL NOT NULL, version INTEGER NOT NULL, supersedes_id TEXT, state TEXT NOT NULL, UNIQUE(supersedes_id))")
    connection.execute("INSERT INTO memory_schema_migrations VALUES (1, '2026-01-01T00:00:00+00:00')")
    connection.execute("CREATE TABLE memory_fts (conflict TEXT)")
    connection.commit()
    connection.close()

    with pytest.raises(MemoryStoreError, match="Memory schema migration failed"):
        MemoryStore(database)
    connection = sqlite3.connect(database)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(memory_records)")}
    assert "permission" not in columns
    assert connection.execute("SELECT MAX(version) FROM memory_schema_migrations").fetchone()[0] == 1
    connection.close()
