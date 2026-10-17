# JARVIS — End-to-End Implementation Plan for Coding-Agent-Driven Development

**Version:** 1.1 • **Prepared:** 17 October 2026 • **Agent workflow updated:** Antigravity + Codex  
**Machine:** Windows laptop, Intel Core i5-1335U, 8 GB RAM, Intel integrated graphics, approximately 184 GB free disk  
**Editor:** Visual Studio Code  
**Cost rule:** No mandatory recurring software, hosting, or model/API cost. No silent paid fallback.  
**Method:** Coding agents write and test code; the owner manages branches, reviews diffs/results, and approves consequential decisions.

---

## 1. Executive decision

Do not ask a coding agent to “build all of JARVIS.” That creates an untestable amount of code, consumes quota on repeated broad-context reasoning, and makes failures difficult to isolate. Build a sequence of independently testable vertical slices. Each slice has a prompt, named Git branch, acceptance checklist, tests, and a human-readable report. One coding agent implements at a time. A second agent reviews milestones; it must not edit the same files concurrently.

### Recommended coding-agent workflow

- **Primary implementer: Codex VS Code extension**, signed in with the existing ChatGPT Go account, as previously selected. Codex is included with Go, but its allowance is account-specific and varies by task/model/context. Codex Cloud is not included with Go, so do not design the workflow around cloud execution. Check the available usage screen before major tasks. Source: https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan
- **Secondary reviewer and legitimate fallback: Google Antigravity VS Code extension**, signed in with the user's Google One / Google AI account. Antigravity has native VS Code agent support, plans, inline diffs and workspace instructions. Its model entitlements and quotas depend on the exact plan and model shown in the account, so do not assume a specific request count or Pro-only model on a Google AI Plus subscription. Sources: https://www.antigravity.google/docs/ide/extensions/ and https://www.antigravity.google/docs/plans
- **Quota fallback:** if Codex is exhausted, stop and checkpoint first. Antigravity may implement one bounded task after reviewing the current branch/worktree; Codex then reviews when available, or vice versa. Never let both agents edit the same task simultaneously. Do not purchase credits, enable overage, add billed APIs, or use account rotation to bypass limits.
- **Optional small-task fallback: GitHub Copilot Free.** Its plan page advertises 2,000 completions/month plus limited chat/agent use. Do not depend on the limited agent allowance for the main build. Source: https://github.com/features/copilot/plans
- **Do not budget for Claude Code at $0.** Anthropic documents Console authentication as requiring active billing and Claude-app authentication with Pro/Max. Ordinary Claude web chat may be useful for occasional manual review if available, but not as the required file-editing agent. Source: https://docs.anthropic.com/en/docs/claude-code/getting-started

Plan details are dynamic. Recheck official pages at each major phase. Never add billing details or a billable API key to unblock an agent. Do not rotate accounts to circumvent quotas.

---

## 2. What to download and install

| Item | Why | Official resource |
|---|---|---|
| Visual Studio Code Stable | One workspace for source, agents, terminal, diffs, tests | https://code.visualstudio.com/ |
| Git for Windows | Version history and branches | https://git-scm.com/download/win |
| GitHub Desktop | Easiest visual branch/commit/push workflow | https://desktop.github.com/ |
| Python 3.12 Windows x64 | Stable compatibility target for initial Python backend | https://www.python.org/downloads/windows/ — choose the 3.12 x64 installer and enable PATH |
| OpenAI Codex VS Code extension | Main coding agent | In VS Code Extensions search “Codex” and verify OpenAI publisher; sign in with your existing ChatGPT account |
| Google Antigravity VS Code extension | Plan-first agentic implementation/review; sign in with an entitled personal Google account | https://www.antigravity.google/docs/ide/extensions/ |

Also install Microsoft's **Python** and **Pylance** extensions in VS Code. Ruff is installed inside the project virtual environment.

### Installation order

