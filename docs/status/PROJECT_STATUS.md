# JARVIS Project Status

**Last updated:** Phase 03 acceptance — 18 October 2026
**Current stage:** Phase 03 — Goals, Plans and Durable Tasks accepted; owner review and commit/push pending
**Current branch:** `feat/durable-task-runtime`
**Status:** Phase 01 is accepted and merged into `main`; Phases 02A and 02B are implemented and accepted, with local Ollama gateway validation documented. Phase 03 passed its implementation reviews and validation: 38 focused tests and 101 full-suite tests passed under Python 3.12.4, and Ruff and `git diff --check` passed. Phase 03 has not yet been committed or pushed.

## Phase 00 completed work (2026-10-17)
- Prepared `docs/decisions/0001-architecture-choice.md` with a weighted A/B/C scorecard (weights total 100%), evidence, trade-offs, and reversal criteria. Recommendation: JARVIS-owned Python control plane with selective adapters; evaluate OpenClaw as an external local Gateway before considering a fork.
- Prepared `docs/research/FEASIBILITY_REPORT.md` using current official project repositories and documentation for OpenClaw, Open Interpreter, OpenHands SDK, Letta Code, LangGraph, Android, Windows UI Automation, WireGuard, Codex/Go, and Antigravity. It distinguishes verified facts, estimates, assumptions, and unresolved account/device questions; links sources and records release dates where available.
- Confirmed in the documented source snapshot that Codex is included with ChatGPT Go while Codex Cloud is not; account-specific quota was not inspected. Antigravity overage can be set to “Never” where the control is available; the user's account panel was not inspected. No fixed agent task quotas were inferred.
- No packages were installed; no model weights were downloaded; no personal account/device was accessed; no personal integrations, cloud resources, paid services, or runtime features were configured or implemented.
- The project owner reviewed and accepted Phase 00 and Option B: a JARVIS-owned Python control plane with selective adapters.
- Phase 01 — Foundation was approved after Phase 00; its implementation and acceptance are recorded below.

## Phase 01 implementation (2026-10-17)
- Added typed environment settings with safe defaults; a configured host other than `127.0.0.1` is rejected. The local runner continues to bind to `127.0.0.1:8000`.
- Added structured JSON logging with redaction for password, token, API-key, authorization, cookie and credential fields/values. Uvicorn access logging is disabled to avoid request-target leaks, and Uvicorn error records use the structured formatter without exception messages or tracebacks. Error handlers return standardized generic responses and do not expose validation inputs, exception messages, stack traces or settings.
- Preserved `/health` exactly as `{"status":"ok","service":"jarvis"}` and added deterministic `/ready` returning `{"status":"ready","service":"jarvis"}`. Readiness has no external dependency checks because none exist in this foundation.
- Regression coverage includes health/readiness, loopback setting rejection, secret redaction, disabled Uvicorn access logs, and generic HTTP, validation and internal-error responses.
- `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\check.ps1` — passed under Python 3.13: Ruff passed; pytest reported 12 passed, 1 third-party Starlette/httpx deprecation warning; `git diff --check` passed (Git emitted LF-to-CRLF conversion warnings).
- `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-local.ps1` — started successfully. `/health` and `/ready` returned the payloads above. `netstat -ano -p tcp` showed the listener only at `127.0.0.1:8000`; the test server was stopped afterward.
- `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-dev.ps1` — historical attempt did not complete: that agent session's `py -0p` showed Python 3.13 only and `py -3.12` could not select a runtime. This does not establish that Python 3.12 is absent machine-wide. A Python 3.13 `.venv` was created and existing declared dependencies were installed to run the checks; no dependency declarations changed. The clean Python 3.12 setup remains unverified.
- CI workflow inspection confirms Python 3.12 setup, Ruff and pytest steps with no secrets. A remote CI run has not been performed.

