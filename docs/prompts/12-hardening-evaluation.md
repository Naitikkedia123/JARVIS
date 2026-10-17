# Phase 12 — Reliability, Safety and Evaluation

Build a repeatable benchmark covering task success, verification accuracy, recovery, restart/resume, correct memory retrieval, source provenance, permissions, quota failures, latency, RAM/CPU and unnecessary user intervention. Include adversarial fixtures for prompt injection in pages/documents and unauthorized tool requests.

Run dependency/license review, secret/log redaction checks, backup/restore, migration, clean-install and permission tests. Do not report a score without running the benchmark and preserving the results. Do not weaken policy to make tests pass. Representative measurements must use the actual CPU-only/8 GB environment. Use fake adapters in external integration tests.

Acceptance: no known critical failing test; release claims match tested features; paid fallback off; rollback and deletion documented; gaps listed in project status. No commit/push.