1. Install VS Code, Git for Windows, GitHub Desktop and Python 3.12 x64.
2. Restart VS Code/PowerShell so PATH changes take effect.
3. Install Codex from VS Code Extensions and sign in with the ChatGPT account. Use local IDE/CLI; Go does not include Codex Cloud.
4. Install the official Google Antigravity extension from the VS Code Marketplace and sign in with the Google account holding your subscription.
5. Open Antigravity's plan/model/usage view and confirm the entitlement. The current plan name can be AI Plus or AI Pro; those are not interchangeable. Do not enable AI-credit purchases or auto-reload.
6. Verify `git --version` and `py -3.12 --version`. Node.js is not required for the first Python-only backend; install it only if a later justified tool requires it.

### Do not install yet

- **Docker Desktop/OpenHands local server:** defer; extra VM/container load can be heavy on an 8 GB laptop.
- **Android Studio/emulator:** defer until Android phase; test on the real phone over USB first.
- **Ollama or local model weights:** defer until benchmarking a specific model's RAM/latency. Do not download multi-GB weights casually.
- **Cloud database, paid API, paid hosting, subscriptions:** not required for initial development.
- **Second full IDE:** do not use Antigravity as another full-time IDE while VS Code is primary; consider only if a real quota/feature gap justifies it.

---

## 3. Free-plan capacity: how much can an agent write?

There is no honest universal estimate such as “the free plan writes X lines.” Plans meter different things: model usage, context, reasoning, tool loops, completions, or requests. A single repository-wide agent job can consume many internal turns. Codex Go and Antigravity allowances depend on account tier, model, context and task complexity. Inspect each product's current usage UI; neither usage allowance translates directly into a fixed number of coding jobs.

Use **task-size gates**, not line counts:

| Work item | Prompt size recommendation | Acceptance rule |
|---|---|---|
| Tiny | One fix, one test, or 1–3 related files | Show a test or concrete proof |
| Small | One cohesive feature, usually about 5–8 files maximum unless justified | Preserve interfaces and pass relevant tests |
| Medium | One subsystem with clear API contracts | Split into plan/schema, implementation, and review if context/diff grows |
| Large | Cross-cutting architecture or multiple subsystems | Never one prompt: use ADRs and several sequential slices |

File counts are workflow heuristics, not provider limits. If an agent expects to change many more files, ask it to stop and propose a split.

### Quota-conservation rules

1. Codex is the default implementer for the current slice. Antigravity is the read-only reviewer at milestones. Do not have both agents edit the same change in parallel.
2. If Codex is unavailable due to quota, save a checkpoint and allow Antigravity to implement only the next bounded task after inspecting branch/worktree status; switch review responsibility when quota permits.
3. Keep `AGENTS.md` short; point to docs instead of repeating the entire architecture in every prompt.
4. Give one specific phase prompt at a time: scope, expected files, tests, boundaries, acceptance criteria.
5. Do not request a full-repository summary every turn. Inspect relevant modules only.
6. Ask for a short plan, implement one slice, run checks and provide a concise report.
7. Use local `pytest`, Ruff and `git diff --check` as often as possible; these do not consume model quota.
8. One independent reviewer per milestone is enough. Avoid multi-agent critique loops.
9. When quota is exhausted, save `PROJECT_STATUS.md`, wait for reset or use another legitimately available free tool. Never activate paid overage or rotate accounts.
10. Do not let agents commit/push/merge, upload private files, add payment details, or use unapproved credentials unless separately authorized.

### Which agent does what?

- **Codex/ChatGPT Go:** default primary implementation and debugging; included usage is limited and Codex Cloud is not included on Go.
- **Google Antigravity extension:** read-only independent reviews and legitimate fallback implementation on a bounded task when Codex is quota-limited. Its model access depends on the actual Google AI plan shown in the account.
- **GitHub Copilot Free:** optional autocomplete/small assistance; not required.
- **Claude Code:** excluded from the mandatory $0 workflow.
- **OpenHands:** reference for coding-agent architecture/sandboxing; defer a local server until resource needs are clear.

---

## 4. Existing agents and reuse strategy

Phase 00 review is complete and Option B is accepted. These projects remain references or potential integration candidates. Before a later, authorized proof-of-fit, recheck the current architecture docs and actual licenses; do not clone every project or copy code just to “learn from it.”

