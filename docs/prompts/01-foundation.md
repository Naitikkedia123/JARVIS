# Phase 01 — Reliable Application Foundation

Read `AGENTS.md`, architecture, Phase 00 ADR, status and tests. Work only on branch `phase/01-foundation` created from the accepted Phase 00 merge.

Implement the foundation: FastAPI structure, typed settings from environment with safe defaults, structured logging with redaction support, consistent error handling, health/readiness endpoints, tests, Ruff compliance, local setup/start scripts, and minimal CI. Keep the app bound to `127.0.0.1`. Do not add model APIs, persistent memory, arbitrary shell tools, mobile code or new frameworks.

List relevant current files, then provide a short plan and expected files before edits. Preserve `/health` behavior unless tests justify a compatible change. If more than about 8 files are needed, explain and split.

Acceptance: clean setup works on Python 3.12; `scripts/setup-dev.ps1`, `scripts/check.ps1`, and `scripts/run-local.ps1` work in Windows PowerShell; health endpoint tests pass; Ruff/pytest pass; logs do not expose secrets; CI runs same checks without secrets. Update `PROJECT_STATUS.md`. Do not commit, push or switch branches.
