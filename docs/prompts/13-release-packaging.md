# Phase 13 — Release and Smooth Local Operation

Prepare diagnostics, config validation, database backup/restore, log redaction, version information, clean setup/upgrade instructions, and rollback. Background startup is opt-in, visible and reversible; do not install hidden services or autostart agent actions without approval.

Test from a clean checkout. Do not require Docker, paid cloud, local weights or Android emulator. If Android Studio/SDK is needed, document exact setup and use real phone for tests where possible.

Acceptance: clean setup passes; health/readiness distinguishes unavailable provider/device from server errors; backup restores; update/rollback documented; main passes checks. Update README and status; propose release tag only after owner review.