## Initial scaffold verification (Phase 01 baseline)
- FastAPI system endpoints exist at `/health` and `/ready`.
- Automated endpoint and foundation security/error tests exist.
- Setup, check and run scripts are provided. Python 3.12 setup was unverified at the scaffold stage; Phase 03 acceptance below records later Python 3.12.4 test evidence.

## Later phases not implemented
- Production model routing is not wired into FastAPI routes; the validated gateway remains an internal provider/routing component.
- Phase 04 tool registry and authorization policy.
- Persistent memory.
- Windows automation, Android, communication integrations, voice, and vision.

## Next task
Phase 03 is accepted. Review and commit/push are pending owner action. Begin a later phase only after separate approval.

## Earlier checks before Phase 01 acceptance
- `scripts/check.ps1` under Python 3.13: passed; Ruff passed and 12 tests passed, with 1 third-party deprecation warning.
- `scripts/run-local.ps1`: passed; health/readiness payloads verified and listener confirmed on `127.0.0.1:8000` only.
- `scripts/setup-dev.ps1`: Python 3.12 setup is unverified. The launcher discrepancy requires confirmation in the user's PowerShell session; remote CI has not been run.
- `git diff --check`: passed; Git reported line-ending conversion warnings only.

## Targeted correction verification (2026-10-17)
- Existing `.venv\Scripts\python.exe --version`: Python 3.13.1. It was not replaced or recreated.
- Existing-environment Ruff command `.venv\Scripts\python.exe -m ruff check .`: passed.
- Existing-environment pytest command `.venv\Scripts\python.exe -m pytest -q`: 13 passed, with 1 existing Starlette/httpx deprecation warning. This includes the Uvicorn error-logger configuration regression test, which calls `logger.exception` with a unique fake secret and confirms the exception message and traceback are absent from captured output. This is a direct logger-configuration test, not a full server-request integration test.
- PowerShell parser static check passed for `scripts/setup-dev.ps1`, `scripts/check.ps1`, and `scripts/run-local.ps1`. Each script was also invoked with `-VenvPath .venv`; all three refused the Python 3.13.1 environment with a Python 3.12 requirement message and left it unchanged.
- The correction makes all three scripts accept `-VenvPath`, verify the actual target interpreter is Python 3.12, and refuse an existing wrong-version target without deleting or recreating it. Setup checks `py -3.12` before creating a new target. `Microsoft/` and the existing `.venv` were left untouched.
- At the time of this historical correction, Python 3.12 setup and script execution against a valid 3.12 environment were unverified in that agent session. The Phase 03 acceptance record below includes the owner-provided Python 3.12.4 test results; this historical note is not the current Phase 03 validation result.

## Real Uvicorn exception-log verification (2026-10-17)
- Added a real loopback server regression test using an ephemeral bound socket and an HTTP request to a test-only failing endpoint. It verifies the generic 500 response, captures an actual `uvicorn.error` record, and confirms the fake secret, raw exception message, and traceback are absent. The server thread and socket are shut down in `finally` cleanup.
- The new test passed under the existing Python 3.13.1 `.venv`; the full suite reported 14 passed with the existing Starlette/httpx deprecation warning. Ruff passed during this verification.
- At that time, the owner reported earlier Python 3.12.4 checks and a local server run using `.venv-py312`. That execution context could not independently repeat them: `.venv-py312\pyvenv.cfg` recorded Python 3.12.4 and a base path under `C:\Users\user\AppData\Local\Programs\Python\Python312`, but invoking its `python.exe` reported that the recorded base interpreter path was unavailable there.
- Requested command `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\check.ps1 -VenvPath ".venv-py312"` exited 1 before Ruff or pytest because that interpreter could not launch. No environment was installed, upgraded, deleted, or recreated. Remote CI remains unverified.

The preceding check sections preserve earlier agent-session observations. The final Phase 01 acceptance verification recorded above supersedes their pending-acceptance and unverified-CI status: the owner confirmed the Python 3.12.4 local results and successful GitHub Actions run.

