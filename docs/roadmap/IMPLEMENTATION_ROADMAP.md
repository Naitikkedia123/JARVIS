# JARVIS Implementation Roadmap

1. **00 Feasibility:** decide extend-OpenClaw vs JARVIS-owned core + adapters; inspect licenses/security; verify tooling and Android limits.
2. **01 Foundation:** typed API, config, logs, health endpoint, tests, CI.
3. **02 Model gateway:** provider interface, mock adapter, structured output validation, quota/privacy routing.
4. **03 Durable tasks:** goals, plan graph, state machine, checkpoints, resume/cancel.
5. **04 Tools and security:** registry, typed tools, approval gates, restricted execution and audit.
6. **05 Memory v1:** SQLite records, source/confidence/sensitivity/validity, correction and deletion.
7. **06 Research/browser:** source metadata, web-content boundaries, browser automation.
8. **07 Windows node:** workspace-scoped files/commands and UI Automation proof of concept.
9. **08 Android:** authenticated pairing, task/status/notifications and offline behavior.
10. **09 Communications:** email/message integrations and calling feasibility prototype.
11. **10 Voice/vision:** push-to-talk, STT/TTS and visual inputs after benchmarks.
12. **11 Proactivity:** goal/deadline monitoring, quiet hours and reusable skills.
13. **12 Hardening:** evaluation, threat tests, backups, quotas, privacy and performance.
14. **13 Release:** clean setup, diagnostics, restore/migration, opt-in startup.
15. **14+ Expansion:** specialist agents, richer retrieval and additional integrations only after benchmarks.

A phase is accepted only after its predecessor's acceptance criteria pass. Read `docs/roadmap/MASTER_IMPLEMENTATION_PLAN.md` for the detailed plan.
