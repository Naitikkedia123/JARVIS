# Phase 08 Pass B — Authenticated Device API

## Goal and scope

Expose the existing Pass A `DeviceService` through the current FastAPI application. Keep the service as the authority for credential validation, owner checks, one-time pairing, expiry and revocation. Keep the application bound to `127.0.0.1`.

This pass does not add task synchronization, event streaming, offline queues, background workers, Android UI, a public listener, new authorization grants, or bearer-token expiry.

## HTTP contract

All routes use the `/devices` prefix:

- `POST /devices/pair` accepts one bounded pairing code, device name, platform and optional descriptive capability report. It does not require a bearer token because the code is the one-time credential. A successful exchange returns a typed device record and the new bearer token exactly once. Bootstrap and companion codes both use the existing `DeviceService.pair()` contract.
- `GET /devices/me` authenticates a bearer token and returns only the authenticated device's public metadata.
- `PATCH /devices/me/capabilities` authenticates a bearer token and updates only its descriptive capability report. Reports never grant authorization.
- `POST /devices/pairing-codes` requires an active owner bearer token and returns the newly generated five-minute, one-use companion code exactly once. Issuance invalidates any earlier unused code for that owner through the existing SQLite transaction.
- `GET /devices` requires an active owner bearer token and returns typed public device records.
- `DELETE /devices/{device_id}` requires an active owner bearer token and persistently revokes a companion. The owner cannot revoke itself.

Missing, malformed, unknown and revoked bearer credentials fail closed. Error responses use the existing generic JSON envelope and safe messages. Pairing-unavailable errors do not disclose whether a code was unknown, expired, consumed or superseded. Storage failures return a generic service-unavailable response. Responses never contain credential digests, pairing-code digests, SQLite paths or exception details.

## Startup and provisioning

Typed settings supply an injectable file-backed SQLite path and an optional `SecretStr` bootstrap code through `JARVIS_DEVICE_DATABASE_PATH` and `JARVIS_DEVICE_BOOTSTRAP_CODE`. The default database path is `data/local/devices.sqlite3`, which is ignored by Git. If a bootstrap code is configured, the service stores only its digest and accepts it once. Provisioning uses the Pass A `generate_bootstrap_code()` helper; the trusted operator must securely deliver the plaintext to the intended owner and configure the same value for the server. No default secret is generated or logged. Without a configured bootstrap code and an existing owner record, initial pairing is unavailable.

The app opens and closes its SQLite store in its lifespan. With no configured bootstrap code and no existing database, device routes are unavailable and no database is created; this avoids silently provisioning an unreachable initial credential. Tests use temporary paths and deterministic codes. A bounded body-size middleware limits `/devices` request bodies. Device handlers perform the existing synchronous SQLite calls on the app event-loop thread because the store connection is thread-affine; SQLite lock waits can therefore briefly block other requests on this local app. This pass does not introduce background work. Existing redacted logging and disabled Uvicorn access logging remain in force so request credentials and codes are not logged.

## Security boundary and validation

Bearer credentials authenticate only to the existing paired-device identity. The service enforces owner-only registry operations; self-reported capabilities remain metadata and are not consulted as grants. API validation errors do not echo submitted values. The one-time token and pairing code are explicitly returned only by their issuance responses and are never persisted in plaintext.

Tests cover successful and failed authentication, owner/companion separation, one-use bootstrap and companion pairing, expiry, revocation, safe serialization, bounded/invalid requests, storage failure and pairing transaction rollback, app lifespan persistence, and unchanged health/readiness/loopback defaults. Tests use local temporary SQLite files and `TestClient`, with no external network calls.

## Deferred work and limitations

This is an authenticated loopback API slice, not remote cross-device transport. Bearer tokens have no expiry in Pass A/B. SQLite is not encrypted by this pass, and deletion is not a forensic-erasure guarantee. Database access still depends on operating-system file permissions. Task submission/status/cancel, notifications/events, offline behavior and the Android companion remain future slices. Pass B is not accepted until validation, CI and independent review pass.
