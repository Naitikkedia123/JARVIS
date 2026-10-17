# Phase 04 — Typed Tool Registry and Permission Policy

Implement a small typed tool registry and separate authorization engine. Read `AGENTS.md`, `THREAT_MODEL.md`, architecture and tests.

Only harmless initial tools: health/status, list files under a dedicated test workspace, read known file, create file within a temporary test workspace. Each tool declares input/output schema, required capabilities, read/write class, timeout, result-size limit and audit fields. Keep tool execution separate from model output. Add an approval-request interface for higher risk, but do not connect real accounts or enable arbitrary shell commands.

Treat tool content as untrusted. Test that a mock model's unauthorized tool request is blocked by policy code. Test path traversal outside workspace. No destructive command, network mutation, secrets or external side effect.

Acceptance: policy denial, typed tool errors/timeouts, observable tool results, audit records, path restriction, tests and Ruff. Update threat model/status. No commit/push.
