# Phase 08 — Cross-Device API and Android Companion

First stabilize authenticated API/device/task contracts. Implement server-side device registry, revocable pairing, authentication, device capability reporting, task submit/status/cancel, notification events and offline state. Tests/mocks first, Android UI second.

Then create a minimal native Android client only if Phase 00 confirms the path; use platform-supported APIs, least permissions and encrypted transport. Test on the real phone first; do not install/run an emulator on the 8 GB laptop unless needed. Do not add message/call permissions in this phase.

Acceptance: unauthenticated requests fail; pairing revocable; tasks/results flow across reachable devices; disconnected laptop reported honestly; state survives restart; API contract documented; tests pass. Document exact device setup. No commit/push.
