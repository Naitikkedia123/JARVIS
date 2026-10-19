# Phase 08 Pass C — Task Synchronization Authorization Gap

## Initial Pass C inspection result

Pass C task submission and status retrieval cannot be enabled safely with the
current task-runtime and API contracts. No task synchronization endpoint or
task service is wired into `create_app`.

The existing `TaskRuntime` accepts a task identifier for execution and exposes
goal-based planning, but it has no authenticated principal, device identity,
authorization context, or policy decision input. Its `StepRunner` receives a
step and optional idempotency key only. The API's `DeviceService` authenticates
device credentials and distinguishes owner and companion roles, but those
facts do not identify which task operations a device may perform. Reported
capabilities are explicitly descriptive.

The task store has no task owner/device association or idempotency key for a
submission. Consequently, status access cannot be scoped to the submitting
device, and a retry cannot be safely associated with its first submission.
The deterministic planner currently emits a generic one-step plan; it does
not translate untrusted companion input into a policy-checked tool request.
The existing tool dispatcher enforces policy and approval for tool requests,
but the task runtime and application are not connected to that boundary.

The repository contains the Pass A and Pass B prompts, but no separate device
API specification. The Pass B prompt specifies pairing and device routes only.

## Required contract before enabling task routes

1. A trusted task-submission authorization contract must accept the
   authenticated device identity and validated proposal, then return a
   trusted authorization context or a denial. Device role and reported
   capabilities must not be converted directly into grants.
2. Task planning and dispatch must carry that trusted context through the
   existing tool policy, approval, and dispatcher boundaries. Missing context,
   policy errors, and unavailable approval handling must fail closed.
3. Durable task ownership and submission idempotency must be recorded
   transactionally with task creation. Status reads must query by both task ID
   and authorized device identity and return the same not-found response for
   unknown and unauthorized tasks.
4. The application needs explicitly injected, lifecycle-managed task runtime
   and policy dependencies. Defaults must leave task routes unavailable until
   those trusted dependencies are configured; no permissive fallback is
   acceptable.
5. Tests must exercise the real runtime-to-policy-to-dispatch path, including
   denied submissions, approval-required tools, per-device status isolation,
   duplicate idempotency keys, persistence failures, and safe HTTP errors.

## Pass C disposition

No task submission route was added because the submission policy is not yet
composed for the HTTP API. A read-only `GET /tasks/{task_id}` endpoint now
authenticates the active device on each request and scopes lookup by task ID
and authenticated device ID. Status retrieval does not require
`system:status:read`; that grant controls task-tool execution. The route cannot
submit or execute a task.
Adding routes around `TaskRuntime.run_task()` would bypass the required policy
contract. This is a deliberate fail-closed result under the Pass C security
requirements, not an implementation of task synchronization. Pass C remains
incomplete; task submission and execution still require the reviewed
authorization and lifecycle contracts above before those API capabilities can
be built safely.

## Follow-up: task authorization foundations

The subsequent Phase 08 Task Authorization Foundation adds typed policy
decisions and device-bound task authorization contexts, runtime submission and
authorized-execution entry points, transactional task ownership and
idempotency storage, and owner-scoped task lookup. Identical retries by the
same device return the original plan; a changed request with the same key is a
conflict. These foundations do not make the earlier HTTP capability available.

At that stage, outstanding integration work remained: the API caller needed
per-request device authentication, a reviewed `TaskSubmissionPolicy`, and a
runner mapping typed actions through `ToolDispatcher`. The subsequent internal
runner, grant policy, application-lifespan composition, and read-only status
route are recorded in the current implementation section below. The primitives
do not guarantee exactly-once task execution or external effects.

## Pass C authorized-execution review (2026-10-19)

At the time of this review, the authorization foundation could not be
connected safely to tool execution without additional architecture decisions.
The findings below record that earlier state; subsequent internal foundations
and the current production blockers are clarified after the review. No task
endpoints were added.

1. **No typed planned action:** `Step` contains a title, dependency IDs and
   replay metadata, while `DeterministicPlanner` emits a generic text step.
   There is no trusted structured intent naming a tool and validated
   arguments. Deriving tool names or arguments from task text, model output,
   memory or reported device capabilities would let untrusted content select
   actions.
2. **No explicit grant policy:** `TaskSubmissionPolicy` is only an injected
   protocol. Application startup has no reviewed per-device policy source or
   approved narrow tool/capability set. Role and self-reported capability
   fields remain descriptive; no permissive default was introduced.
3. **No enforceable dispatcher integration:** `AuthorizedStepRunner` is a
   caller-implemented protocol. `TaskRuntime` passes it an authorization
   context but cannot establish that it invokes `ToolDispatcher`. In addition,
   task runtime calls are synchronous and `ToolDispatcher.dispatch()` is
   asynchronous; there is no established invocation model safe for both
   synchronous execution and a future async API.
4. **No safe approval lifecycle:** approval requests are volatile in-memory
   pending records. The service has no trusted decision operation, expiry or
   single-use consumption; the task store has no approval-waiting state or
   atomic approval/task linkage. A dispatcher approval exception currently
   follows the generic uncertain-outcome path. A client-provided “approved”
   claim or retry cannot safely resume the task.

The existing foundations continue to reject missing/denied/mismatched policy
contexts, reject legacy lifecycle calls for owned tasks, preserve owner-scoped
status queries and create task ownership/idempotency records transactionally.
Existing tests cover those foundations, dispatcher denial, unavailable
approval handling, pre-execution audit failure, immutable authorization
contexts, and concurrent same-device submission using two SQLite connections
synchronized before the transactional insert. They do not prove an
approval wait/resume flow. The application must not expose device-owned
execution until the remaining production blockers are resolved. Status
handlers must continue to query by both task ID and authenticated device ID
when a later pass adds them.