| Project | Why it matters | Recommended use |
|---|---|---|
| [OpenClaw](https://github.com/openclaw/openclaw) | Close to a complete personal assistant: local Gateway/control plane, model adapters, channels, tools, device nodes | Potential external local Gateway behind a JARVIS-owned adapter. Check whether its channels/nodes fit JARVIS's permission boundary; read https://github.com/openclaw/openclaw/blob/main/SECURITY.md before enabling host tools. It is not the authoritative JARVIS control plane; do not blindly fork or grant broad permissions. |
| [Open Interpreter](https://github.com/openinterpreter/openinterpreter) | Computer-operation and tool execution patterns | Reference for computer-operation UX; avoid unrestricted execution. |
| [OpenHands SDK](https://github.com/OpenHands/software-agent-sdk) | Coding agents, workspace isolation, events and tool patterns | Potential specialized coding worker later; not required for Alpha. |
| [Letta](https://docs.letta.com/concepts/stateful-agents) | Stateful agents and memory | Compare memory ideas but keep JARVIS memory contracts independent. |
| [LangGraph durable execution](https://langchain-ai.github.io/langgraph/concepts/durable_execution/) | Checkpoints and recovery in multi-step workflows | Evaluate against a small custom task state machine; use only if it reduces work without taking over JARVIS semantics. |
| [Google Antigravity VS Code extension](https://www.antigravity.google/docs/ide/extensions/) | Agentic development with implementation plans, inline diffs and workspace rules | Secondary read-only reviewer and bounded fallback, separate from JARVIS runtime. |

### Accepted architecture decision

Phase 00 is complete and Option B is approved: JARVIS owns the control plane, with selective adapters for suitable components. OpenClaw may be evaluated as an external local Gateway behind a JARVIS-owned adapter; it does not own authoritative JARVIS task state, policy, memory contracts or verification. The A/B/C scorecard, evidence, trade-offs and reversal criteria are recorded in [ADR 0001](../decisions/0001-architecture-choice.md). Before any later reuse, recheck fit, license, security and maintenance; this decision does not approve embedding or forking external source.

---

## 5. Target architecture

```text
Windows Web/Desktop UI                 Android Companion (later)
           |                                    |
           +------------ Authenticated API -----+
                            |
                 JARVIS Control Plane
  +-------------------------+------------------------------+
  | Goal manager | Context manager | Task/event state      |
  | Permission policy | Device registry | Audit log         |
  +-------------------------+------------------------------+
                            |
                  Intelligence Gateway
  +-------------------------+------------------------------+
  | Provider adapters | model routing | output validation  |
  | evaluation | quota/privacy/latency policy                |
  +-------------------------+------------------------------+
                            |
                Durable Agent Runtime
  +-------------------------+------------------------------+
  | Planner | task graph | checkpoints | bounded recovery  |
  | tool registry | observation | completion verification  |
  +-------------------------+------------------------------+
                            |
      +---------------------+----------------------+
      |                     |                      |
 Browser/research     Windows operations     Integrations
 file/code tools      UI Automation          messages/email
      |                     |                 calling later
      +---------------------+----------------------+
                            |
                   Memory and Knowledge
    SQLite initially; provenance-aware records; full-text search;
       vector/graph/temporal storage only after measured need.
```

### Initial stack

- Python 3.12, FastAPI, Pydantic v2.
- SQLite initially; SQLAlchemy/Alembic when persistence warrants them.
- pytest, pytest-cov, Ruff; lightweight GitHub Actions CI.
- JARVIS-owned model-provider interface and typed tool registry.
- Playwright for browser automation when needed; Windows UI Automation for accessible desktop controls.
- Local-first storage. Hosted inference is optional, authorized, quota-aware and no-bill by default.
- Responsive web interface first; native Android companion later.
- Modular monolith first. No microservices, distributed queues, vector DB or specialist agents at the start.

### Security boundaries

Models propose; policy code authorizes; tools execute; observations provide evidence; verifier determines completion. Web pages, documents and messages are untrusted data. Never execute a shell command only because a model proposed it. Use least privilege, workspace restrictions, timeouts, logs, bounded retries and approval for high-impact actions. Keep secrets out of source, prompts, logs and Git. Do not allow self-modification of permission/security code without human review.

---
## 6. GitHub, branches and backups

### Repository visibility
Create a **private** GitHub repository initially. Consider public release only after checking for credentials, personal records, memory, logs and accidental local data. Actual `.env` files belong in `.gitignore`; `.env.example` contains placeholders only.

### Branch strategy
Keep it simple: `main` is the last known-good version; create one phase/feature branch at a time. Avoid a permanent `develop` branch. Every phase starts from latest `main`, is tested, then merged back.

Examples: `phase/00-feasibility`, `phase/01-foundation`, `feat/model-gateway`, `feat/durable-task-runtime`, `feat/memory-v1`, `feat/windows-node`, `feat/android-companion`, `feat/communication-integrations`, `fix/task-resume-after-restart`.

### Create the remote
1. Visit https://github.com/new and create a private, empty repository named `JARVIS`. Leave README, license and `.gitignore` unchecked; the scaffold script creates local files and the first commit.
2. Run `New-JARVISProject.ps1` from the starter kit. It creates the project structure, baseline commit on `main`, and branch `phase/00-feasibility`.
3. From the project folder, replace the placeholder username in these commands:

```powershell
git remote add origin https://github.com/<YOUR_GITHUB_USERNAME>/JARVIS.git
git push -u origin main
git push -u origin phase/00-feasibility
```

Git may open the standard sign-in flow. Do not paste a password or personal access token into source code. If the remote already has commits, stop rather than force-pushing.

### Each phase
1. In GitHub Desktop, choose the phase branch or create a new branch based on latest `main`.
2. Open the JARVIS folder in VS Code and verify the branch in the lower left.
3. Paste only the matching prompt under `docs/prompts/` into Codex.
4. Review the plan and diffs; reject unrelated changes.
5. Run `.\scripts\check.ps1` from the repository root in PowerShell.
6. Run `git diff --check` and inspect `git diff --stat`.
7. Ask Antigravity for a read-only review at milestones.
8. Use GitHub Desktop to inspect, commit after tests pass, publish/push, and open a PR.
9. Merge only when acceptance criteria pass. Start the next phase from updated `main`.
10. Tag meaningful milestones, e.g. `v0.1.0-alpha`.

### Useful Git commands

```powershell
# Inspect state
git status --short
git branch --show-current
git log --oneline -5

# Start a small task branch from current main
git switch main
git pull --ff-only
git switch -c feat/my-small-feature

# Before committing
git diff --check
git diff --stat
git status

# After tests pass
git add -A
git commit -m "feat: describe the completed change"
git push -u origin feat/my-small-feature
```

If `git pull --ff-only` says main has no upstream, sync in GitHub Desktop or run `git push -u origin main` first. Do not use `git reset --hard`, force-push or broad cleanup commands unless the change is understood and recoverable.

The starter includes a small GitHub Actions workflow for package installation, Ruff and pytest. Keep secrets/external API calls out of CI. Use included Actions minutes carefully; local checks are always available.

---

## 7. Full implementation roadmap with gates

A phase is complete only when its gate passes, not merely when code has been generated.

### Phase 00 — Feasibility and architecture decision
**Branch:** `phase/00-feasibility`  
Compared extending OpenClaw, a JARVIS-owned core with adapters, and building integrations from scratch; reviewed projects/licenses/security and researched tools, Windows automation, local inference feasibility, coding-agent entitlements, Android APIs and call restrictions. Findings and the accepted decision are in the ADR and feasibility report.

**Deliverables:** `docs/decisions/0001-architecture-choice.md`, `docs/research/FEASIBILITY_REPORT.md`, risk register and prioritized tasks.  
**Do not:** install every candidate, download model weights, connect personal accounts, or add paid services.  
**Gate:** evidence-based decision, recorded assumptions and explicit deferred capabilities.

### Phase 01 — Stable foundation
**Branch:** `phase/01-foundation`  
Build typed FastAPI app, config, redacted structured logging, error handling, health/readiness endpoints, tests, Ruff, setup/start scripts, minimal CI.  
**Gate:** setup works on Python 3.12; local server binds to loopback; tests and Ruff pass; no secrets tracked.

### Phase 02 — Model gateway
**Branch:** `phase/02-model-gateway`

Build provider protocol, deterministic mock adapter, typed request/response/errors, structured-output validation, capability declarations, router-policy skeleton, timeout/cancellation, usage/quota event metadata. Add a real provider only after data/privacy/quota policy is approved.  
**Gate:** provider can be swapped without changing task state; malformed/unavailable provider is handled; no paid fallback.

### Phase 03 — Goals and durable task runtime
**Branch:** `feat/durable-task-runtime`  
Build goal/task/step models, finite state machine, plan dependencies, events, checkpoints, pause/resume/cancel, bounded retry and duplicate-execution safeguards. Use deterministic/mock planner for tests.  
**Gate:** task survives restart and resumes without repeating a completed non-idempotent action.

### Phase 04 — Tool registry and policy engine
**Branch:** `feat/tool-registry-security`  
Build typed tool schemas, capability policy, low-risk test tools, timeouts, output limits, approval request interface, audit events. No arbitrary shell or real external actions yet.  
**Gate:** unauthorized tool calls are blocked by code, not prompts; errors/timeouts are observable; path traversal is denied.

### Phase 05 — Memory v1
**Branch:** `feat/memory-v1`  
Build SQLite records for task episodes, explicit user preferences, projects, artifacts and facts with source, confidence, sensitivity, scope, validity, expiry, version/supersession, correction and deletion. Begin with full-text and metadata search; add embeddings only after benchmarks prove value.  
**Gate:** provenance returned; corrections supersede stale facts; deletion works; scopes are isolated; migration tests pass.

### Phase 06 — Research and browser tools
**Branch:** `feat/research-browser-tools`  
Build source-aware research and selected Playwright workflows where an API is unsuitable; retain title/URL/retrieval time/evidence; isolate page content as untrusted data.  
**Gate:** source links retained; page prompt injection cannot authorize privileged tools; navigation/timeouts are bounded.

### Phase 07 — Windows execution node
**Branch:** `feat/windows-node`  
Build workspace-scoped file tools, allowlisted development commands, timeouts/output limits, and a small Windows UI Automation proof-of-concept for a harmless stable workflow.  
**Gate:** forbidden commands and outside-workspace paths are rejected; unexpected UI state safely halts; no unrestricted model-driven shell.

### Phase 08 — Cross-device API and Android companion
**Branch:** `feat/android-companion`  
Build authenticated pairing, device registry, capability reporting, task submit/status/cancel, notification events, offline behavior, then lightweight native Android client. Test on real phone first; avoid emulator initially.  
**Gate:** authentication/revocation work; task/results synchronize across reachable devices; disconnected laptop is reported honestly.

### Phase 09 — Communication integrations and call feasibility
**Branch:** `feat/communication-integrations`  
Build in sub-slices: drafts/summaries; authorized integration through official API/scopes; sending with action-specific authorization; audit send results; real-device call-state/screening feasibility; conversational-call prototype only if supported by OS/audio APIs and lawful.  
**Gate:** draft vs attempted send vs provider-confirmed send are distinct; ambiguous recipient prompts a question; call autonomy is marked implemented only after a real-device end-to-end proof. Investigate supported VoIP if cellular audio is unavailable.

### Phase 10 — Voice and vision
**Branch:** `feat/voice-vision`  
Build push-to-talk first, speech recognition, text-to-speech, explicit listening/mute/cancel states, image/screenshot/document input. Optional wake-word only after privacy/reliability tests.  
**Gate:** latency, CPU/RAM and failure behavior are benchmarked; no background recording without consent; text continues to work if voice is unavailable.

### Phase 11 — Proactivity and reusable skills
**Branch:** `feat/proactive-workflows`  
Build goal/deadline triggers, follow-up tasks, quiet hours, notifications, cancellation and versioned reusable skills. Do not add continuous polling without a concrete use case.  
**Gate:** proactive actions have an auditable trigger and user control; no spam; no high-impact commitment without approval.

### Phase 12 — Evaluation and security hardening
**Branch:** `phase/12-hardening`  
Build benchmark for task success, verification accuracy, recovery, memory correctness, prompt injection and permissions; add backup/restore, log redaction, dependency/license review, latency/RAM/CPU measurement.  
**Gate:** critical tests pass; known gaps documented; release claims match proven features; paid fallback is demonstrably off.

### Phase 13 — Release packaging
**Branch:** `phase/13-release`  
Build diagnostics, config checks, database backup/restore, clean setup/update/rollback guide, versioning, logs view and opt-in visible background start/stop.  
**Gate:** clean-install, restore, migration and rollback tests; resource profile recorded.

### Phase 14 — Advanced specialist agents
**Branch:** `feat/specialist-agents`  
Only after baseline benchmarks: add one specialized researcher/coder/verifier where an A/B test shows measurable improvement. Parallelize only independent tasks with bounded resource use.  
**Gate:** quality or latency improves without unacceptable quota, safety, resource or regression costs. Otherwise do not ship.

### Phase 15 — Ongoing expansion
Add new app integrations, business workflows, more devices, richer retrieval/knowledge graphs and eventually IoT as independent tools/adapters. Each gets a use case, permission policy, tests and acceptance criteria. Do not hardcode every new skill into the orchestrator.

---

## 8. Exact agent loop for each phase

1. Open the JARVIS repo in VS Code; confirm the correct feature branch.
2. Paste the matching phase prompt under `docs/prompts/` into Codex.
3. Require a short plan and expected files before edits. If it proposes >5–8 files or multiple subsystems, request a split.
4. Let Codex implement only the approved slice. Approve commands deliberately; decline destructive/unrelated commands.
5. Run `.\scripts\check.ps1` from the repository root in PowerShell.
6. Run `git diff --check` and inspect `git diff --stat`.
7. Start Antigravity and paste `docs/prompts/review-only.md`; require read-only review.
8. Have Codex fix only valid findings; rerun checks.
9. Use GitHub Desktop to inspect files, commit after tests pass, push and open a PR.
10. Merge only after acceptance criteria pass. Start next branch from updated `main`.
11. Update `docs/status/PROJECT_STATUS.md` with features, exact test results, gaps and the next task.

---

## 9. Definition of done

Do not accept a change because an agent says it is complete. Require:

- Tests added for behavior/regressions and test results shown.
- Ruff and `git diff --check` pass.
- Interfaces are documented.
- No secrets, personal data, virtual environments, model weights or logs committed.
- No unrelated files changed.
- No paid service/account/resource activated.
- No permission/security boundary loosened without a reviewed decision and approval.
- A report listing behavior, files changed, commands/tests, pass/fail, limitations and next step.

Maintain `docs/status/PROJECT_STATUS.md` as the recovery point for future agent sessions. Record current phase/branch, tested features, failures, decisions, exact setup/test commands and only the next immediate task.

---

## 10. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Huge, unreviewable agent diff | One phase/feature branch; small tasks; request split before broad edits. |
| Codex quota exhausted | Save a checkpoint; use Antigravity for one bounded task if included quota is available; use local checks; no paid fallback. |
| Antigravity throttling/model changes | Inspect the account's plan/usage/model screen; use it only for bounded tasks; wait for reset or use local checks. |
| OpenClaw has hidden integration/security boundaries | Phase 00 proof-of-fit; review security/extension maturity; least privilege. |
| RAM pressure | No Docker/emulator/large local model initially; one coding agent at a time; benchmark before inference. |
| Android restrictions on cellular calls | Investigate the actual device early; build messaging/notifications first; do not promise inaccessible audio. |
| Agent claims success without evidence | Tool results/tests determine status; use a verifier. |
| Personal data/credentials leak | Private repo, ignored `.env`/local data, minimal OAuth scopes, redacted logs, no sensitive cloud context without consent. |
| Dependency/license conflict | Review licenses and versions before reuse; prefer adapters instead of copying source. |
| Unexpected charges | No billing-enabled keys, no paid fallback, disable overage, verify settings. |

---

## 11. Official sources to recheck

- Codex plan/usage: https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan
- Antigravity VS Code extension: https://www.antigravity.google/docs/ide/extensions/
- Antigravity rules / AGENTS.md: https://www.antigravity.google/docs/rules/
- Antigravity plans: https://www.antigravity.google/docs/plans
- Antigravity models: https://www.antigravity.google/docs/models
- Copilot plans: https://github.com/features/copilot/plans
- Claude Code auth: https://docs.anthropic.com/en/docs/claude-code/getting-started
- OpenClaw/security: https://github.com/openclaw/openclaw and https://github.com/openclaw/openclaw/blob/main/SECURITY.md
- OpenHands SDK: https://github.com/OpenHands/software-agent-sdk
- GitHub Desktop branches: https://docs.github.com/en/desktop/making-changes-in-a-branch/managing-branches-in-github-desktop
- VS Code branches/worktrees: https://code.visualstudio.com/docs/sourcecontrol/branches-worktrees

## Final principle

Build a small, working, tested core first. Coding agents accelerate implementation; they do not remove engineering control. JARVIS owns its core state, permissions, memory contracts and verification. Reuse existing systems only after a proof of fit. Spend coding-agent quota on focused slices, use local tests for repetitive work, and expand only when the previous stage is reliable.


---

## 12. Where to store everything

Use a predictable local path such as `C:\Users\<you>\source\JARVIS`.

| Path | Contents | GitHub-tracked? |
|---|---|---|
| `src/jarvis/` | JARVIS backend source code | Yes |
| `apps/web/`, `apps/android/` | Future interfaces and mobile project | Source/docs yes; no build outputs |
| `tests/` | Automated tests and safe fixtures | Yes, provided fixtures have no personal data |
| `docs/` | Architecture, ADRs, research sources, status, prompts, security notes | Yes |
| `scripts/` | Setup, run, check and branch helpers | Yes |
| `.venv/` | Local Python environment and installed dependencies | **No**; regenerate with `setup-dev.ps1` |
| `data/local/` | Local SQLite database and personal memory, when implemented | **No** |
| `data/artifacts/` | Local outputs, downloaded files and generated artifacts | **No by default**; commit only deliberately selected, sanitized artifacts |
| `logs/` | Local diagnostic logs; redact first | **No** |
| `.env` | Local secrets when a later approved feature requires them | **Never** |
| `models/` | Optional local model weights/cache in a future phase | **Never** |
| GitHub remote | Versioned source, tests, docs, phase history | Code and non-sensitive docs only |

Backups should be outside the Git repo, for example `Documents\JARVIS-Backups`, and should not be synchronized publicly. Back up the database/config using a documented export procedure, not by committing a live personal-memory database. Do not store phone messages, call transcripts, private emails, model caches or secrets in GitHub.

---

## 13. How to work in VS Code without personally coding

For each phase you mostly need to review and approve, not manually write files.

1. Open the repository folder in VS Code.
2. Check the current branch in the lower-left status bar.
3. Open the phase prompt in `docs/prompts/`.
4. Copy the prompt into Codex. Ask it to read the prompt file itself if the extension supports workspace context.
5. Require a plan before changes. If it proposes a large cross-cutting rewrite, ask for a split.
6. Let the agent make the scoped change and run local checks.
7. Review the diff panel. Reject unexpected changes, especially to `.gitignore`, secrets/config, permissions, database migrations, workflows and scripts.
8. Run `.\scripts\check.ps1` in the terminal.
9. Use Antigravity for a read-only independent review. Do not let two agents edit the same files at once.
10. In GitHub Desktop review changed files and commit only after tests pass. Push and open a PR, then merge it after acceptance criteria pass.

When a task fails, give the agent the exact error and ask for the smallest root-cause fix plus a regression test. Avoid asking “fix everything” on a large codebase.

When quota runs out, update `docs/status/PROJECT_STATUS.md`, then stop. Resume using `docs/prompts/quota-recovery.md` when a legitimate allowance resets or use the other eligible included agent for the next bounded task after checking current Git state. Do not enable paid fallback.

