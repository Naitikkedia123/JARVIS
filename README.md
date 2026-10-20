# JARVIS

JARVIS is a model-independent personal AI operating-system project. This repository is built in tested stages with coding agents. The detailed plan is in `docs/roadmap/MASTER_IMPLEMENTATION_PLAN.md`.

## Status
Phases 05 Memory v1, 06 Research, Sources and Browser Tools, and 07 Memory
Retrieval and Context Assembly are merged into `main`. Phase 08 Pass D and Pass E
are merged into `main` through PRs #12 and #13; Pass E includes the owner
credential lifecycle and durable approval/resume work. Owner-authorized task
cancellation was merged through PR #14. Its local post-merge validation reported
423 passed and 2 skipped because Windows policy blocked symlink creation; this
validates the cancellation implementation, not all Phase 08 acceptance criteria.
Phase 08 remains open while cross-device synchronization, notification events,
offline behavior, and the Android companion are unresolved. JARVIS is not a
complete autonomous assistant.

## Quick start (Windows PowerShell)
```powershell
.\scripts\setup-dev.ps1
.\scripts\check.ps1
.\scripts\run-local.ps1
```
Visit `http://127.0.0.1:8000/health`. Read `AGENTS.md` before editing and use one phase prompt at a time from `docs/prompts/`.

## Safety and privacy
- Never commit `.env`, tokens, passwords, private messages, or logs.
- Paid model fallback must remain disabled.
- Model output is not authorization; policy code authorizes tools.
- Do not expose the development server to the public internet.
