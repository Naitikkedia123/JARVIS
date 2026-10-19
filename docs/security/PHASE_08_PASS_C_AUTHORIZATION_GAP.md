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

No task submission or status route was added because the current interfaces
cannot prove authorization or associate tasks with their submitting device.
Adding routes around `TaskRuntime.run_task()` would bypass the required policy
contract. This is a deliberate fail-closed result under the Pass C security
requirements, not an implementation of task synchronization. Pass C remains
incomplete and requires the contract above before its requested API capability
can be built safely.

## Follow-up: task authorization foundations

The subsequent Phase 08 Task Authorization Foundation adds typed policy
decisions and device-bound task authorization contexts, runtime submission and
authorized-execution entry points, transactional task ownership and
idempotency storage, and owner-scoped task lookup. Identical retries by the
same device return the original plan; a changed request with the same key is a
conflict. These foundations do not make the earlier HTTP capability available.

Outstanding integration work remains: the API caller must authenticate an
active device through `DeviceService` for each request; the application owner
must inject a concrete, reviewed `TaskSubmissionPolicy`; and an authorized
runner must map each planned operation to a `ToolRequest` and pass the policy
context through `ToolDispatcher`. The repository still has no application
runner adapter or task HTTP routes. Future task status handlers must use the
owner-scoped store query. The primitives do not guarantee exactly-once task
execution or external effects.
