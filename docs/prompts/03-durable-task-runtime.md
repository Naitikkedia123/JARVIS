# Phase 03 — Goals, Plans and Durable Tasks

Read project instructions, architecture, ADR, status and tests. Implement only the goal/task persistence and execution-state slice. Use SQLite or approved minimal persistence; add versioned migrations if schema changes warrant them. Use deterministic plans for tests; no LLM calls in unit tests.

Implement typed goal/task/step models, finite task states, dependency validation, audit timestamps/events, pause/resume/cancel, checkpoints, bounded retries and duplicate-execution safeguards for external effects. Keep planner replaceable. Do not add real external tools or send messages.

Acceptance: allowed state transitions tested; invalid transitions fail; task survives process restart and resumes from checkpoint; completed non-idempotent steps aren't silently repeated; cancellation works; errors are visible. If scope exceeds this slice, propose a split first. Update status. No commit/push.
