# Threat Model

## Assets
Private files, source code, account credentials, model credentials (if later added), conversations, memory, task artifacts, contact information, phone notifications and permission settings.

## Threats
- Prompt injection in websites, documents, email and tool output.
- Accidental deletion/modification by generated commands.
- Credential leakage into Git, logs, screenshots or agent prompts.
- Unauthorized message/email sending.
- Overbroad Windows/mobile permissions.
- Unexpected model API/cloud charges.
- False claims that a task completed.
- Duplicate external actions after retry/restart.
- Background work/recording without consent.

## Initial controls
- Private GitHub repository; `.env` and local data ignored.
- No secrets in code or coding-agent prompts.
- Bind development server to `127.0.0.1`, not `0.0.0.0`.
- Typed tool allowlists; no arbitrary shell by default.
- Policy is separate from model reasoning.
- Approval for high-impact/irreversible operations.
- Timeouts, bounded retries, idempotency where possible, and audit events.
- External content is untrusted.
- No paid fallbacks; no billing-enabled API keys for initial build.
- Stop/cancel controls and explicit device capability status.
- Verify actual Android/telephony support before claiming call handling.

## Phase 04 Pass A: tool boundary

- Tool definitions are registered by trusted application code and declare typed input and output schemas, required capabilities, read/write classification, bounded timeout, maximum result size, and audit fields.
- Registry registration is a trusted application initialization operation; constructing a dispatcher seals the registry, after which its registration API rejects changes while lookups remain available. Model-generated requests and untrusted tool content cannot register or replace definitions. This lifecycle does not prevent arbitrary trusted-process code from mutating private internals directly.
- Tool requests and arguments remain untrusted proposals. Request data cannot supply a principal, capabilities, policy decision, or execution policy.
- Authorization is evaluated separately using a caller-provided trusted context. Missing context or required capabilities denies dispatch by default. WRITE definitions also require the dedicated `tool:write` capability; READ definitions do not.
- Dispatch emits structured events for authorization decisions, execution start, success, validation failures, timeouts, and execution failures. Events contain timestamps, tool/principal identity when available, access class, stable reason codes, and duration where applicable. Raw arguments, results, credentials, and exception text are excluded.
- Audit sink failures fail closed before executor invocation. A denied request remains denied even if its denial event cannot be recorded. A sink failure after execution is surfaced as a typed audit error; it cannot cause an automatic retry in this dispatcher.
- The provided in-memory sink is not durable; its events are lost when the process exits. No durable audit storage is implemented.
- Pass B1 adds a local status tool and workspace list/read/create tools. The status result reports only that the local control-plane tool responded and explicitly says external services were not checked; it makes no network requests.
- Workspace tools require an absolute root injected by trusted initialization. Requests cannot select the root. Listing is nonrecursive, examines at most 10,000 directory entries, returns at most 100 sorted regular file names, and omits symlink entries. Reads accept strict UTF-8 regular files up to 65,536 bytes. Creation accepts UTF-8 content up to 32,768 bytes and uses exclusive creation; existing files are never overwritten and parent directories are not created.
- Workspace paths reject absolute, drive-qualified, UNC, traversal and redirected components; returned paths are workspace-relative and filesystem errors are normalized by the dispatcher. Initialization checks the supplied root itself before resolving it, rejects symbolic links and Windows reparse points, and compares the original and canonical directory identities before retaining the canonical root. Operations also recheck that the retained root still has the same filesystem identity. These checks detect ordinary root substitution and many initialization races, but they do not bind later path-based opens to a directory handle. A hostile local process may still replace workspace entries between a check and a read or create operation; such concurrent filesystem replacement is not eliminated by this implementation. The configured workspace must be trusted against concurrent local mutation. Workspace symlink tests were skipped in the current Windows environment because its policy prevented symlink creation; these symlink scenarios were not dynamically tested.
- File contents are returned as untrusted data and do not grant permissions or authorize further operations. Workspace creation requires both `workspace:write` and `tool:write`.
- Higher-risk tool definitions may require approval and supply a static safe summary. After input validation and authorization, the dispatcher creates a pending request from that metadata and the trusted principal, records an approval-requested audit event, and returns a typed approval-required error without invoking the executor. Requests contain no raw arguments. If recording the audit event fails after request creation, the caller can receive an error while the pending request remains in memory; the tool executor is not invoked. The in-memory request service is non-durable and loses requests on process restart. No decision is accepted, request IDs do not authorize execution, and no post-approval execution path exists.
- These tools are only registered through trusted application initialization and are not wired into production routes. No arbitrary shell, network or personal-account tools, external side effects, approval UI, trusted decision channel, persistent approval storage, expiry workflow, or execution-after-approval workflow is implemented. Pass B2 does not establish production readiness or complete Phase 04.

Update this document whenever an integration or permission is introduced.
