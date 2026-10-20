"""SQLite persistence for task-domain records and their audit trail.

This store persists execution intent and outcomes. It does not dispatch work or
provide exactly-once guarantees for effects outside SQLite.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import ValidationError

from jarvis.tasks.action import AuthorizedTaskAction
from jarvis.tasks.authorization import TaskOwnership, TaskSubmissionProposal
from jarvis.tasks.errors import (
    ActiveAttemptError,
    DependencyNotSatisfiedError,
    EntityNotFoundError,
    InvalidDependencyError,
    InvalidTransitionError,
    RetryLimitExceededError,
    TaskStoreError,
    TaskSubmissionConflictError,
)
from jarvis.tasks.models import (
    ApprovalAuditEvent,
    ApprovalAuditEventType,
    ApprovalRequestRecord,
    ApprovalStatus,
    AttemptAuthorization,
    AttemptAuthorizationBinding,
    AttemptDenial,
    AttemptOutcome,
    EventMetadata,
    Goal,
    GoalState,
    ReconciliationOutcome,
    ReplaySafety,
    SafeErrorCode,
    Step,
    StepAttempt,
    StepResult,
    StepState,
    Task,
    TaskCheckpoint,
    TaskEvent,
    TaskEventType,
    TaskPlan,
    TaskState,
)
from jarvis.tasks.state import (
    validate_goal_transition,
    validate_step_transition,
    validate_task_transition,
)

Clock = Callable[[], datetime]
CURRENT_SCHEMA_VERSION = 7

_SCHEMA_V1 = (
    """CREATE TABLE goals (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200),
        description TEXT NOT NULL CHECK(length(description) <= 4000),
        state TEXT NOT NULL CHECK(state IN ('active','paused','cancelling','completed','cancelled')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE tasks (
        id TEXT PRIMARY KEY,
        goal_id TEXT NOT NULL,
        title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200),
        state TEXT NOT NULL CHECK(state IN ('ready','running','paused','cancelling',
            'reconciliation_required','completed','failed','cancelled')),
        planner_version TEXT NOT NULL CHECK(length(planner_version) BETWEEN 1 AND 64),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(id, goal_id),
        FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE steps (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200),
        state TEXT NOT NULL CHECK(state IN ('pending','ready','running','retry_wait','paused',
            'reconciliation_required','succeeded','failed','cancelled')),
        replay_safety TEXT NOT NULL CHECK(replay_safety IN ('pure','idempotent','non_idempotent')),
        idempotency_key TEXT,
        attempt_limit INTEGER NOT NULL CHECK(attempt_limit BETWEEN 1 AND 10),
        attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND attempt_limit),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(task_id, id),
        CHECK(replay_safety != 'idempotent' OR idempotency_key IS NOT NULL),
        CHECK(state != 'running' OR attempt_count > 0),
        CHECK(state != 'reconciliation_required' OR attempt_count > 0),
        FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE step_dependencies (
        task_id TEXT NOT NULL,
        step_id TEXT NOT NULL,
        depends_on_step_id TEXT NOT NULL,
        PRIMARY KEY(task_id, step_id, depends_on_step_id),
        CHECK(step_id != depends_on_step_id),
        FOREIGN KEY(task_id, step_id) REFERENCES steps(task_id, id) ON DELETE CASCADE,
        FOREIGN KEY(task_id, depends_on_step_id) REFERENCES steps(task_id, id) ON DELETE CASCADE
    )""",
    """CREATE TABLE step_attempts (
        id TEXT PRIMARY KEY,
        step_id TEXT NOT NULL,
        attempt_number INTEGER NOT NULL CHECK(attempt_number BETWEEN 1 AND 10),
        outcome TEXT NOT NULL CHECK(outcome IN ('running','succeeded','retryable_failure',
            'permanent_failure','outcome_unknown')),
        replay_safety TEXT NOT NULL CHECK(replay_safety IN ('pure','idempotent','non_idempotent')),
        idempotency_key TEXT,
        error_code TEXT CHECK(error_code IS NULL OR error_code IN ('transient','permanent',
            'outcome_unknown')),
        started_at TEXT NOT NULL,
        finished_at TEXT,
        UNIQUE(step_id, attempt_number),
        CHECK((outcome = 'running' AND finished_at IS NULL) OR
            (outcome != 'running' AND finished_at IS NOT NULL)),
        CHECK(replay_safety != 'idempotent' OR idempotency_key IS NOT NULL),
        FOREIGN KEY(step_id) REFERENCES steps(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE task_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL CHECK(event_type IN ('goal_created','task_created','step_created',
            'state_changed','attempt_started','attempt_finished','reconciled')),
        goal_id TEXT NOT NULL,
        task_id TEXT,
        step_id TEXT,
        from_state TEXT,
        to_state TEXT,
        occurred_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL,
        CHECK(step_id IS NULL OR task_id IS NOT NULL),
        CHECK(task_id IS NULL OR goal_id IS NOT NULL),
        FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE CASCADE,
        FOREIGN KEY(task_id, goal_id) REFERENCES tasks(id, goal_id) ON DELETE CASCADE,
        FOREIGN KEY(task_id, step_id) REFERENCES steps(task_id, id) ON DELETE CASCADE
    )""",
    """CREATE TABLE task_checkpoints (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        step_id TEXT NOT NULL,
        sequence INTEGER NOT NULL CHECK(sequence >= 1),
        completed_step_ids_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(task_id, sequence),
        FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
        FOREIGN KEY(task_id, step_id) REFERENCES steps(task_id, id) ON DELETE CASCADE
    )""",
    """CREATE TRIGGER task_events_no_update
        BEFORE UPDATE ON task_events BEGIN
            SELECT RAISE(ABORT, 'task_events are append-only');
        END""",
    """CREATE TRIGGER task_events_no_delete
        BEFORE DELETE ON task_events BEGIN
            SELECT RAISE(ABORT, 'task_events are append-only');
        END""",
)

_SCHEMA_V2 = ("ALTER TABLE steps ADD COLUMN result_json TEXT",)

_SCHEMA_V3 = (
    """CREATE TABLE task_submissions (
        device_id TEXT NOT NULL CHECK(length(device_id) = 32),
        idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 16 AND 128),
        request_digest TEXT NOT NULL CHECK(length(request_digest) = 64),
        task_id TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        PRIMARY KEY(device_id, idempotency_key),
        FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
    )""",
    "CREATE INDEX task_submissions_task_id_idx ON task_submissions(task_id)",
)

_SCHEMA_V4 = ("ALTER TABLE steps ADD COLUMN action_json TEXT",)

_SCHEMA_V5 = (
    """CREATE TABLE attempt_authorizations (
        attempt_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        step_id TEXT NOT NULL,
        action_digest TEXT NOT NULL CHECK(length(action_digest) = 64),
        device_id TEXT NOT NULL CHECK(length(device_id) = 32),
        authorization_fingerprint TEXT NOT NULL
            CHECK(length(authorization_fingerprint) = 64),
        FOREIGN KEY(attempt_id) REFERENCES step_attempts(id) ON DELETE CASCADE,
        FOREIGN KEY(task_id, step_id) REFERENCES steps(task_id, id) ON DELETE CASCADE
    )""",
    "CREATE INDEX attempt_authorizations_task_id_idx ON attempt_authorizations(task_id)",
)

_SCHEMA_V6 = ("ALTER TABLE step_attempts ADD COLUMN dispatch_claimed_at TEXT",)

_SCHEMA_V7_APPROVALS = (
    """CREATE TABLE approval_requests (
        request_id TEXT PRIMARY KEY CHECK(length(request_id) = 36),
        task_id TEXT NOT NULL,
        step_id TEXT NOT NULL,
        requesting_device_id TEXT NOT NULL CHECK(length(requesting_device_id) = 32),
        owner_device_id TEXT NOT NULL CHECK(length(owner_device_id) = 32),
        owner_credential_generation INTEGER NOT NULL CHECK(owner_credential_generation >= 1),
        action_digest TEXT NOT NULL CHECK(
            length(action_digest) = 64 AND action_digest NOT GLOB '*[^0-9a-f]*'
        ),
        tool_definition_digest TEXT NOT NULL CHECK(
            length(tool_definition_digest) = 64
                AND tool_definition_digest NOT GLOB '*[^0-9a-f]*'
        ),
        status TEXT NOT NULL CHECK(status IN (
            'pending','approved','denied','expired','revoked','consumed'
        )),
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        decided_at TEXT,
        decided_by_device_id TEXT CHECK(
            decided_by_device_id IS NULL OR length(decided_by_device_id) = 32
        ),
        expired_at TEXT,
        revoked_at TEXT,
        revoked_by_device_id TEXT CHECK(
            revoked_by_device_id IS NULL OR length(revoked_by_device_id) = 32
        ),
        consumed_at TEXT,
        consumed_by_device_id TEXT CHECK(
            consumed_by_device_id IS NULL OR length(consumed_by_device_id) = 32
        ),
        UNIQUE(request_id, task_id, step_id),
        CHECK(expires_at > created_at),
        CHECK((decided_at IS NULL) = (decided_by_device_id IS NULL)),
        CHECK((revoked_at IS NULL) = (revoked_by_device_id IS NULL)),
        CHECK((consumed_at IS NULL) = (consumed_by_device_id IS NULL)),
        CHECK(
            (status = 'pending' AND decided_at IS NULL AND expired_at IS NULL
                AND revoked_at IS NULL AND consumed_at IS NULL)
            OR (status IN ('approved','denied') AND decided_at IS NOT NULL
                AND expired_at IS NULL AND revoked_at IS NULL AND consumed_at IS NULL)
            OR (status = 'expired' AND expired_at IS NOT NULL
                AND revoked_at IS NULL AND consumed_at IS NULL)
            OR (status = 'revoked' AND revoked_at IS NOT NULL
                AND expired_at IS NULL AND consumed_at IS NULL)
            OR (status = 'consumed' AND decided_at IS NOT NULL
                AND consumed_at IS NOT NULL AND expired_at IS NULL AND revoked_at IS NULL)
        ),
        FOREIGN KEY(task_id, step_id) REFERENCES steps(task_id, id) ON DELETE RESTRICT
    )""",
    """CREATE UNIQUE INDEX approval_requests_one_active_per_step
        ON approval_requests(task_id, step_id) WHERE status IN ('pending','approved')""",
    """CREATE TABLE approval_audit_events (
        event_id TEXT PRIMARY KEY CHECK(length(event_id) = 36),
        request_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        step_id TEXT NOT NULL,
        event_type TEXT NOT NULL CHECK(event_type IN (
            'requested','approved','denied','expired','revoked','consumed','dispatch_linked'
        )),
        occurred_at TEXT NOT NULL,
        actor_device_id TEXT CHECK(actor_device_id IS NULL OR length(actor_device_id) = 32),
        owner_credential_generation INTEGER NOT NULL CHECK(owner_credential_generation >= 1),
        action_digest TEXT NOT NULL CHECK(
            length(action_digest) = 64 AND action_digest NOT GLOB '*[^0-9a-f]*'
        ),
        tool_definition_digest TEXT NOT NULL CHECK(
            length(tool_definition_digest) = 64
                AND tool_definition_digest NOT GLOB '*[^0-9a-f]*'
        ),
        attempt_id TEXT,
        reason_code TEXT CHECK(reason_code IS NULL OR
            (length(reason_code) BETWEEN 1 AND 64 AND reason_code NOT GLOB '*[^a-z0-9_.:-]*')),
        UNIQUE(request_id, event_type),
        FOREIGN KEY(request_id, task_id, step_id)
            REFERENCES approval_requests(request_id, task_id, step_id) ON DELETE RESTRICT
    )""",
    "CREATE INDEX approval_audit_events_task_idx ON approval_audit_events(task_id, occurred_at)",
    """CREATE TRIGGER approval_requests_valid_status_transition
        BEFORE UPDATE OF status ON approval_requests
        WHEN NOT (
            (OLD.status = 'pending' AND NEW.status IN ('approved','denied','expired','revoked'))
            OR (OLD.status = 'approved' AND NEW.status IN ('expired','revoked','consumed'))
        )
        BEGIN SELECT RAISE(ABORT, 'invalid approval status transition'); END""",
    """CREATE TRIGGER approval_requests_binding_immutable
        BEFORE UPDATE OF request_id, task_id, step_id, requesting_device_id,
            owner_device_id, owner_credential_generation, action_digest,
            tool_definition_digest, created_at, expires_at ON approval_requests
        BEGIN SELECT RAISE(ABORT, 'approval binding is immutable'); END""",
    """CREATE TRIGGER approval_requests_no_delete
        BEFORE DELETE ON approval_requests
        BEGIN SELECT RAISE(ABORT, 'approval requests are retained'); END""",
    """CREATE TRIGGER approval_audit_no_update
        BEFORE UPDATE ON approval_audit_events
        BEGIN SELECT RAISE(ABORT, 'approval audit events are append-only'); END""",
    """CREATE TRIGGER approval_audit_no_delete
        BEFORE DELETE ON approval_audit_events
        BEGIN SELECT RAISE(ABORT, 'approval audit events are append-only'); END""",
)


def _json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone.")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


class TaskStore:
    """Synchronous SQLite store with a required, injectable database path."""

    current_schema_version = CURRENT_SCHEMA_VERSION

    def __init__(self, db_path: str | Path, *, clock: Clock | None = None) -> None:
        self.db_path = str(db_path)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._connection = sqlite3.connect(self.db_path, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        try:
            self._migrate()
        except Exception:
            self._connection.close()
            raise

    def close(self) -> None:
        self._connection.close()

    @property
    def schema_version(self) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()
        return int(row[0])

    def __enter__(self) -> TaskStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self._connection.rollback()
            raise
        else:
            try:
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def _migrate(self) -> None:
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {
            int(row[0])
            for row in self._connection.execute("SELECT version FROM schema_migrations")
        }
        if applied and max(applied) > CURRENT_SCHEMA_VERSION:
            raise TaskStoreError("Database schema is newer than this task store.")
        if 1 not in applied:
            with self._transaction():
                for statement in _SCHEMA_V1:
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (1, self._now()),
                )
            applied.add(1)
        if 2 not in applied:
            with self._transaction():
                for statement in _SCHEMA_V2:
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (2, self._now()),
                )
            applied.add(2)
        if 3 not in applied:
            with self._transaction():
                for statement in _SCHEMA_V3:
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (3, self._now()),
                )
            applied.add(3)
        if 4 not in applied:
            with self._transaction():
                for statement in _SCHEMA_V4:
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (4, self._now()),
                )
            applied.add(4)
        if 5 not in applied:
            with self._transaction():
                for statement in _SCHEMA_V5:
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (5, self._now()),
                )
            applied.add(5)
        if 6 not in applied:
            with self._transaction():
                for statement in _SCHEMA_V6:
                    self._connection.execute(statement)
                self._connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (6, self._now()),
                )
            applied.add(6)
        if 7 not in applied:
            self._apply_v7()

    def _apply_v7(self) -> None:
        """Add approval persistence and reserve explicit waiting task states.

        SQLite cannot widen the existing state CHECK constraints in place. The
        table rebuild runs as one DDL transaction with foreign-key enforcement
        temporarily disabled; a full foreign-key check runs before commit.
        """
        self._connection.execute("PRAGMA foreign_keys = OFF")
        try:
            with self._transaction():
                self._connection.execute(
                    """CREATE TABLE tasks_v7 (
                        id TEXT PRIMARY KEY,
                        goal_id TEXT NOT NULL,
                        title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200),
                        state TEXT NOT NULL CHECK(state IN ('ready','running','paused',
                            'waiting_approval','cancelling','reconciliation_required',
                            'completed','failed','cancelled')),
                        planner_version TEXT NOT NULL
                            CHECK(length(planner_version) BETWEEN 1 AND 64),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(id, goal_id),
                        FOREIGN KEY(goal_id) REFERENCES goals(id) ON DELETE CASCADE
                    )"""
                )
                self._connection.execute(
                    """INSERT INTO tasks_v7
                        (id, goal_id, title, state, planner_version, created_at, updated_at)
                        SELECT id, goal_id, title, state, planner_version, created_at, updated_at
                        FROM tasks"""
                )
                self._connection.execute("DROP TABLE tasks")
                self._connection.execute("ALTER TABLE tasks_v7 RENAME TO tasks")

                self._connection.execute(
                    """CREATE TABLE steps_v7 (
                        id TEXT PRIMARY KEY,
                        task_id TEXT NOT NULL,
                        title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 200),
                        state TEXT NOT NULL CHECK(state IN ('pending','ready','running',
                            'retry_wait','paused','waiting_approval','reconciliation_required',
                            'succeeded','failed','cancelled')),
                        replay_safety TEXT NOT NULL
                            CHECK(replay_safety IN ('pure','idempotent','non_idempotent')),
                        idempotency_key TEXT,
                        attempt_limit INTEGER NOT NULL CHECK(attempt_limit BETWEEN 1 AND 10),
                        attempt_count INTEGER NOT NULL
                            CHECK(attempt_count BETWEEN 0 AND attempt_limit),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        result_json TEXT,
                        action_json TEXT,
                        UNIQUE(task_id, id),
                        CHECK(replay_safety != 'idempotent' OR idempotency_key IS NOT NULL),
                        CHECK(state != 'running' OR attempt_count > 0),
                        CHECK(state != 'reconciliation_required' OR attempt_count > 0),
                        FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                    )"""
                )
                self._connection.execute(
                    """INSERT INTO steps_v7
                        (id, task_id, title, state, replay_safety, idempotency_key,
                         attempt_limit, attempt_count, created_at, updated_at,
                         result_json, action_json)
                        SELECT id, task_id, title, state, replay_safety, idempotency_key,
                            attempt_limit, attempt_count, created_at, updated_at,
                            result_json, action_json
                        FROM steps"""
                )
                self._connection.execute("DROP TABLE steps")
                self._connection.execute("ALTER TABLE steps_v7 RENAME TO steps")

                for statement in _SCHEMA_V7_APPROVALS:
                    self._connection.execute(statement)
                violations = self._connection.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise TaskStoreError("Approval schema migration violates task references.")
                self._connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (7, self._now()),
                )
        finally:
            self._connection.execute("PRAGMA foreign_keys = ON")

    def _now(self) -> str:
        try:
            return _timestamp(self._clock())
        except (TypeError, ValueError):
            raise TaskStoreError("The task store clock must return a timezone-aware timestamp.") from None

    def current_time(self) -> datetime:
        """Return the validated store clock for related approval lifecycle checks."""
        return datetime.fromisoformat(self._now())

    def _append_event(
        self,
        *,
        event_type: TaskEventType,
        goal_id: str,
        task_id: str | None = None,
        step_id: str | None = None,
        from_state: str | None = None,
        to_state: str | None = None,
        metadata: EventMetadata | None = None,
    ) -> int:
        cursor = self._connection.execute(
            """INSERT INTO task_events
                (event_type, goal_id, task_id, step_id, from_state, to_state, occurred_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_type.value,
                goal_id,
                task_id,
                step_id,
                from_state,
                to_state,
                self._now(),
                _json_dumps((metadata or EventMetadata()).model_dump(mode="json", exclude_none=True)),
            ),
        )
        return int(cursor.lastrowid)

    def add_goal(self, goal: Goal) -> Goal:
        if goal.state is not GoalState.ACTIVE:
            raise TaskStoreError("A new goal must begin in the active state.")
        with self._transaction():
            self._insert_goal(goal)
        return goal

    def _insert_goal(self, goal: Goal) -> None:
        if goal.state is not GoalState.ACTIVE:
            raise TaskStoreError("A new goal must begin in the active state.")
        try:
            self._connection.execute(
                """INSERT INTO goals(id, title, description, state, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    goal.id,
                    goal.title,
                    goal.description,
                    goal.state.value,
                    _timestamp(goal.created_at),
                    _timestamp(goal.updated_at),
                ),
            )
        except sqlite3.IntegrityError:
            raise TaskStoreError("Goal could not be stored due to a constraint violation.") from None
        self._append_event(event_type=TaskEventType.GOAL_CREATED, goal_id=goal.id)

    def add_task_plan(self, task: Task, steps: Iterable[Step]) -> TaskPlan:
        step_list = list(steps)
        candidate_ids = {step.id for step in step_list}
        dependency_ids = {
            dependency_id
            for step in step_list
            for dependency_id in step.dependencies
            if dependency_id not in candidate_ids
        }
        for dependency_id in dependency_ids:
            existing = self._connection.execute(
                "SELECT task_id FROM steps WHERE id = ?", (dependency_id,)
            ).fetchone()
            if existing is not None:
                raise InvalidDependencyError("Cross-task dependencies are not permitted.")
            raise InvalidDependencyError("A dependency is missing from this task plan.")
        plan = TaskPlan(task=task, steps=step_list)
        with self._transaction():
            self._insert_task_plan(plan)
        return plan

    def _insert_task_plan(self, plan: TaskPlan) -> None:
        task = plan.task
        step_list = plan.steps
        if task.state is not TaskState.READY:
            raise TaskStoreError("A new task must begin in the ready state.")
        if any(step.state is not StepState.PENDING for step in step_list):
            raise TaskStoreError("New plan steps must begin in the pending state.")

        goal_row = self._connection.execute(
            "SELECT id FROM goals WHERE id = ?", (task.goal_id,)
        ).fetchone()
        if goal_row is None:
            raise EntityNotFoundError("The task goal does not exist.")

        candidate_ids = {step.id for step in step_list}
        dependency_ids = {
            dependency_id
            for step in step_list
            for dependency_id in step.dependencies
            if dependency_id not in candidate_ids
        }
        for dependency_id in dependency_ids:
            existing = self._connection.execute(
                "SELECT task_id FROM steps WHERE id = ?", (dependency_id,)
            ).fetchone()
            if existing is not None:
                raise InvalidDependencyError("Cross-task dependencies are not permitted.")
            raise InvalidDependencyError("A dependency is missing from this task plan.")

        try:
            self._connection.execute(
                """INSERT INTO tasks
                    (id, goal_id, title, state, planner_version, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    task.id,
                    task.goal_id,
                    task.title,
                    task.state.value,
                    task.planner_version,
                    _timestamp(task.created_at),
                    _timestamp(task.updated_at),
                ),
            )
            self._append_event(
                event_type=TaskEventType.TASK_CREATED,
                goal_id=task.goal_id,
                task_id=task.id,
            )
            for step in step_list:
                self._connection.execute(
                    """INSERT INTO steps
                        (id, task_id, title, state, replay_safety, idempotency_key,
                         attempt_limit, attempt_count, created_at, updated_at, action_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        step.id,
                        task.id,
                        step.title,
                        step.state.value,
                        step.replay_safety.value,
                        step.idempotency_key,
                        step.attempt_limit,
                        step.attempt_count,
                        _timestamp(step.created_at),
                        _timestamp(step.updated_at),
                        None
                        if step.action is None
                        else _json_dumps(step.action.model_dump(mode="json")),
                    ),
                )
                self._append_event(
                    event_type=TaskEventType.STEP_CREATED,
                    goal_id=task.goal_id,
                    task_id=task.id,
                    step_id=step.id,
                    to_state=step.state.value,
                )
            for step in step_list:
                for dependency_id in step.dependencies:
                    self._connection.execute(
                        """INSERT INTO step_dependencies(task_id, step_id, depends_on_step_id)
                            VALUES (?, ?, ?)""",
                        (task.id, step.id, dependency_id),
                    )
        except sqlite3.IntegrityError:
            raise TaskStoreError("Task plan could not be stored due to a constraint violation.") from None

    def add_device_task_submission(
        self,
        *,
        goal: Goal,
        plan: TaskPlan,
        ownership: TaskOwnership,
    ) -> tuple[TaskPlan, bool]:
        """Atomically persist a new owned task or return an identical retry.

        Idempotency is scoped to `(device_id, idempotency_key)`. Reusing a key
        for a different request digest raises a conflict. This covers durable
        task creation only; it does not promise exactly-once task execution or
        external effects.
        """
        if plan.task.goal_id != goal.id:
            raise TaskStoreError("Task plan does not belong to its goal.")
        with self._transaction():
            existing = self._connection.execute(
                """SELECT request_digest, task_id FROM task_submissions
                    WHERE device_id = ? AND idempotency_key = ?""",
                (ownership.device_id, ownership.idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != ownership.request_digest:
                    raise TaskSubmissionConflictError(
                        "Submission idempotency key conflicts with an earlier request."
                    )
                return self._load_task_plan(str(existing["task_id"])), False

            self._insert_goal(goal)
            self._insert_task_plan(plan)
            try:
                self._connection.execute(
                    """INSERT INTO task_submissions
                        (device_id, idempotency_key, request_digest, task_id, created_at)
                        VALUES (?, ?, ?, ?, ?)""",
                    (
                        ownership.device_id,
                        ownership.idempotency_key,
                        ownership.request_digest,
                        plan.task.id,
                        self._now(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise TaskStoreError("Task submission could not be stored safely.") from None
        return plan, True

    def find_device_task_submission(self, ownership: TaskOwnership) -> TaskPlan | None:
        """Return an identical prior submission or raise on key reuse conflict."""
        row = self._connection.execute(
            """SELECT request_digest, task_id FROM task_submissions
                WHERE device_id = ? AND idempotency_key = ?""",
            (ownership.device_id, ownership.idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row["request_digest"] != ownership.request_digest:
            raise TaskSubmissionConflictError(
                "Submission idempotency key conflicts with an earlier request."
            )
        return self._load_task_plan(str(row["task_id"]))

    def _load_task_plan(self, task_id: str) -> TaskPlan:
        return TaskPlan(task=self.get_task(task_id), steps=self.list_steps(task_id))

    def get_task_ownership(self, task_id: str) -> TaskOwnership | None:
        row = self._connection.execute(
            """SELECT device_id, idempotency_key, request_digest
                FROM task_submissions WHERE task_id = ?""",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            return TaskOwnership(
                device_id=row["device_id"],
                idempotency_key=row["idempotency_key"],
                request_digest=row["request_digest"],
            )
        except (ValidationError, TypeError, ValueError):
            raise TaskStoreError("Stored task ownership is invalid.") from None

    def get_task_for_device(self, task_id: str, device_id: str) -> Task:
        return self.get_task_plan_for_device(task_id, device_id).task

    def get_task_plan_for_device(self, task_id: str, device_id: str) -> TaskPlan:
        """Return plan state only when its owner matches the trusted device ID."""
        row = self._connection.execute(
            """SELECT tasks.id FROM tasks
                JOIN task_submissions ON task_submissions.task_id = tasks.id
                WHERE tasks.id = ? AND task_submissions.device_id = ?""",
            (task_id, device_id),
        ).fetchone()
        if row is None:
            raise EntityNotFoundError("Task was not found.")
        return self._load_task_plan(task_id)

    @staticmethod
    def _approval_request_from_row(row: sqlite3.Row) -> ApprovalRequestRecord:
        data = {
            name: value
            for name, value in dict(row).items()
            if name in ApprovalRequestRecord.model_fields
        }
        data["request_id"] = UUID(data["request_id"])
        for name in (
            "created_at", "expires_at", "decided_at", "expired_at", "revoked_at", "consumed_at"
        ):
            if data[name] is not None:
                data[name] = datetime.fromisoformat(data[name])
        return ApprovalRequestRecord.model_validate(data)

    @staticmethod
    def _approval_event_from_row(row: sqlite3.Row) -> ApprovalAuditEvent:
        data = dict(row)
        data["event_id"] = UUID(data["event_id"])
        data["request_id"] = UUID(data["request_id"])
        data["occurred_at"] = datetime.fromisoformat(data["occurred_at"])
        return ApprovalAuditEvent.model_validate(data)

    def create_approval_request(self, request: ApprovalRequestRecord) -> ApprovalRequestRecord:
        """Persist a request and move its task and step to waiting atomically."""
        if request.status is not ApprovalStatus.PENDING:
            raise TaskStoreError("A new approval request must be pending.")
        now = datetime.fromisoformat(self._now())
        if request.created_at > now or request.expires_at <= now:
            raise TaskStoreError("Approval request timestamps are not current.")
        with self._transaction():
            step = self._connection.execute(
                """SELECT steps.task_id, steps.action_json, steps.state AS step_state,
                    tasks.state AS task_state, tasks.goal_id, task_submissions.device_id
                    FROM steps LEFT JOIN task_submissions
                        ON task_submissions.task_id = steps.task_id
                    JOIN tasks ON tasks.id = steps.task_id
                    WHERE steps.id = ?""",
                (request.step_id,),
            ).fetchone()
            if step is None or step["task_id"] != request.task_id:
                raise EntityNotFoundError("Approval task step was not found.")
            action = self._action_from_json(step["action_json"])
            if (
                action is None
                or action.digest() != request.action_digest
                or step["device_id"] != request.requesting_device_id
                or step["task_state"] != TaskState.READY.value
                or step["step_state"] not in (StepState.PENDING.value, StepState.READY.value)
            ):
                raise TaskStoreError("Approval request does not match the stored task action.")
            incomplete = self._connection.execute(
                """SELECT 1 FROM step_dependencies AS dependency
                    JOIN steps AS prerequisite ON prerequisite.id = dependency.depends_on_step_id
                    WHERE dependency.step_id = ? AND prerequisite.state != 'succeeded' LIMIT 1""",
                (request.step_id,),
            ).fetchone()
            if incomplete is not None:
                raise DependencyNotSatisfiedError("Approval cannot wait on an incomplete dependency.")
            now_text = self._now()
            self._connection.execute(
                """INSERT INTO approval_requests
                    (request_id, task_id, step_id, requesting_device_id, owner_device_id,
                     owner_credential_generation, action_digest, tool_definition_digest,
                     status, created_at, expires_at, decided_at, decided_by_device_id,
                     expired_at, revoked_at, revoked_by_device_id, consumed_at,
                     consumed_by_device_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL,
                        NULL, NULL, NULL)""",
                (
                    str(request.request_id), request.task_id, request.step_id,
                    request.requesting_device_id, request.owner_device_id,
                    request.owner_credential_generation, request.action_digest,
                    request.tool_definition_digest, request.status.value,
                    _timestamp(request.created_at), _timestamp(request.expires_at),
                ),
            )
            self._append_approval_audit_event(
                request,
                event_type=ApprovalAuditEventType.REQUESTED,
                actor_device_id=request.requesting_device_id,
            )
            self._connection.execute(
                "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ? AND state = ?",
                (TaskState.WAITING_APPROVAL.value, now_text, request.task_id, TaskState.READY.value),
            )
            self._connection.execute(
                "UPDATE steps SET state = ?, updated_at = ? WHERE id = ? AND state IN (?, ?)",
                (
                    StepState.WAITING_APPROVAL.value,
                    now_text,
                    request.step_id,
                    StepState.PENDING.value,
                    StepState.READY.value,
                ),
            )
            self._append_event(
                event_type=TaskEventType.STATE_CHANGED,
                goal_id=step["goal_id"],
                task_id=request.task_id,
                from_state=TaskState.READY.value,
                to_state=TaskState.WAITING_APPROVAL.value,
                metadata=EventMetadata(
                    action_digest=request.action_digest,
                    device_id=request.requesting_device_id,
                ),
            )
            self._append_event(
                event_type=TaskEventType.STATE_CHANGED,
                goal_id=step["goal_id"],
                task_id=request.task_id,
                step_id=request.step_id,
                from_state=step["step_state"],
                to_state=StepState.WAITING_APPROVAL.value,
                metadata=EventMetadata(
                    action_digest=request.action_digest,
                    device_id=request.requesting_device_id,
                ),
            )
        return self.get_approval_request(request.request_id)

    def decide_approval_request(
        self,
        request_id: UUID | str,
        *,
        owner_device_id: str,
        owner_credential_generation: int,
        action_digest: str,
        tool_definition_digest: str,
        approve: bool,
        precommit_validation: Callable[[], None],
    ) -> ApprovalRequestRecord:
        """Record one authenticated owner's decision or terminally invalidate it."""
        parsed_id = self._parse_approval_id(request_id)
        target = ApprovalStatus.APPROVED if approve else ApprovalStatus.DENIED
        with self._transaction():
            row = self._approval_context(parsed_id)
            if row is None or row["owner_device_id"] != owner_device_id:
                raise EntityNotFoundError("Approval request was not found.")
            request = self._approval_request_from_row(row)
            if request.status is not ApprovalStatus.PENDING:
                raise InvalidTransitionError("Approval request is unavailable.")
            step_action = self._action_from_json(row["action_json"])
            now = datetime.fromisoformat(self._now())
            invalid_reason: str | None = None
            terminal = target
            if request.owner_credential_generation != owner_credential_generation:
                terminal = ApprovalStatus.REVOKED
                invalid_reason = "credential_generation_changed"
            elif (
                step_action is None
                or step_action.digest() != request.action_digest
                or row["task_owner_device_id"] != request.requesting_device_id
                or row["task_state"] != TaskState.WAITING_APPROVAL.value
                or row["step_state"] != StepState.WAITING_APPROVAL.value
                or action_digest != request.action_digest
                or tool_definition_digest != request.tool_definition_digest
            ):
                terminal = ApprovalStatus.REVOKED
                invalid_reason = "binding_changed"
            elif now >= request.expires_at:
                terminal = ApprovalStatus.EXPIRED
                invalid_reason = "expired"

            values = request.model_dump(mode="python")
            values["status"] = terminal
            event_type = ApprovalAuditEventType(terminal.value)
            actor_device_id: str | None = owner_device_id
            if terminal in (ApprovalStatus.APPROVED, ApprovalStatus.DENIED):
                values["decided_at"] = now
                values["decided_by_device_id"] = owner_device_id
            elif terminal is ApprovalStatus.EXPIRED:
                values["expired_at"] = now
                actor_device_id = None
            elif terminal is ApprovalStatus.REVOKED:
                values["revoked_at"] = now
                values["revoked_by_device_id"] = owner_device_id
            updated = ApprovalRequestRecord.model_validate(values)
            cursor = self._connection.execute(
                """UPDATE approval_requests SET status = ?, decided_at = ?,
                    decided_by_device_id = ?, expired_at = ?, revoked_at = ?,
                    revoked_by_device_id = ? WHERE request_id = ? AND status = 'pending'""",
                (
                    terminal.value,
                    None if updated.decided_at is None else _timestamp(updated.decided_at),
                    updated.decided_by_device_id,
                    None if updated.expired_at is None else _timestamp(updated.expired_at),
                    None if updated.revoked_at is None else _timestamp(updated.revoked_at),
                    updated.revoked_by_device_id,
                    str(parsed_id),
                ),
            )
            if cursor.rowcount != 1:
                raise InvalidTransitionError("Approval request is unavailable.")
            self._append_approval_audit_event(
                updated,
                event_type=event_type,
                actor_device_id=actor_device_id,
                reason_code=invalid_reason,
            )
            if terminal in (ApprovalStatus.DENIED, ApprovalStatus.EXPIRED, ApprovalStatus.REVOKED):
                self._fail_waiting_approval_tx(row, request, terminal)
            precommit_validation()
        return self.get_approval_request(parsed_id)

    def consume_approval_and_start_attempt(
        self,
        request_id: UUID | str,
        *,
        requesting_device_id: str,
        owner_credential_generation: int,
        action_digest: str,
        tool_definition_digest: str,
        authorization: AttemptAuthorization,
        precommit_validation: Callable[[], None],
    ) -> StepAttempt:
        """Consume approval and persist running attempt plus dispatch claim atomically."""
        parsed_id = self._parse_approval_id(request_id)
        with self._transaction():
            row = self._approval_context(parsed_id)
            if row is None or row["requesting_device_id"] != requesting_device_id:
                raise EntityNotFoundError("Task was not found.")
            request = self._approval_request_from_row(row)
            step = self._step_from_context(self._step_context(request.step_id))
            action = step.action
            now = datetime.fromisoformat(self._now())
            if (
                request.status is not ApprovalStatus.APPROVED
                or row["task_state"] != TaskState.WAITING_APPROVAL.value
                or row["step_state"] != StepState.WAITING_APPROVAL.value
                or request.expires_at <= now
                or request.owner_credential_generation != owner_credential_generation
                or row["task_owner_device_id"] != requesting_device_id
                or action is None
                or action.digest() != request.action_digest
                or request.action_digest != action_digest
                or request.tool_definition_digest != tool_definition_digest
                or authorization.device_id != requesting_device_id
                or authorization.action_digest != request.action_digest
            ):
                raise InvalidTransitionError("Approved task is unavailable for resume.")
            if step.attempt_count >= step.attempt_limit:
                raise RetryLimitExceededError("Step attempt limit has been reached.")
            incomplete = self._connection.execute(
                """SELECT 1 FROM step_dependencies AS dependency
                    JOIN steps AS prerequisite ON prerequisite.id = dependency.depends_on_step_id
                    WHERE dependency.step_id = ? AND prerequisite.state != 'succeeded' LIMIT 1""",
                (request.step_id,),
            ).fetchone()
            if incomplete is not None:
                raise DependencyNotSatisfiedError("Task dependencies are not satisfied.")
            attempt_number = step.attempt_count + 1
            attempt_id = uuid4().hex
            now_text = _timestamp(now)
            binding = AttemptAuthorizationBinding(
                task_id=request.task_id,
                step_id=request.step_id,
                attempt_id=attempt_id,
                attempt_number=attempt_number,
                device_id=authorization.device_id,
                action_digest=authorization.action_digest,
                authorization_fingerprint=authorization.authorization_fingerprint,
            )
            attempt = StepAttempt(
                id=attempt_id,
                step_id=request.step_id,
                attempt_number=attempt_number,
                outcome=AttemptOutcome.RUNNING,
                replay_safety=step.replay_safety,
                idempotency_key=step.idempotency_key,
                started_at=now,
                dispatch_claimed_at=now,
            )
            self._connection.execute(
                """UPDATE approval_requests SET status = 'consumed', consumed_at = ?,
                    consumed_by_device_id = ? WHERE request_id = ? AND status = 'approved'""",
                (now_text, requesting_device_id, str(parsed_id)),
            )
            self._connection.execute(
                """INSERT INTO step_attempts
                    (id, step_id, attempt_number, outcome, replay_safety, idempotency_key,
                     error_code, started_at, finished_at, dispatch_claimed_at)
                    VALUES (?, ?, ?, 'running', ?, ?, NULL, ?, NULL, ?)""",
                (
                    attempt_id,
                    request.step_id,
                    attempt_number,
                    step.replay_safety.value,
                    step.idempotency_key,
                    now_text,
                    now_text,
                ),
            )
            self._connection.execute(
                """INSERT INTO attempt_authorizations
                    (attempt_id, task_id, step_id, action_digest, device_id,
                     authorization_fingerprint) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    attempt_id,
                    request.task_id,
                    request.step_id,
                    binding.action_digest,
                    binding.device_id,
                    binding.authorization_fingerprint,
                ),
            )
            self._connection.execute(
                "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ? AND state = ?",
                (
                    TaskState.RUNNING.value,
                    now_text,
                    request.task_id,
                    TaskState.WAITING_APPROVAL.value,
                ),
            )
            self._connection.execute(
                """UPDATE steps SET state = ?, attempt_count = ?, updated_at = ?
                    WHERE id = ? AND state = ?""",
                (
                    StepState.RUNNING.value,
                    attempt_number,
                    now_text,
                    request.step_id,
                    StepState.WAITING_APPROVAL.value,
                ),
            )
            self._append_event(
                event_type=TaskEventType.STATE_CHANGED,
                goal_id=row["goal_id"],
                task_id=request.task_id,
                from_state=TaskState.WAITING_APPROVAL.value,
                to_state=TaskState.RUNNING.value,
                metadata=EventMetadata(
                    attempt_number=attempt_number,
                    replay_safety=step.replay_safety,
                    attempt_id=attempt_id,
                    action_digest=request.action_digest,
                    device_id=requesting_device_id,
                    authorization_fingerprint=authorization.authorization_fingerprint,
                ),
            )
            self._append_event(
                event_type=TaskEventType.ATTEMPT_STARTED,
                goal_id=row["goal_id"],
                task_id=request.task_id,
                step_id=request.step_id,
                from_state=StepState.WAITING_APPROVAL.value,
                to_state=StepState.RUNNING.value,
                metadata=EventMetadata(
                    attempt_number=attempt_number,
                    replay_safety=step.replay_safety,
                    attempt_id=attempt_id,
                    action_digest=request.action_digest,
                    device_id=requesting_device_id,
                    authorization_fingerprint=authorization.authorization_fingerprint,
                ),
            )
            self._append_approval_audit_event(
                request.model_copy(
                    update={
                        "status": ApprovalStatus.CONSUMED,
                        "consumed_at": now,
                        "consumed_by_device_id": requesting_device_id,
                    }
                ),
                event_type=ApprovalAuditEventType.CONSUMED,
                actor_device_id=requesting_device_id,
                attempt_id=attempt_id,
            )
            precommit_validation()
        return attempt.model_copy(update={"authorization_binding": binding})

    def invalidate_approved_approval(
        self, request_id: UUID | str, *, reason_code: str
    ) -> ApprovalRequestRecord:
        """Terminally expire or revoke an unusable approval and fail its waiter."""
        parsed_id = self._parse_approval_id(request_id)
        with self._transaction():
            row = self._approval_context(parsed_id)
            if row is None:
                raise EntityNotFoundError("Approval request was not found.")
            request = self._approval_request_from_row(row)
            if request.status is not ApprovalStatus.APPROVED:
                raise InvalidTransitionError("Approval request is unavailable.")
            expired = self.current_time() >= request.expires_at
            self._revoke_approved_approval_tx(
                row,
                request,
                owner_device_id=request.owner_device_id,
                reason_code="expired" if expired else reason_code,
                expired=expired,
            )
        return self.get_approval_request(parsed_id)

    @staticmethod
    def _parse_approval_id(request_id: UUID | str) -> UUID:
        try:
            return request_id if isinstance(request_id, UUID) else UUID(request_id)
        except (TypeError, ValueError, AttributeError):
            raise EntityNotFoundError("Approval request was not found.") from None

    def _approval_context(self, request_id: UUID) -> sqlite3.Row | None:
        return self._connection.execute(
            """SELECT approval_requests.*, tasks.state AS task_state, tasks.goal_id,
                steps.state AS step_state, steps.action_json,
                task_submissions.device_id AS task_owner_device_id
                FROM approval_requests
                JOIN tasks ON tasks.id = approval_requests.task_id
                JOIN steps ON steps.id = approval_requests.step_id
                LEFT JOIN task_submissions ON task_submissions.task_id = tasks.id
                WHERE approval_requests.request_id = ?""",
            (str(request_id),),
        ).fetchone()

    def _fail_waiting_approval_tx(
        self,
        row: sqlite3.Row,
        request: ApprovalRequestRecord,
        terminal: ApprovalStatus,
    ) -> None:
        now = self._now()
        self._connection.execute(
            "UPDATE tasks SET state = 'failed', updated_at = ? WHERE id = ? AND state = 'waiting_approval'",
            (now, request.task_id),
        )
        self._connection.execute(
            "UPDATE steps SET state = 'failed', updated_at = ? WHERE id = ? AND state = 'waiting_approval'",
            (now, request.step_id),
        )
        self._append_event(
            event_type=TaskEventType.STATE_CHANGED,
            goal_id=row["goal_id"],
            task_id=request.task_id,
            from_state=TaskState.WAITING_APPROVAL.value,
            to_state=TaskState.FAILED.value,
        )
        self._append_event(
            event_type=TaskEventType.STATE_CHANGED,
            goal_id=row["goal_id"],
            task_id=request.task_id,
            step_id=request.step_id,
            from_state=StepState.WAITING_APPROVAL.value,
            to_state=StepState.FAILED.value,
        )

    def _revoke_approved_approval_tx(
        self,
        row: sqlite3.Row,
        request: ApprovalRequestRecord,
        *,
        owner_device_id: str,
        reason_code: str,
        expired: bool,
    ) -> None:
        now = datetime.fromisoformat(self._now())
        status = ApprovalStatus.EXPIRED if expired else ApprovalStatus.REVOKED
        values = request.model_dump(mode="python")
        values["status"] = status
        if expired:
            values["expired_at"] = now
        else:
            values["revoked_at"] = now
            values["revoked_by_device_id"] = owner_device_id
        updated = ApprovalRequestRecord.model_validate(values)
        self._connection.execute(
            """UPDATE approval_requests SET status = ?, expired_at = ?, revoked_at = ?,
                revoked_by_device_id = ? WHERE request_id = ? AND status = 'approved'""",
            (
                status.value,
                None if updated.expired_at is None else _timestamp(updated.expired_at),
                None if updated.revoked_at is None else _timestamp(updated.revoked_at),
                updated.revoked_by_device_id,
                str(request.request_id),
            ),
        )
        self._append_approval_audit_event(
            updated,
            event_type=ApprovalAuditEventType(status.value),
            actor_device_id=owner_device_id,
            reason_code=reason_code,
        )
        self._fail_waiting_approval_tx(row, request, status)

    def get_approval_request(self, request_id: UUID | str) -> ApprovalRequestRecord:
        try:
            parsed_id = request_id if isinstance(request_id, UUID) else UUID(request_id)
        except (TypeError, ValueError, AttributeError):
            raise EntityNotFoundError("Approval request was not found.") from None
        row = self._connection.execute(
            "SELECT * FROM approval_requests WHERE request_id = ?", (str(parsed_id),)
        ).fetchone()
        if row is None:
            raise EntityNotFoundError("Approval request was not found.")
        try:
            return self._approval_request_from_row(row)
        except (ValidationError, TypeError, ValueError):
            raise TaskStoreError("Stored approval request is invalid.") from None

    def list_approval_requests(self, task_id: str) -> tuple[ApprovalRequestRecord, ...]:
        rows = self._connection.execute(
            "SELECT * FROM approval_requests WHERE task_id = ? ORDER BY created_at, request_id",
            (task_id,),
        ).fetchall()
        try:
            return tuple(self._approval_request_from_row(row) for row in rows)
        except (ValidationError, TypeError, ValueError):
            raise TaskStoreError("Stored approval request is invalid.") from None

    def _append_approval_audit_event(
        self,
        request: ApprovalRequestRecord,
        *,
        event_type: ApprovalAuditEventType,
        actor_device_id: str | None,
        reason_code: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        event = ApprovalAuditEvent(
            request_id=request.request_id,
            task_id=request.task_id,
            step_id=request.step_id,
            event_type=event_type,
            occurred_at=datetime.fromisoformat(self._now()),
            actor_device_id=actor_device_id,
            owner_credential_generation=request.owner_credential_generation,
            action_digest=request.action_digest,
            tool_definition_digest=request.tool_definition_digest,
            attempt_id=attempt_id,
            reason_code=reason_code,
        )
        self._connection.execute(
            """INSERT INTO approval_audit_events
                (event_id, request_id, task_id, step_id, event_type, occurred_at,
                 actor_device_id, owner_credential_generation, action_digest,
                 tool_definition_digest, attempt_id, reason_code)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(event.event_id), str(event.request_id), event.task_id, event.step_id,
                event.event_type.value, _timestamp(event.occurred_at), event.actor_device_id,
                event.owner_credential_generation, event.action_digest,
                event.tool_definition_digest, event.attempt_id, event.reason_code,
            ),
        )

    def list_approval_audit_events(
        self, request_id: UUID | str | None = None
    ) -> tuple[ApprovalAuditEvent, ...]:
        if request_id is None:
            rows = self._connection.execute(
                "SELECT * FROM approval_audit_events ORDER BY rowid"
            ).fetchall()
        else:
            try:
                parsed_id = request_id if isinstance(request_id, UUID) else UUID(request_id)
            except (TypeError, ValueError, AttributeError):
                raise EntityNotFoundError("Approval request was not found.") from None
            rows = self._connection.execute(
                """SELECT * FROM approval_audit_events WHERE request_id = ?
                    ORDER BY rowid""",
                (str(parsed_id),),
            ).fetchall()
        try:
            return tuple(self._approval_event_from_row(row) for row in rows)
        except (ValidationError, TypeError, ValueError):
            raise TaskStoreError("Stored approval audit event is invalid.") from None

    def get_goal(self, goal_id: str) -> Goal:
        row = self._connection.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
        if row is None:
            raise EntityNotFoundError("Goal was not found.")
        return Goal(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            state=GoalState(row["state"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def get_task(self, task_id: str) -> Task:
        row = self._connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise EntityNotFoundError("Task was not found.")
        return Task(
            id=row["id"],
            goal_id=row["goal_id"],
            title=row["title"],
            state=TaskState(row["state"]),
            planner_version=row["planner_version"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def get_step(self, step_id: str) -> Step:
        row = self._connection.execute("SELECT * FROM steps WHERE id = ?", (step_id,)).fetchone()
        if row is None:
            raise EntityNotFoundError("Step was not found.")
        dependencies = [
            str(dependency[0])
            for dependency in self._connection.execute(
                "SELECT depends_on_step_id FROM step_dependencies "
                "WHERE step_id = ? ORDER BY depends_on_step_id",
                (step_id,),
            )
        ]
        try:
            return Step(
                id=row["id"],
                task_id=row["task_id"],
                title=row["title"],
                dependencies=dependencies,
                state=StepState(row["state"]),
                replay_safety=ReplaySafety(row["replay_safety"]),
                idempotency_key=row["idempotency_key"],
                attempt_limit=row["attempt_limit"],
                attempt_count=row["attempt_count"],
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
                action=self._action_from_json(row["action_json"]),
            )
        except (ValidationError, TypeError, ValueError) as exc:
            raise TaskStoreError("Stored step is invalid.") from exc

    def list_steps(self, task_id: str) -> list[Step]:
        if self._connection.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
            raise EntityNotFoundError("Task was not found.")
        step_ids = self._connection.execute(
            "SELECT id FROM steps WHERE task_id = ? ORDER BY id", (task_id,)
        )
        return [self.get_step(str(row[0])) for row in step_ids]

    def get_step_result(self, step_id: str) -> StepResult | None:
        row = self._connection.execute(
            "SELECT result_json FROM steps WHERE id = ?", (step_id,)
        ).fetchone()
        if row is None:
            raise EntityNotFoundError("Step was not found.")
        if row["result_json"] is None:
            return None
        try:
            return StepResult.model_validate_json(row["result_json"])
        except ValidationError as exc:
            raise TaskStoreError("Stored step result is invalid.") from exc

    def transition_goal(self, goal_id: str, target: GoalState) -> Goal:
        with self._transaction():
            row = self._connection.execute(
                "SELECT state FROM goals WHERE id = ?", (goal_id,)
            ).fetchone()
            if row is None:
                raise EntityNotFoundError("Goal was not found.")
            current = GoalState(row["state"])
            validate_goal_transition(current, target)
            now = self._now()
            self._connection.execute(
                "UPDATE goals SET state = ?, updated_at = ? WHERE id = ?",
                (target.value, now, goal_id),
            )
            self._append_event(
                event_type=TaskEventType.STATE_CHANGED,
                goal_id=goal_id,
                from_state=current.value,
                to_state=target.value,
            )
        return self.get_goal(goal_id)

    def transition_task(self, task_id: str, target: TaskState) -> Task:
        with self._transaction():
            row = self._connection.execute(
                "SELECT goal_id, state FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise EntityNotFoundError("Task was not found.")
            current = TaskState(row["state"])
            if current is TaskState.WAITING_APPROVAL or target is TaskState.WAITING_APPROVAL:
                raise InvalidTransitionError(
                    "Approval waiting transitions require an atomic approval operation."
                )
            validate_task_transition(current, target)
            self._connection.execute(
                "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?",
                (target.value, self._now(), task_id),
            )
            self._append_event(
                event_type=TaskEventType.STATE_CHANGED,
                goal_id=row["goal_id"],
                task_id=task_id,
                from_state=current.value,
                to_state=target.value,
            )
        return self.get_task(task_id)

    def transition_step(self, step_id: str, target: StepState) -> Step:
        """Change a non-running step state; attempts own running outcomes."""
        with self._transaction():
            row = self._step_context(step_id)
            current = StepState(row["state"])
            if current is StepState.RUNNING or target is StepState.RUNNING:
                raise InvalidTransitionError("Running transitions require an attempt operation.")
            if (
                current is StepState.WAITING_APPROVAL
                or target is StepState.WAITING_APPROVAL
            ):
                raise InvalidTransitionError(
                    "Approval waiting transitions require an atomic approval operation."
                )
            if current is StepState.RECONCILIATION_REQUIRED:
                raise InvalidTransitionError("Reconciliation requires an explicit outcome.")
            validate_step_transition(current, target)
            self._connection.execute(
                "UPDATE steps SET state = ?, updated_at = ? WHERE id = ?",
                (target.value, self._now(), step_id),
            )
            self._append_event(
                event_type=TaskEventType.STATE_CHANGED,
                goal_id=row["goal_id"],
                task_id=row["task_id"],
                step_id=step_id,
                from_state=current.value,
                to_state=target.value,
            )
        return self.get_step(step_id)

    def start_step_attempt(
        self,
        step_id: str,
        *,
        authorization: AttemptAuthorization | None = None,
        denial: AttemptDenial | None = None,
    ) -> StepAttempt:
        with self._transaction():
            row = self._step_context(step_id)
            step = self._step_from_context(row)
            owner = self._connection.execute(
                """SELECT task_submissions.device_id, task_submissions.idempotency_key,
                    task_submissions.request_digest, goals.title, goals.description
                    FROM task_submissions JOIN tasks ON tasks.id = task_submissions.task_id
                    JOIN goals ON goals.id = tasks.goal_id WHERE task_submissions.task_id = ?""",
                (row["task_id"],),
            ).fetchone()
            if owner is not None:
                supplied = authorization or denial
                persisted_digest = (
                    TaskSubmissionProposal(
                        idempotency_key=owner["idempotency_key"],
                        title=owner["title"],
                        description=owner["description"],
                        action=step.action,
                    ).request_digest()
                    if step.action is not None
                    else None
                )
                if (
                    supplied is None
                    or (authorization is not None and denial is not None)
                    or step.action is None
                    or supplied.device_id != owner["device_id"]
                    or supplied.action_digest != step.action.digest()
                    or persisted_digest != owner["request_digest"]
                ):
                    raise TaskStoreError("Owned task attempt authorization is invalid.")
            elif authorization is not None or denial is not None:
                raise TaskStoreError("Unowned tasks cannot use device authorization bindings.")
            if step.state is not StepState.READY:
                raise InvalidTransitionError("Only ready steps can start an attempt.")
            if step.attempt_count >= step.attempt_limit:
                raise RetryLimitExceededError("Step attempt limit has been reached.")
            incomplete = self._connection.execute(
                """SELECT 1 FROM step_dependencies AS dependency
                    JOIN steps AS prerequisite ON prerequisite.id = dependency.depends_on_step_id
                    WHERE dependency.step_id = ? AND prerequisite.state != 'succeeded' LIMIT 1""",
                (step_id,),
            ).fetchone()
            if incomplete is not None:
                raise DependencyNotSatisfiedError("Step dependencies are not complete.")

            now = self._now()
            attempt_number = step.attempt_count + 1
            attempt = StepAttempt(
                id=uuid4().hex,
                step_id=step_id,
                attempt_number=attempt_number,
                outcome=AttemptOutcome.RUNNING,
                replay_safety=step.replay_safety,
                idempotency_key=step.idempotency_key,
                started_at=datetime.fromisoformat(now),
            )
            self._connection.execute(
                """INSERT INTO step_attempts
                    (id, step_id, attempt_number, outcome, replay_safety, idempotency_key,
                     error_code, started_at, finished_at)
                    VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL)""",
                (
                    attempt.id,
                    step_id,
                    attempt_number,
                    attempt.outcome.value,
                    attempt.replay_safety.value,
                    attempt.idempotency_key,
                    now,
                ),
            )
            binding = None
            if authorization is not None:
                binding = AttemptAuthorizationBinding(
                    task_id=row["task_id"],
                    step_id=step_id,
                    attempt_id=attempt.id,
                    attempt_number=attempt_number,
                    device_id=authorization.device_id,
                    action_digest=authorization.action_digest,
                    authorization_fingerprint=authorization.authorization_fingerprint,
                )
                self._connection.execute(
                    """INSERT INTO attempt_authorizations
                        (attempt_id, task_id, step_id, action_digest, device_id,
                         authorization_fingerprint) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        binding.attempt_id,
                        binding.task_id,
                        binding.step_id,
                        binding.action_digest,
                        binding.device_id,
                        binding.authorization_fingerprint,
                    ),
                )
            event_authorization = authorization or denial
            self._connection.execute(
                "UPDATE steps SET state = ?, attempt_count = ?, updated_at = ? WHERE id = ?",
                (StepState.RUNNING.value, attempt_number, now, step_id),
            )
            self._append_event(
                event_type=TaskEventType.ATTEMPT_STARTED,
                goal_id=row["goal_id"],
                task_id=row["task_id"],
                step_id=step_id,
                from_state=StepState.READY.value,
                to_state=StepState.RUNNING.value,
                metadata=EventMetadata(
                    attempt_number=attempt_number,
                    replay_safety=step.replay_safety,
                    attempt_id=attempt.id,
                    action_digest=(
                        None
                        if event_authorization is None
                        else event_authorization.action_digest
                    ),
                    device_id=(
                        None
                        if event_authorization is None
                        else event_authorization.device_id
                    ),
                    authorization_fingerprint=(
                        None
                        if event_authorization is None
                        else event_authorization.authorization_fingerprint
                    ),
                ),
            )
        return attempt.model_copy(update={"authorization_binding": binding})

    def finish_step_attempt(
        self,
        step_id: str,
        outcome: AttemptOutcome,
        *,
        error_code: SafeErrorCode | None = None,
        result: StepResult | None = None,
        allow_safe_replay: bool = False,
    ) -> Step:
        if outcome is AttemptOutcome.RUNNING:
            raise ValueError("A finished attempt requires a final outcome.")
        if result is not None and outcome is not AttemptOutcome.SUCCEEDED:
            raise ValueError("Only successful attempts can store a step result.")
        with self._transaction():
            row = self._step_context(step_id)
            step = self._step_from_context(row)
            if step.state is not StepState.RUNNING:
                raise ActiveAttemptError("Step has no running attempt.")
            active = self._connection.execute(
                """SELECT step_attempts.id, step_attempts.attempt_number,
                    attempt_authorizations.action_digest,
                    attempt_authorizations.device_id,
                    attempt_authorizations.authorization_fingerprint,
                    EXISTS(SELECT 1 FROM approval_audit_events
                        WHERE approval_audit_events.attempt_id = step_attempts.id
                            AND approval_audit_events.event_type = 'consumed')
                        AS approval_consumed
                    FROM step_attempts LEFT JOIN attempt_authorizations
                        ON attempt_authorizations.attempt_id = step_attempts.id
                    WHERE step_attempts.step_id = ? AND step_attempts.outcome = 'running'""",
                (step_id,),
            ).fetchone()
            if active is None:
                raise ActiveAttemptError("Step has no persisted running attempt.")

            if outcome is AttemptOutcome.SUCCEEDED:
                target = StepState.SUCCEEDED
            elif outcome is AttemptOutcome.OUTCOME_UNKNOWN:
                error_code = SafeErrorCode.OUTCOME_UNKNOWN
                safe_replay = step.replay_safety is ReplaySafety.PURE or (
                    step.replay_safety is ReplaySafety.IDEMPOTENT and allow_safe_replay
                )
                if active["approval_consumed"]:
                    target = StepState.RECONCILIATION_REQUIRED
                elif safe_replay and step.attempt_count < step.attempt_limit:
                    target = StepState.RETRY_WAIT
                elif step.replay_safety is ReplaySafety.PURE:
                    target = StepState.FAILED
                else:
                    target = StepState.RECONCILIATION_REQUIRED
            elif outcome is AttemptOutcome.PERMANENT_FAILURE:
                target = StepState.FAILED
                error_code = error_code or SafeErrorCode.PERMANENT
            else:
                target = (
                    StepState.RETRY_WAIT
                    if step.attempt_count < step.attempt_limit
                    else StepState.FAILED
                )
                error_code = error_code or SafeErrorCode.TRANSIENT

            validate_step_transition(StepState.RUNNING, target)
            now = self._now()
            self._connection.execute(
                """UPDATE step_attempts SET outcome = ?, error_code = ?, finished_at = ?
                    WHERE id = ?""",
                (outcome.value, error_code.value if error_code else None, now, active["id"]),
            )
            self._connection.execute(
                "UPDATE steps SET state = ?, updated_at = ? WHERE id = ?",
                (target.value, now, step_id),
            )
            if result is not None:
                self._connection.execute(
                    "UPDATE steps SET result_json = ? WHERE id = ?",
                    (_json_dumps(result.model_dump(mode="json")), step_id),
                )
            self._append_event(
                event_type=TaskEventType.ATTEMPT_FINISHED,
                goal_id=row["goal_id"],
                task_id=row["task_id"],
                step_id=step_id,
                from_state=StepState.RUNNING.value,
                to_state=target.value,
                metadata=EventMetadata(
                    attempt_number=active["attempt_number"],
                    replay_safety=step.replay_safety,
                    outcome=outcome,
                    error_code=error_code,
                    attempt_id=active["id"],
                    action_digest=active["action_digest"],
                    device_id=active["device_id"],
                    authorization_fingerprint=active["authorization_fingerprint"],
                ),
            )
            if target is StepState.SUCCEEDED:
                self._insert_checkpoint(row["task_id"], step_id)
        return self.get_step(step_id)

    def claim_dispatch(self, binding: AttemptAuthorizationBinding) -> None:
        """Atomically consume a single dispatch right for one bound attempt."""
        with self._transaction():
            row = self._connection.execute(
                """SELECT step_attempts.outcome, step_attempts.dispatch_claimed_at,
                    steps.state AS step_state, steps.action_json, steps.task_id,
                    attempt_authorizations.task_id AS binding_task_id,
                    attempt_authorizations.step_id AS binding_step_id,
                    attempt_authorizations.action_digest,
                    attempt_authorizations.device_id,
                    attempt_authorizations.authorization_fingerprint
                    FROM step_attempts
                    JOIN steps ON steps.id = step_attempts.step_id
                    LEFT JOIN attempt_authorizations
                        ON attempt_authorizations.attempt_id = step_attempts.id
                    WHERE step_attempts.id = ? AND step_attempts.step_id = ?
                        AND step_attempts.attempt_number = ?""",
                (binding.attempt_id, binding.step_id, binding.attempt_number),
            ).fetchone()
            if row is None:
                raise ActiveAttemptError("Attempt is unavailable for dispatch.")
            action = self._action_from_json(row["action_json"])
            owner = self._connection.execute(
                """SELECT task_submissions.device_id, task_submissions.idempotency_key,
                    task_submissions.request_digest, goals.title, goals.description
                    FROM task_submissions JOIN tasks ON tasks.id = task_submissions.task_id
                    JOIN goals ON goals.id = tasks.goal_id WHERE task_submissions.task_id = ?""",
                (row["task_id"],),
            ).fetchone()
            persisted_digest = (
                TaskSubmissionProposal(
                    idempotency_key=owner["idempotency_key"],
                    title=owner["title"],
                    description=owner["description"],
                    action=action,
                ).request_digest()
                if owner is not None and action is not None
                else None
            )
            if (
                row["outcome"] != AttemptOutcome.RUNNING.value
                or row["step_state"] != StepState.RUNNING.value
                or row["dispatch_claimed_at"] is not None
                or row["binding_task_id"] != binding.task_id
                or row["binding_step_id"] != binding.step_id
                or row["action_digest"] != binding.action_digest
                or row["device_id"] != binding.device_id
                or row["authorization_fingerprint"] != binding.authorization_fingerprint
                or row["task_id"] != binding.task_id
                or action is None
                or action.digest() != binding.action_digest
                or owner is None
                or owner["device_id"] != binding.device_id
                or persisted_digest != owner["request_digest"]
            ):
                raise ActiveAttemptError("Attempt is unavailable for dispatch.")
            cursor = self._connection.execute(
                "UPDATE step_attempts SET dispatch_claimed_at = ? "
                "WHERE id = ? AND dispatch_claimed_at IS NULL AND outcome = 'running'",
                (self._now(), binding.attempt_id),
            )
            if cursor.rowcount != 1:
                raise ActiveAttemptError("Attempt is unavailable for dispatch.")

    def reconcile_step(
        self,
        step_id: str,
        outcome: ReconciliationOutcome,
        *,
        confirmation_id: UUID,
        result: StepResult | None = None,
    ) -> Step:
        """Record an explicit opaque confirmation; this method does not execute work."""
        if result is not None and outcome is not ReconciliationOutcome.CONFIRMED_SUCCEEDED:
            raise ValueError("Only confirmed successful outcomes can store a step result.")
        with self._transaction():
            row = self._step_context(step_id)
            step = self._step_from_context(row)
            if step.state is not StepState.RECONCILIATION_REQUIRED:
                raise InvalidTransitionError("Only steps requiring reconciliation can be resolved.")
            if outcome is ReconciliationOutcome.CONFIRMED_SUCCEEDED:
                target = StepState.SUCCEEDED
            elif outcome is ReconciliationOutcome.CONFIRMED_NOT_APPLIED:
                target = (
                    StepState.READY
                    if step.attempt_count < step.attempt_limit
                    else StepState.FAILED
                )
            else:
                target = StepState.FAILED
            validate_step_transition(StepState.RECONCILIATION_REQUIRED, target)
            self._connection.execute(
                "UPDATE steps SET state = ?, updated_at = ? WHERE id = ?",
                (target.value, self._now(), step_id),
            )
            if result is not None:
                self._connection.execute(
                    "UPDATE steps SET result_json = ? WHERE id = ?",
                    (_json_dumps(result.model_dump(mode="json")), step_id),
                )
            self._append_event(
                event_type=TaskEventType.RECONCILED,
                goal_id=row["goal_id"],
                task_id=row["task_id"],
                step_id=step_id,
                from_state=StepState.RECONCILIATION_REQUIRED.value,
                to_state=target.value,
                metadata=EventMetadata(outcome=outcome, confirmation_id=confirmation_id),
            )
            if target is StepState.SUCCEEDED:
                self._insert_checkpoint(row["task_id"], step_id)
        return self.get_step(step_id)

    def recover_interrupted_attempt(
        self, step_id: str, *, allow_idempotent_replay: bool
    ) -> Step:
        """Close an orphaned running attempt and authorize replay only when safe."""
        with self._transaction():
            row = self._step_context(step_id)
            step = self._step_from_context(row)
            if step.state is not StepState.RUNNING:
                raise ActiveAttemptError("Step has no interrupted running attempt.")
            active = self._connection.execute(
                """SELECT step_attempts.id, step_attempts.attempt_number,
                    attempt_authorizations.action_digest,
                    attempt_authorizations.device_id,
                    attempt_authorizations.authorization_fingerprint,
                    EXISTS(SELECT 1 FROM approval_audit_events
                        WHERE approval_audit_events.attempt_id = step_attempts.id
                            AND approval_audit_events.event_type = 'consumed')
                        AS approval_consumed
                    FROM step_attempts LEFT JOIN attempt_authorizations
                        ON attempt_authorizations.attempt_id = step_attempts.id
                    WHERE step_attempts.step_id = ? AND step_attempts.outcome = 'running'""",
                (step_id,),
            ).fetchone()
            if active is None:
                raise ActiveAttemptError("Step has no persisted running attempt.")

            if active["approval_consumed"]:
                target = StepState.RECONCILIATION_REQUIRED
            elif step.replay_safety is ReplaySafety.PURE:
                target = (
                    StepState.RETRY_WAIT
                    if step.attempt_count < step.attempt_limit
                    else StepState.FAILED
                )
            elif (
                step.replay_safety is ReplaySafety.IDEMPOTENT
                and allow_idempotent_replay
                and step.attempt_count < step.attempt_limit
            ):
                target = StepState.RETRY_WAIT
            else:
                target = StepState.RECONCILIATION_REQUIRED
            validate_step_transition(StepState.RUNNING, target)
            now = self._now()
            self._connection.execute(
                "UPDATE step_attempts SET outcome = ?, error_code = ?, finished_at = ? WHERE id = ?",
                (
                    AttemptOutcome.OUTCOME_UNKNOWN.value,
                    SafeErrorCode.OUTCOME_UNKNOWN.value,
                    now,
                    active["id"],
                ),
            )
            self._connection.execute(
                "UPDATE steps SET state = ?, updated_at = ? WHERE id = ?",
                (target.value, now, step_id),
            )
            self._append_event(
                event_type=TaskEventType.ATTEMPT_FINISHED,
                goal_id=row["goal_id"],
                task_id=row["task_id"],
                step_id=step_id,
                from_state=StepState.RUNNING.value,
                to_state=target.value,
                metadata=EventMetadata(
                    attempt_number=active["attempt_number"],
                    replay_safety=step.replay_safety,
                    outcome=AttemptOutcome.OUTCOME_UNKNOWN,
                    error_code=SafeErrorCode.OUTCOME_UNKNOWN,
                    attempt_id=active["id"],
                    action_digest=active["action_digest"],
                    device_id=active["device_id"],
                    authorization_fingerprint=active["authorization_fingerprint"],
                ),
            )
        return self.get_step(step_id)

    def _step_context(self, step_id: str) -> sqlite3.Row:
        row = self._connection.execute(
            """SELECT steps.*, tasks.goal_id FROM steps
                JOIN tasks ON tasks.id = steps.task_id WHERE steps.id = ?""",
            (step_id,),
        ).fetchone()
        if row is None:
            raise EntityNotFoundError("Step was not found.")
        return row

    @staticmethod
    def _step_from_context(row: sqlite3.Row) -> Step:
        try:
            return Step(
                id=row["id"],
                task_id=row["task_id"],
                title=row["title"],
                state=StepState(row["state"]),
                replay_safety=ReplaySafety(row["replay_safety"]),
                idempotency_key=row["idempotency_key"],
                attempt_limit=row["attempt_limit"],
                attempt_count=row["attempt_count"],
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
                action=TaskStore._action_from_json(row["action_json"]),
            )
        except (ValidationError, TypeError, ValueError) as exc:
            raise TaskStoreError("Stored step is invalid.") from exc

    @staticmethod
    def _action_from_json(value: str | None) -> AuthorizedTaskAction | None:
        if value is None:
            return None
        try:
            return AuthorizedTaskAction.model_validate_json(value)
        except (ValidationError, TypeError, ValueError) as exc:
            raise TaskStoreError("Stored step action is invalid.") from exc

    def _insert_checkpoint(self, task_id: str, step_id: str) -> None:
        sequence_row = self._connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM task_checkpoints WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        sequence = int(sequence_row[0])
        completed_ids = [
            str(row[0])
            for row in self._connection.execute(
                "SELECT id FROM steps WHERE task_id = ? AND state = 'succeeded' ORDER BY id",
                (task_id,),
            )
        ]
        self._connection.execute(
            """INSERT INTO task_checkpoints
                (id, task_id, step_id, sequence, completed_step_ids_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
            (uuid4().hex, task_id, step_id, sequence, _json_dumps(completed_ids), self._now()),
        )

    def list_events(
        self,
        *,
        goal_id: str | None = None,
        task_id: str | None = None,
        step_id: str | None = None,
    ) -> list[TaskEvent]:
        clauses: list[str] = []
        parameters: list[str] = []
        for column, value in (("goal_id", goal_id), ("task_id", task_id), ("step_id", step_id)):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT * FROM task_events{where} ORDER BY id", parameters
        )
        return [
            TaskEvent(
                id=row["id"],
                event_type=TaskEventType(row["event_type"]),
                goal_id=row["goal_id"],
                task_id=row["task_id"],
                step_id=row["step_id"],
                from_state=row["from_state"],
                to_state=row["to_state"],
                occurred_at=datetime.fromisoformat(row["occurred_at"]),
                metadata=EventMetadata.model_validate_json(row["metadata_json"]),
            )
            for row in rows
        ]

    def list_attempts(self, step_id: str) -> list[StepAttempt]:
        rows = self._connection.execute(
            """SELECT step_attempts.*, attempt_authorizations.task_id AS binding_task_id,
                attempt_authorizations.step_id AS binding_step_id,
                attempt_authorizations.action_digest AS binding_action_digest,
                attempt_authorizations.device_id AS binding_device_id,
                attempt_authorizations.authorization_fingerprint AS binding_fingerprint
                FROM step_attempts LEFT JOIN attempt_authorizations
                    ON attempt_authorizations.attempt_id = step_attempts.id
                WHERE step_attempts.step_id = ? ORDER BY step_attempts.attempt_number""",
            (step_id,),
        )
        return [
            StepAttempt(
                id=row["id"],
                step_id=row["step_id"],
                attempt_number=row["attempt_number"],
                outcome=AttemptOutcome(row["outcome"]),
                replay_safety=ReplaySafety(row["replay_safety"]),
                idempotency_key=row["idempotency_key"],
                authorization_binding=(
                    None
                    if row["binding_task_id"] is None
                    else AttemptAuthorizationBinding(
                        task_id=row["binding_task_id"],
                        step_id=row["binding_step_id"],
                        attempt_id=row["id"],
                        attempt_number=row["attempt_number"],
                        device_id=row["binding_device_id"],
                        action_digest=row["binding_action_digest"],
                        authorization_fingerprint=row["binding_fingerprint"],
                    )
                ),
                error_code=SafeErrorCode(row["error_code"]) if row["error_code"] else None,
                started_at=datetime.fromisoformat(row["started_at"]),
                finished_at=(
                    datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None
                ),
                dispatch_claimed_at=(
                    datetime.fromisoformat(row["dispatch_claimed_at"])
                    if row["dispatch_claimed_at"]
                    else None
                ),
            )
            for row in rows
        ]

    def list_checkpoints(self, task_id: str) -> list[TaskCheckpoint]:
        rows = self._connection.execute(
            "SELECT * FROM task_checkpoints WHERE task_id = ? ORDER BY sequence", (task_id,)
        )
        return [
            TaskCheckpoint(
                id=row["id"],
                task_id=row["task_id"],
                sequence=row["sequence"],
                step_id=row["step_id"],
                completed_step_ids=json.loads(row["completed_step_ids_json"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]
