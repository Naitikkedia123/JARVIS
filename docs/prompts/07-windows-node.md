# Phase 07 — Windows Execution Node

Implement the smallest real Windows capability behind the existing tool registry. Prioritize workspace-scoped files and observable UI Automation over coordinates or a general-purpose shell. Read the threat model.

Start with file operations in the JARVIS workspace and an allowlist for a few development commands (test/lint/status), with timeout and output-size limits. Do not expose arbitrary PowerShell or Admin execution to a model. Prototype UI Automation for one harmless, repeatable app workflow; if accessibility controls are inadequate, report the limitation rather than faking success. Do not change files outside the workspace unless separately approved.

Acceptance: path traversal/forbidden commands rejected; output and errors recorded; result verified independently; timeout/cancel tested; Windows-specific tests gated elsewhere. Update security/status. No installs or large downloads without explaining first. No commit/push.
