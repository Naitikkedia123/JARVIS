# Gateway Validation Report

**Validation target:** `main` at `9182bc4` (`merge: Ollama thinking option fix`)

**Environment:** Windows, Python 3.12.4, local Ollama at `http://127.0.0.1:11434`, model `qwen3.5:2b`

This report records the local validation results for the current gateway implementation. Live Ollama observations and automated test evidence are listed separately.

## Live local observations

- **Local adapter:** returned `JARVIS ADAPTER WORKS`; Ollama reported 23 input tokens, 7 output tokens, and approximately 14.06 seconds.
- **Gateway routing:** with provider `ollama_local` explicitly selected, returned model `qwen3.5:2b` and `JARVIS GATEWAY ROUTING WORKS`; Ollama reported 26 input tokens, 10 output tokens, and approximately 17.59 seconds.
- **Fail-closed routing:** a request without provider selection raised `NoProviderSelectedError`. No implicit provider fallback occurred.
- **Latency sample:** the same prompt was run three times through the gateway: 7.96, 2.06, and 1.69 seconds. Average: 3.90 seconds; minimum: 1.69 seconds; maximum: 7.96 seconds. This is a small preliminary sample, not a rigorous benchmark.

## Automated test evidence

- The gateway test suites passed: 49 tests in 0.78 seconds.
- The full test suite passed: 63 tests, with one existing Starlette/httpx deprecation warning.
- Ruff and `git diff --check` passed.
- Existing tests cover timeouts, provider errors and unavailability, safe error messages, structured-output validation, and no retry or fallback.

To reproduce the checks in PowerShell from the repository root with `.venv-py312`:

```powershell
.\.venv-py312\Scripts\python.exe -m pytest -q tests/unit/test_gateway.py tests/unit/test_gateway_local.py
.\.venv-py312\Scripts\python.exe -m pytest -q
.\.venv-py312\Scripts\python.exe -m ruff check .
git diff --check
```

## Scope and limitations

These results validate the reported Windows local setup only. They do not establish that every failure mode was exercised against a live Ollama server, that the latency sample represents general performance, or that a production JARVIS application is fully integrated. The gateway remains a provider and routing component; these observations are limited to the tested adapter and gateway paths.
