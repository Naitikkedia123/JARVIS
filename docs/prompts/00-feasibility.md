# Phase 00 — Feasibility and Architecture Decision (Codex is default implementer)

Read `AGENTS.md`, `docs/architecture/MASTER_ARCHITECTURE.md`, `docs/roadmap/MASTER_IMPLEMENTATION_PLAN.md`, `docs/research/REFERENCE_PROJECTS.md`, `docs/research/CODING_AGENT_OPTIONS.md`, and `docs/status/PROJECT_STATUS.md` first.

**Mode: research/documentation only.** Do not build runtime features, download model weights, install frameworks, configure personal integrations, create cloud resources, purchase credits, enable overage, or use billing-enabled APIs.

Compare (A) extending OpenClaw, (B) a JARVIS-owned Python control plane with selective OpenClaw/MCP adapters, and (C) building integrations from scratch. Inspect current official repositories/docs and actual licenses/security notes for OpenClaw, Open Interpreter, OpenHands SDK, Letta and LangGraph. Assess durable tasks, ownership of the control plane, Windows/Android fit, communications, security, model independence, memory provenance, maintenance, 8 GB RAM and zero recurring cost. Evaluate whether OpenClaw is easier to integrate as an external local gateway than to fork.

Verify using official docs, not assumptions:
- Current Codex eligibility and usage display for ChatGPT Go; note Codex Cloud is not included with Go.
- Current Antigravity IDE-extension plan/model/usage display. User says they have Google One/Google AI Plus; do not assume Google AI Pro entitlements or a fixed number of coding tasks. Explain how to ensure no paid AI credits/overage/auto-reload is activated.
- How to benchmark local inference without downloading any model yet.
- Windows UI Automation options.
- Android version/call roles/audio limitations that still require verification on the actual phone.
- Secure laptop-phone connectivity.

Required outputs:
1. `docs/decisions/0001-architecture-choice.md` — weighted scorecard, cited sources, decision, trade-offs and reversal criteria.
2. `docs/research/FEASIBILITY_REPORT.md` — findings, unverified assumptions and no more than 10 next actions.
3. Update `docs/status/PROJECT_STATUS.md` with exact completed work and next action.

Before edits, show a short plan and intended files. Do not modify other files. Label unverified facts; never invent benchmarks or agent quotas. Run `git diff --check`. Report changed files and verification results.

Use the local repo docs and official sources linked by them. Do not start Phase 01 or implement runtime features in this phase.
