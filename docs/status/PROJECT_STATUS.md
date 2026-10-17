# JARVIS Project Status

**Last updated:** Phase 01 foundation implementation — 17 October 2026
**Current stage:** Phase 01 — Foundation implementation; acceptance pending Python 3.12 setup verification and CI execution
**Current branch:** `phase/01-foundation`
**Status:** The foundation slice is implemented. Local Ruff/tests and loopback endpoint checks passed under Python 3.13. The existing `.venv` uses Python 3.13.1. Python 3.12 availability is unresolved: one agent session's launcher reported only Python 3.13, while another reported Python 3.12.4 at `C:\Users\user\AppData\Local\Programs\Python\Python312\python.exe`. This discrepancy remains unresolved until checked in the user's actual PowerShell session. A clean Python 3.12 setup is unverified, and CI has not been run remotely.

## Phase 00 completed work (2026-10-17)
- Prepared `docs/decisions/0001-architecture-choice.md` with a weighted A/B/C scorecard (weights total 100%), evidence, trade-offs, and reversal criteria. Recommendation: JARVIS-owned Python control plane with selective adapters; evaluate OpenClaw as an external local Gateway before considering a fork.
- Prepared `docs/research/FEASIBILITY_REPORT.md` using current official project repositories and documentation for OpenClaw, Open Interpreter, OpenHands SDK, Letta Code, LangGraph, Android, Windows UI Automation, WireGuard, Codex/Go, and Antigravity. It distinguishes verified facts, estimates, assumptions, and unresolved account/device questions; links sources and records release dates where available.
- Confirmed in the documented source snapshot that Codex is included with ChatGPT Go while Codex Cloud is not; account-specific quota was not inspected. Antigravity overage can be set to “Never” where the control is available; the user's account panel was not inspected. No fixed agent task quotas were inferred.
- No packages were installed; no model weights were downloaded; no personal account/device was accessed; no personal integrations, cloud resources, paid services, or runtime features were configured or implemented.
- The project owner reviewed and accepted Phase 00 and Option B: a JARVIS-owned Python control plane with selective adapters.
- Phase 01 — Foundation was approved after Phase 00; implementation is underway on `phase/01-foundation`.

## Phase 01 implementation (2026-10-17)
- Added typed environment settings with safe defaults; a configured host other than `127.0.0.1` is rejected. The local runner continues to bind to `127.0.0.1:8000`.
- Added structured JSON logging with redaction for password, token, API-key, authorization, cookie and credential fields/values. Uvicorn access logging is disabled to avoid request-target leaks, and Uvicorn error records use the structured formatter without exception messages or tracebacks. Error handlers return standardized generic responses and do not expose validation inputs, exception messages, stack traces or settings.
- Preserved `/health` exactly as `{"status":"ok","service":"jarvis"}` and added deterministic `/ready` returning `{"status":"ready","service":"jarvis"}`. Readiness has no external dependency checks because none exist in this foundation.
- Regression coverage includes health/readiness, loopback setting rejection, secret redaction, disabled Uvicorn access logs, and generic HTTP, validation and internal-error responses.
- `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\check.ps1` — passed under Python 3.13: Ruff passed; pytest reported 12 passed, 1 third-party Starlette/httpx deprecation warning; `git diff --check` passed (Git emitted LF-to-CRLF conversion warnings).
- `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-local.ps1` — started successfully. `/health` and `/ready` returned the payloads above. `netstat -ano -p tcp` showed the listener only at `127.0.0.1:8000`; the test server was stopped afterward.
- `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup-dev.ps1` — historical attempt did not complete: that agent session's `py -0p` showed Python 3.13 only and `py -3.12` could not select a runtime. This does not establish that Python 3.12 is absent machine-wide. A Python 3.13 `.venv` was created and existing declared dependencies were installed to run the checks; no dependency declarations changed. The clean Python 3.12 setup remains unverified.
- CI workflow inspection confirms Python 3.12 setup, Ruff and pytest steps with no secrets. A remote CI run has not been performed.

## Verified in scaffold
- FastAPI system endpoints exist at `/health` and `/ready`.
- Automated endpoint and foundation security/error tests exist.
- Setup, check and run scripts are provided; setup remains unverified on Python 3.12 in this environment.

## Not implemented yet
- Real model providers or routing.
- Goal planning and durable task state.
- Tool registry or execution tools.
- Persistent memory.
- Windows automation, Android, communication integrations, voice or vision.

## Next task
Verify clean setup under Python 3.12 and obtain a CI run before marking Phase 01 accepted.

## Current checks
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
- Python 3.12 setup, execution of the scripts against a valid 3.12 venv, and remote CI remain unverified. The machine-wide Python 3.12 discrepancy remains unresolved pending a check in the user's actual PowerShell session.

## Real Uvicorn exception-log verification (2026-10-17)
- Added a real loopback server regression test using an ephemeral bound socket and an HTTP request to a test-only failing endpoint. It verifies the generic 500 response, captures an actual `uvicorn.error` record, and confirms the fake secret, raw exception message, and traceback are absent. The server thread and socket are shut down in `finally` cleanup.
- The new test passed under the existing Python 3.13.1 `.venv`; the full suite reported 14 passed with the existing Starlette/httpx deprecation warning. Ruff passed during this verification.
- The owner reports earlier Python 3.12.4 checks and a local server run using `.venv-py312`. This execution context could not independently repeat them: `.venv-py312\pyvenv.cfg` records Python 3.12.4 and a base path under `C:\Users\user\AppData\Local\Programs\Python\Python312`, but invoking its `python.exe` reports that the recorded base interpreter path is unavailable here.
- Requested command `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\check.ps1 -VenvPath ".venv-py312"` exited 1 before Ruff or pytest because that interpreter could not launch. No environment was installed, upgraded, deleted, or recreated. Remote CI remains unverified.