## Source correction: internal authorized execution foundations

Subsequent Milestone 2A/2B changes add persisted typed actions, an implemented
`DispatcherBackedTaskRunner` class, and `TaskRuntime.run_authorized_task_async()`
with injectable dependencies. The internal runner reauthenticates the bearer
credential, checks persisted ownership and action identity, and sends the
request through `ToolDispatcher`, preserving its policy, validation, approval,
and audit path. The action and attempt authorization binding are persisted in
SQLite; dispatch audit events still use `InMemoryToolAuditSink` and are lost on
process restart. The transactional dispatch claim reduces duplicate dispatch
for one attempt, but does not guarantee exactly-once external effects. A
post-dispatch or interrupted uncertain outcome must not be blindly retried.

`LocalDeviceTaskGrantPolicy` now reads a strict version-1 JSON allowlist from
the local operator-controlled `Settings.device_task_grants_path` on each
authorization decision. The default path is `data/local/device-task-grants.json`;
no device IDs are preconfigured. Missing, empty, malformed, duplicate,
unsupported, or unreadable grant data denies. The policy issues only the exact
`system:status:read` capability for an allowlisted device's empty-argument
`jarvis.status` action. A deterministic revision of the validated grant set
forms part of the decision ID, which the existing resolver binds to device and
action in its authorization fingerprint. A completed file update is observed
on the next check; a prior decision is not cached. JARVIS does not maintain a
durable audit log of local grant-file edits, and restoring an older valid file
is treated as the operator's current configuration.

The FastAPI lifespan now opens the separate `TaskStore` configured by
`Settings.task_database_path` (`data/local/tasks.sqlite3` by default) and
privately composes `TaskRuntime` and, only when a trusted `DeviceService` is
available, `DispatcherBackedTaskRunner`. That runner receives
`LocalDeviceTaskGrantPolicy` from the server-controlled
`Settings.device_task_grants_path` and a `ToolDispatcher` using a dedicated
registry containing only `jarvis.status`, guarded by `DeviceTaskToolPolicy`.
The application publishes this bundle only in private lifespan state after
successful construction, and closes the task store at shutdown. If the device
service is absent, no authorized runner is constructed. A missing, empty,
malformed, invalid, or unreadable grant file still denies all devices; no IDs
are preconfigured. A grant applies only to a fresh authenticated device whose
persisted task ownership and typed action match, and dispatcher policy,
validation, and audit checks remain mandatory.

The app registers `/health`, `/ready`, device pairing, identity,
capability-reporting, pairing-code, listing, and revocation routes under
`/devices`, and the owner-scoped read-only `GET /tasks/{task_id}` route. Each
status request requires fresh bearer authentication and an owner-scoped task
lookup; this read does not require the dispatch capability grant. No task
submission, execution, or grant-management route exists, and no background
execution or automatic resume path exists. Internal composition and status
retrieval do not make public execution available. The local JSON file has
no independent rollback history or durable grant-change audit; restoring
identical old content restores the same deterministic policy revision. The
device and task stores use separate SQLite transactions, so revocation can
still race after final authentication and before dispatch.

Approval still has no durable waiting state or trusted resume path, so
approval-required tools remain unavailable for task execution. Ownership-scoped
status queries are available internally, but no status handler exposes them.
Dispatcher audit events use the non-durable `InMemoryToolAuditSink`; attempt,
action, device, and authorization bindings are persisted separately in the
task database. Dispatch claims reduce duplicate dispatch for one attempt but
do not guarantee exactly-once external effects. Uncertain outcomes must not be
blindly retried. Pass C remains incomplete pending separate review and
approval of any public task-execution entry point.

## Milestone 2E-B — narrow task submission API (2026-10-19)

The owner accepted the read-only status API in Milestone 2E-A. Milestone 2E-B
adds `POST /tasks` for a narrow, non-executing submission flow. The route
requires a fresh device bearer, a validated typed proposal, and a separate
`LocalTaskSubmissionPolicy` decision on every request, including retries. It
does not use the dispatch grant as a submission grant. The submission policy
reads the operator-managed strict version-1 allowlist at
`Settings.task_submission_grants_path` (default
`data/local/task-submission-grants.json`); no devices are configured by
default. It permits only listed devices submitting exactly empty-argument
`jarvis.status`. Missing, empty, malformed, duplicate-key, duplicate-ID,
unsupported, oversized, or unreadable policy data returns a generic service
unavailable response. A valid policy that does not authorize the device/action
denies without persisting a task.

The route delegates persistence to the existing `TaskRuntime.submit_device_task`
and `TaskStore` idempotency transaction, then performs an owner-scoped read for
the response. New plans return 201; identical retries for the same device/key
return 200 with the original task ID and current persisted state; an authorized
request with changed title/description under the same key returns 409. The
response includes `Location` and `Cache-Control: no-store`. The request body is
bounded to 16 KiB and preflighted for duplicate JSON keys, nesting depth and
structural complexity before Pydantic validation. Submission persists a ready
plan and does not run the planner's result through an execution runner,
dispatcher, approval service, or background worker. Regression tests patch the
runtime and dispatcher execution entry points to fail if submission invokes
them.

The single-action policy means a changed or unsupported action is rejected by
policy before idempotency comparison; it therefore receives a generic denial,
not a 409. Returning 409 for a changed action would require comparing content
before policy authorization, which would change the approved authorization
ordering. The owner confirmed that authorization denial takes priority. The
submission policy's decision/revision is not persisted as task provenance, and
local policy changes have no durable audit or rollback history. No public task execution route,
approval wait/resume lifecycle, grant-management route, or background worker
was added; Pass C execution remains gated.
