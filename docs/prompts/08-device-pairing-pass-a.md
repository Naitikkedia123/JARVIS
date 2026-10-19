# Phase 08 Pass A — Persistent Device Pairing Core

## Capability

Provide a local, persistent device identity and pairing service that later API routes can use to authenticate devices, report their advertised capabilities, and revoke a compromised companion. This is the first backend contract slice of Phase 08, not the complete cross-device API or Android companion.

## Included

- Typed device, platform, role, pairing metadata, and capability-report models with bounded fields and UTC timestamps.
- File-backed SQLite storage with an injectable path and a versioned migration ledger.
- One explicit bootstrap pairing code (minimum 32 characters), consumed atomically once to establish the owner device. The caller must provide it out of band; length alone does not prove entropy.
- Random bearer tokens issued once, with only SHA-256 digests persisted. Active tokens authenticate to a typed device record; revocation survives database reopen.
- An owner can list devices, issue a random five-minute single-use companion pairing code, and revoke companions. Owner credentials cannot revoke the owner device.
- Device-reported capability labels are untrusted descriptive metadata only. They cannot grant tool, task, or API authorization.
- Deterministic temporary-database tests for migration, reopen, one-use pairing, expiry, token hashing, authorization, capability validation, and revocation.

## Explicit non-goals

- No FastAPI routes, authentication middleware, changes to the listener, or remote access in Pass A. Existing `/health` and `/ready` remain loopback liveness endpoints.
- No task submit/status/cancel API, notification stream, offline-state model, Android project/UI, VPN, TLS termination, account integration, or production deployment configuration.
- No automatic bootstrap-code generation, credential recovery, owner reset, token expiry, encryption at rest, or claim of forensic erasure.

## Reused interfaces and boundaries

- The service is a trusted application-layer dependency. A future API layer must authenticate before accessing protected device or task operations and must not expose a token to model output, browser content, or tool arguments.
- The SQLite path is injected. Device metadata and hashes are local application state; operating-system file permissions protect the database file.
- The current FastAPI host remains `127.0.0.1`. Cross-device reachability requires a separately reviewed private transport design; this pass does not open a LAN listener.
- Reported platform/capability values are claims from the paired client and are not verified device facts or policy grants.

## Failure and recovery behavior

- Invalid, expired, consumed, or unauthorized pairing requests fail with typed safe errors. They do not create a device or token record.
- Pairing code consumption, device insertion, and token digest persistence occur in one SQLite transaction. A failed transaction leaves the code available for a retry; a committed pairing cannot reuse that code.
- Unknown, malformed, and revoked bearer tokens return the same authentication failure category. Plaintext tokens and pairing codes are never stored or logged.
- Revocation is permanent in this pass. Losing the owner credential has no recovery flow; operators must preserve the local database and owner credential. No task dispatch or external side effect occurs here.

## Acceptance checks

- Pairing creates one owner and persists only secret digests; reopening the database authenticates the same token and rejects bootstrap replay.
- The owner can issue a short-lived one-use pairing code, pair a companion, list it, and revoke it. A companion cannot manage the registry; revoked credentials fail after restart.
- Invalid capability syntax and invalid timestamp/name bounds fail model validation; capability reports never alter device role or authorization.
- Focused device tests, full pytest suite, scoped Ruff, and `git diff --check` pass without network, API keys, model weights, or paid services.

## Deferred Phase 08 slices

1. Expose authenticated pairing/device routes and wire trusted startup configuration without changing the loopback-only binding.
2. Define task submit/status/cancel, durable event delivery, and honest disconnected/offline behavior using the existing task runtime.
3. Implement a minimal native Android client only after the server contract and real-device transport/pairing setup are reviewed. Do not add message/call permissions.