## Phase 02A — Provider-neutral model gateway core (2026-10-17)
- Added Pydantic request/response, message, capability, usage and provider-output contracts; a minimal asynchronous provider and routing-policy protocol; typed safe gateway errors; an explicit no-fallback router; and a deterministic mock provider. The gateway is internal and is not wired to FastAPI routes.
- Structured output is checked against a requested Pydantic model only when the selected provider declares support. Provider errors and timeout outcomes are normalized without raw messages; caller cancellation propagates. Usage and provider-reported quota fields remain optional and non-negative; the gateway does not enforce quotas.
- Ruff and `git diff --check` passed; the full suite reported 30 passed under the existing Python 3.13.1 `.venv`, with one existing Starlette/httpx deprecation warning. These tests use no network sockets, credentials, services, or model weights.
- This execution context previously could not launch `.venv-py312` because its recorded base Python executable path was unavailable. Antigravity subsequently independently verified `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\check.ps1 -VenvPath ".venv-py312"` with exit code 0 using Python 3.12.4: Ruff passed, all 30 tests passed in 1.55 seconds, and Git whitespace checks passed. One existing Starlette/httpx deprecation warning remains. This is an independently reported result; it was not rerun in this documentation-only task.
- No dependency, CI, script, configuration, route, local-provider, remote-provider, or model-runtime changes were made in the Phase 02A core slice. The gateway was subsequently validated as recorded in the Phase 02B and gateway validation sections.

## Phase 02B — Local Ollama adapter (2026-10-17)
- Implemented a local-only asynchronous adapter for Ollama's native `/api/chat` endpoint, with a loopback-literal URL restriction, `trust_env=False`, a configured `qwen3.5:2b` model identifier, explicit context length, typed error normalization, and offline mock-transport tests. The adapter is not implicitly registered with the gateway router.
- Antigravity independently reviewed the adapter and reported **PASS WITH FINDINGS**, with no critical or high-severity issues. The reviewer found no mandatory source-code corrections before acceptance.
- The reviewer recorded `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\check.ps1 -VenvPath ".venv-py312"` passing under Python 3.12.4: Ruff passed; all 56 tests passed (14 foundation, 16 gateway-core, 26 local-adapter); Git whitespace checks passed. One existing Starlette/httpx deprecation warning remains. These are the independent reviewer's results, not a rerun in this documentation update.
- Phase 02B acceptance is complete. Live adapter and gateway-routing observations, including the selected local model, are recorded in [GATEWAY_VALIDATION.md](../research/GATEWAY_VALIDATION.md). The report limits its claims to the tested local setup and does not claim full production application integration.

## Phase 03 — Goals, Plans and Durable Tasks (accepted)
- Pass A added typed goal/task/step models, finite-state transitions, dependency validation, SQLite persistence and schema migration, append-only events, attempts, and checkpoints.
- Pass B added a replaceable deterministic planner, dependency-aware scheduling, pause/resume/cancel at step boundaries, bounded retries, restart recovery, reconciliation for uncertain non-idempotent outcomes, and stable idempotency-key safeguards.
- File-backed recovery tests simulate an effect occurring before success is durably recorded and confirm that an uncertain non-idempotent step is not automatically replayed. Completed steps are not silently re-executed during recovery.
- Antigravity's runtime review verdict was **PASS WITH FINDINGS**, with no blocking findings. Its follow-up review of M-1 returned **PASS**. M-1 was resolved by normalizing malformed persisted JSON and `StepResult` validation failures to a generic `TaskStoreError`; tests cover both corrupt forms and preserve not-found behavior.
- Owner-provided Python 3.12.4 acceptance results: focused Phase 03 tests, 38 passed; full suite, 101 passed with one existing Starlette/httpx deprecation warning. Ruff and `git diff --check` passed.
- No claim is made that SQLite guarantees exactly-once external effects, that scheduling works across multiple processes, or that JARVIS is production-ready. No real external tools were added. The current branch's Phase 03 work has not been committed or pushed.
