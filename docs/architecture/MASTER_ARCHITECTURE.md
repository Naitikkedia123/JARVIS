# JARVIS Master Architecture — Baseline

## Principle
JARVIS owns its control-plane semantics: goals, durable tasks, permissions, memory contracts, model-routing policy and completion verification. Models propose; policy code authorizes; tools execute; observations establish actual outcomes.

## Logical flow
```text
Windows UI / Android companion
             |
      Authenticated API
             |
  JARVIS control plane
  (goals, context, devices, policy, audit)
             |
      Intelligence gateway
  (adapters, routing, structured output, evaluation)
             |
      Durable agent runtime
  (planning, task graph, checkpoints, bounded recovery)
             |
      Typed tool registry
  (browser, files, commands, Windows UI, integrations)
             |
   Observations -> Verifier -> Task result
             |
     Memory / project knowledge
```

## Starting deployment
A Python modular monolith on Windows, SQLite when persistence is added, FastAPI and a responsive local interface. Do not start with microservices, a large vector database, Docker agents, large local models or a phone emulator.

## Boundaries
- Provider adapters do not own task state or permissions.
- Model output does not grant tool permissions.
- Actions have typed inputs, explicit capabilities, limits and observable results.
- Local-first data; no remote model receives sensitive context unless explicitly authorized.
- No paid fallback; quotas must fail closed.
- Untrusted web pages/files/messages cannot override system policy.
- Cellular call control/audio is a tested feasibility question, not an assumed Android capability.

## Accepted architecture direction
Phase 00 is accepted. Option B is the approved direction: JARVIS owns its control plane, including task state, permissions, model-routing policy, memory contracts and verification. OpenClaw remains a potential external integration behind a selective adapter; it is not the authoritative JARVIS control plane. Recheck fit, license, security and maintenance before reusing or embedding any external component. See [ADR 0001](../decisions/0001-architecture-choice.md) for the decision and trade-offs.

## Phase 08 task authorization foundations

- `TaskSubmissionPolicy` is an injected trusted contract. It receives an authenticated device ID and a validated task proposal, and must return an explicit allow decision with a device-bound `AuthorizationContext`. The task layer does not infer grants from a device role or self-reported capabilities. A missing policy, denied decision, mismatched principal, revoked device record or policy exception fails closed.
- `TaskRuntime.submit_device_task()` evaluates that policy before creating a plan. `TaskStore` persists the goal, task plan, device owner, idempotency key and request digest in one SQLite transaction. A retry with the same device, key and request digest returns the original plan; reusing that device's key with different content returns a conflict. This guarantees idempotent task creation only; it does not guarantee exactly-once dispatch or external effects.
- Owned tasks cannot be run or controlled through the legacy `run_task()`, `pause_task()`, `resume_task()`, `cancel_task()` or `reconcile_step()` entry points. `run_authorized_task()` checks task ownership, re-evaluates the current policy, and passes the resulting device-bound authorization context to an explicitly authorized runner. Device-scoped status reads use `TaskStore.get_task_plan_for_device()` (or `get_task_for_device()` for the task record); unknown and other-device task IDs return the same not-found result.
- The authorized runner contract requires implementations to pass the supplied tool authorization context unchanged to the existing `ToolDispatcher`. The dispatcher remains responsible for per-tool capability checks, auditing and approval requests. If an approval-required tool has no approval service, dispatch fails before executor invocation. No runner adapter currently maps task steps into tool requests, and no concrete task policy, API route, or default policy is wired into application startup.
- The current task state machine has no approval-waiting or approved-resume state. A future runner must not treat a pending approval as successful or replay an action; mapping approval outcomes into durable task state and a trusted decision flow remains unresolved.
- The current task state machine has no approval-waiting or approved-resume state. A future runner must not treat a pending approval as successful or replay an action; mapping approval outcomes into durable task state and a trusted decision flow remains unresolved.
- Device authentication remains the responsibility of the existing `DeviceService` at a trusted caller boundary. The task runtime does not authenticate bearer tokens. Future API code must authenticate an active device on every request, inject an explicit policy, keep task IDs scoped to the authenticated device, and use `run_authorized_task()` for owned work. No task HTTP endpoints, Android client, event stream, offline queue or listener change is part of these foundations; the server remains loopback-only.
