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
- `run_authorized_task()` currently re-evaluates the injected task policy and hands its context to the `AuthorizedStepRunner` protocol. The protocol documents dispatcher use but the runtime cannot verify it, so it is not an end-to-end enforcement boundary. The planner emits a generic text step without a tool name or typed arguments; mapping its text into tool requests would let task content select actions. No concrete task policy or policy configuration is wired into application startup.
- The task runtime is synchronous while `ToolDispatcher.dispatch()` is asynchronous. There is no established safe adapter contract for invoking it from both synchronous callers and an async API request. A future integration needs an explicit execution model and a typed, trusted step-to-tool intent format; request text, model output, memory and device metadata must never select capabilities.
- The current task state machine has no approval-waiting state. `ApprovalRequestService` stores only in-memory pending metadata and exposes no trusted decision operation or post-approval execution. The task runtime does not atomically persist approval references with a waiting task state. Approval-required dispatch raises an exception which the generic runtime currently classifies as an uncertain step outcome. No approval resume path is safe until durable waiting, trusted decision provenance, ownership checks, policy reauthorization, and single-use execution semantics are designed together.
- No authorized dispatcher runner, concrete task policy, task API route, or default policy is wired into application startup. Device-owned execution must not be exposed by an application integration; do not treat the protocol or pending approval record as proof of end-to-end authorized execution.
- Device authentication remains the responsibility of the existing `DeviceService` at a trusted caller boundary. The task runtime does not authenticate bearer tokens. Future API code must authenticate an active device on every request, inject an explicit policy, keep task IDs scoped to the authenticated device, and use `run_authorized_task()` for owned work. No task HTTP endpoints, Android client, event stream, offline queue or listener change is part of these foundations; the server remains loopback-only.
