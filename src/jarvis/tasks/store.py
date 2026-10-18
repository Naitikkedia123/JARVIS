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

from jarvis.tasks.errors import (
    ActiveAttemptError,
    DependencyNotSatisfiedError,
    EntityNotFoundError,
    InvalidDependencyError,
    InvalidTransitionError,
    RetryLimitExceededError,
    TaskStoreError,
)
from jarvis.tasks.models import (
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
CURRENT_SCHEMA_VERSION = 2

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
            self._connection.commit()

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

    def _now(self) -> str:
        try:
            return _timestamp(self._clock())
        except (TypeError, ValueError):
            raise TaskStoreError("The task store clock must return a timezone-aware timestamp.") from None

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
        return goal

    def add_task_plan(self, task: Task, steps: Iterable[Step]) -> TaskPlan:
        step_list = list(steps)
        if task.state is not TaskState.READY:
            raise TaskStoreError("A new task must begin in the ready state.")
        if any(step.state is not StepState.PENDING for step in step_list):
            raise TaskStoreError("New plan steps must begin in the pending state.")

        with self._transaction():
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

            plan = TaskPlan(task=task, steps=step_list)

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
                             attempt_limit, attempt_count, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            step.id,
                            step.task_id,
                            step.title,
                            step.state.value,
                            step.replay_safety.value,
                            step.idempotency_key,
                            step.attempt_limit,
                            step.attempt_count,
                            _timestamp(step.created_at),
                            _timestamp(step.updated_at),
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
        return plan

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
        )

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

    def start_step_attempt(self, step_id: str) -> StepAttempt:
        with self._transaction():
            row = self._step_context(step_id)
            step = self._step_from_context(row)
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
                ),
            )
        return attempt

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
                """SELECT id, attempt_number FROM step_attempts
                    WHERE step_id = ? AND outcome = 'running'""",
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
                if safe_replay and step.attempt_count < step.attempt_limit:
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
                ),
            )
            if target is StepState.SUCCEEDED:
                self._insert_checkpoint(row["task_id"], step_id)
        return self.get_step(step_id)

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
                "SELECT id, attempt_number FROM step_attempts "
                "WHERE step_id = ? AND outcome = 'running'",
                (step_id,),
            ).fetchone()
            if active is None:
                raise ActiveAttemptError("Step has no persisted running attempt.")

            if step.replay_safety is ReplaySafety.PURE:
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
        )

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
            "SELECT * FROM step_attempts WHERE step_id = ? ORDER BY attempt_number", (step_id,)
        )
        return [
            StepAttempt(
                id=row["id"],
                step_id=row["step_id"],
                attempt_number=row["attempt_number"],
                outcome=AttemptOutcome(row["outcome"]),
                replay_safety=ReplaySafety(row["replay_safety"]),
                idempotency_key=row["idempotency_key"],
                error_code=SafeErrorCode(row["error_code"]) if row["error_code"] else None,
                started_at=datetime.fromisoformat(row["started_at"]),
                finished_at=(
                    datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None
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
