# Phase 09 — Communications and Calling Feasibility

Split into separate sub-slices: (A) summarize/draft using fixtures; (B) authorized provider via official API and minimal scopes; (C) send only with action-specific approval or a narrow user-configured allowlist; (D) research call-state/screening/audio on the actual Android device; (E) conversational-call prototype only if OS/audio APIs support it and it is lawful.

Never use stored account passwords, scrape private app internals, record secretly or impersonate a human. Ask if contact/recipient is ambiguous. Do not make commitments for the user unless specifically authorized. Distinguish draft, send attempt, provider-confirmed send and delivery status. Keep message bodies out of ordinary logs. Do not connect real accounts until user approves exact OAuth scopes.

For cellular calls, document actual Android version, API/role requirements, OEM/carrier restrictions, audio limitations and disclosure/consent requirements. If cellular audio is unavailable, report it and consider supported VoIP; do not claim full call autonomy without real-device evidence.

Acceptance: draft vs send tested separately; no real external sends in tests; audit is content-minimized; calling is claimed only after an end-to-end phone test. Update threat model/status. No commit/push.
