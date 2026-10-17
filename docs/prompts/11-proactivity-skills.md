# Phase 11 — Proactivity, Reminders and Reusable Skills

Implement minimal event-driven scheduling for configured goals, deadlines and follow-ups. No continuously polling daemon without a concrete use case. Every scheduled action needs its source/condition, next run, permission, audit record, cancellation and quiet-hours policy.

Reusable skills must be versioned/validated; the model cannot silently change executable skill code or permissions. Distinguish recommendation from execution. Approval is required for high-impact/irreversible actions, spending, deletion, new recipients or commitments.

Acceptance: user can inspect/edit/pause/delete schedule; no duplicate jobs after restart; notifications rate-limited and traceable; timezone/missed-run tests pass. Update status. No commit/push.
