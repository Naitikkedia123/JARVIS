# Phase 14 — Specialist Agents (Only with Evidence)

Before adding specialist agents, compare current baseline against a clear evaluation set. Propose one A/B experiment addressing a demonstrated failure mode. Do not build a multi-agent system just because it looks advanced.

If approved, add one specialist behind existing task/tool/model interfaces, with bounded budget, explicit permissions, cancellation and supervisor verification. Specialist agents cannot grant permissions or mark tasks complete without verifier evidence. Parallel execution is only for independent idempotent tasks and must be resource-bounded.

Acceptance: benchmark shows measurable improvement without unacceptable latency, quota, resource, safety or regression penalty. Document failure as well as improvement; if no improvement is shown, do not ship.
