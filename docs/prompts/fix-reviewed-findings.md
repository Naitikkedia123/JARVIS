# Fix Only Confirmed Review Findings (Codex)

Read `AGENTS.md`, active phase prompt and independent review. Classify findings as valid, invalid or already fixed; do not blindly accept suggestions.

Fix only confirmed P0/P1 findings and directly relevant P2s. Don't refactor unrelated modules. Add regression tests for accepted bugs. Run `.\scripts\check.ps1` and `git diff --check`. Report fixes/rejections and exact commands/results. Do not commit/push.
