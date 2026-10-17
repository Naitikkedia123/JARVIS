# Phase 02 — Model Gateway and Provider Contracts

Read `AGENTS.md`, architecture, ADR 0001, status and existing interfaces/tests. Implement only the model gateway slice; do not implement planning, tools or a paid API.

Create a JARVIS-owned provider protocol and typed request/response/error contracts. Include a deterministic mock provider for tests, timeout/cancellation behavior, structured-output validation, capability declarations and a small router-policy interface. Track provider usage/quota as metadata only for now. Future credentials must come from environment/config, never source code/logs. Paid fallback must be impossible by default and fail closed when no free/authorized provider is available.

Before editing, list intended files. Avoid heavyweight frameworks. Tests must run without network, API keys or model weights. Acceptance: mock providers can be swapped without changing task/core interfaces; malformed output/unavailable provider returns typed error; no provider owns task state or permissions; checks pass; status updated. No commit/push.
