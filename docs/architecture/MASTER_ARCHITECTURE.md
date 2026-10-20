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
- Owned tasks cannot be run or controlled through the legacy `run_task()`, `pause_task()`, `resume_task()`, `cancel_task()` or `reconcile_step()` entry points. The object-based `run_authorized_task()` compatibility method now fails closed. Internal execution uses `run_authorized_task_async()` with a fresh bearer credential, the existing `DeviceService.authenticate()` check, durable ownership comparison, and the persisted typed action. Device-scoped status reads use `TaskStore.get_task_plan_for_device()` (or `get_task_for_device()` for the task record); unknown and other-device task IDs return the same not-found result.
- `LocalDeviceTaskGrantPolicy` reads the local operator's versioned JSON allowlist on every decision. `Settings.device_task_grants_path` defaults to `data/local/device-task-grants.json`; a missing file, empty ID list, malformed or unsupported content, duplicate or invalid IDs, and read failures deny. No device IDs are included by default. The policy issues only `system:status:read` to an exact listed device for the empty-argument `jarvis.status` action; its decision ID includes a deterministic revision of the validated grant set. JARVIS does not currently record grant-file edits in a durable audit log; those changes remain under local operator/configuration management. `DispatcherBackedTaskRunner` is implemented as a class, and `TaskRuntime.run_authorized_task_async()` supports it when supplied with the required dependencies. The runner reauthenticates and reauthorizes, then calls the existing asynchronous `ToolDispatcher`, which retains policy, validation, approval and audit checks. The dispatcher must use the exact `DeviceTaskToolPolicy` instance type bound to its own registry. Request text, model output, memory, device role, and reported capabilities never select a tool or create a grant.
- An owned attempt and its action digest, authenticated device ID, and authorization fingerprint are persisted in SQLite. A transactional dispatch claim reduces duplicate dispatch for the same attempt. Dispatcher audit events carry the same typed correlation fields, but `InMemoryToolAuditSink` events are not durable across process restarts. An interrupted or post-dispatch uncertain attempt reaches reconciliation and must not be blindly retried; SQLite does not provide exactly-once external execution.
- The `main.py` lifespan opens the separate file-backed `TaskStore` at `Settings.task_database_path`, defaulting to `data/local/tasks.sqlite3`, and privately stores task application services after successful initialization. When a `DeviceService` exists, it composes `TaskRuntime`, separate submission and dispatch policies, `DispatcherBackedTaskRunner`, and a `ToolDispatcher` whose dedicated registry contains only `jarvis.status`, guarded by `DeviceTaskToolPolicy`. If no `DeviceService` exists, the authorized runner is absent. The task store is closed during lifespan teardown; SQLite access remains on its creating thread. The runtime's legacy runner is optional, and legacy execution fails before task state or attempts change when it is absent.
- Submission and dispatch use independent operator-managed policies. The submission policy reads `Settings.task_submission_grants_path`, defaulting to `data/local/task-submission-grants.json`; its strict version-1 allowlist has no device IDs by default and permits only empty-argument `jarvis.status`. Every submission, including an idempotent replay, requires a fresh valid bearer and current policy authorization. A new task returns 201; same-device/key/content replay returns 200; changed content under that key returns 409. `Location` and `Cache-Control: no-store` are set. Missing, empty, malformed, duplicate, unsupported, or unreadable policy data produces generic 503; a valid but nonmatching device/action is denied. Submission writes only the task plan and does not invoke execution. Its policy revision is not persisted as task provenance.
- The app registers `GET /health`, `GET /ready`, device routes `POST /devices/pair`, `GET /devices/me`, `PATCH /devices/me/capabilities`, `POST /devices/pairing-codes`, `GET /devices`, and `DELETE /devices/{device_id}`, plus `POST /tasks` and owner-scoped `GET /tasks/{task_id}`. The task status route authenticates the active device on each request and uses the task ID plus authenticated device ID in the owner-scoped lookup; it does not require a task grant. No task execution, grant-management, or background-execution route exists. Production task execution remains gated for a separately reviewed milestone.

- The owner-scoped task status JSON exposes `task_id`, canonical task `status`, the established compatibility field `state` (same persisted `TaskState` value), timestamps, progress, task-level `result`, and ordered `steps`. `progress.completed_steps` counts persisted succeeded steps and `progress.total_steps` is the persisted plan size. Task-level `result` is the validated `StatusOutput` only for a single-step task whose persisted action is exactly empty-argument `jarvis.status`; it is `null` for multi-step tasks, so no ambiguous aggregation is implied. Each step reports its persisted `state`, dependency IDs, attempt count/limit, bounded attempt history (number, outcome, safe error code, start/finish timestamps), latest-attempt summary, timestamps, and its own permitted result under the same exact-action and `StatusOutput` validation rule. Reconciliation is visible through persisted task/step states and `outcome_unknown` attempt records. Attempt IDs, action arguments, authorization bindings, digests, and checkpoint internals are not exposed.
- The current task state machine has no approval-waiting state. `ApprovalRequestService` stores only in-memory pending metadata and exposes no trusted decision operation or post-approval execution. The task runtime does not atomically persist approval references with a waiting task state. It classifies `ToolApprovalRequiredError` as a known permanent pre-execution failure because the dispatcher raises it before invoking the executor; this does not create a resumable task. No approval resume path is safe until durable waiting, trusted decision provenance, ownership checks, policy reauthorization, and single-use execution semantics are designed together.
- The task API has read-only status and narrow task submission routes, but no public execution route, grant-management workflow, or background worker. Every direct internal execution call must present a fresh bearer credential and pass current grant and ownership checks; stored ownership alone cannot resume execution. Revocation is checked again immediately before dispatch, but the separate device-store transaction and dispatch cannot make revocation atomic with tool execution. Local grant-file edits have no durable rollback history or JARVIS audit record. Submission policy provenance is not persisted. Dispatcher audit events remain in-memory, and uncertain outcomes must not be blindly retried. No Android client, event stream, offline queue or listener change is part of this work; the server remains loopback-only.
