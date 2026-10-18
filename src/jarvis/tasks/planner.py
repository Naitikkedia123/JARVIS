"""Replaceable planner contracts and a deterministic, model-free planner."""

from __future__ import annotations

from hashlib import sha256
from typing import Protocol
from uuid import UUID, uuid5

from jarvis.tasks.models import Goal, ReplaySafety, Step, Task, TaskPlan

_TASK_NAMESPACE = UUID("d9708cd4-b158-4d0f-9f0d-f5f38ec63f06")


class Planner(Protocol):
    """Build a validated task plan from a persisted goal."""

    @property
    def version(self) -> str: ...

    def plan(self, goal: Goal) -> TaskPlan: ...


class DeterministicPlanner:
    """Create a stable one-step plan without calling a model or external service."""

    version = "deterministic-v1"

    def plan(self, goal: Goal) -> TaskPlan:
        task_id = uuid5(_TASK_NAMESPACE, f"task:{goal.id}").hex
        step_id = uuid5(_TASK_NAMESPACE, f"step:{goal.id}").hex
        suffix = sha256(goal.title.encode("utf-8")).hexdigest()[:8]
        task = Task(
            id=task_id,
            goal_id=goal.id,
            title=f"Plan for {goal.title}"[:200],
            planner_version=self.version,
            created_at=goal.created_at,
            updated_at=goal.created_at,
        )
        step = Step(
            id=step_id,
            task_id=task_id,
            title=f"Complete goal {suffix}",
            replay_safety=ReplaySafety.PURE,
            created_at=goal.created_at,
            updated_at=goal.created_at,
        )
        return TaskPlan(task=task, steps=[step])
