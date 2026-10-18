# Memory v1 design

## Scope and record contract

Memory v1 stores four record kinds in a local SQLite database: task episodes, explicit user preferences, project facts and artifact references. A `MemoryRecord` carries a stable ID, optional logical key and artifact reference, title/content, source and source timestamp, UTC creation/update timestamps, confidence, provenance, scope, sensitivity, permission state, validity/expiry, version, supersession link and lifecycle state. Unexpected fields are rejected. Timestamps must be timezone-aware and are normalized to UTC; confidence is constrained to 0–1.

Provenance values distinguish `user_confirmed`, `model_inferred`, `tool_observed` and `legacy_unknown`. A provenance value is descriptive evidence, not permission to retain or reveal data. Permission defaults to `unspecified`, and only `allowed` records appear in ordinary retrieval.

## Access and management

Retrieval requires a `RetrievalContext` with exact scopes, maximum sensitivity and an explicit `include_inferred` choice. Empty scope sets return no records. ID lookup, FTS search and metadata filters apply the same policy inside the SQLite store: current lifecycle, validity window, non-expiry, exact scope, sensitivity ceiling, allowed permission and provenance policy. An inaccessible ID returns the same not-found-style error as a missing ID.

Creation, correction and deletion require a separate `ManagementContext`; it grants no retrieval rights. Mutations are allowed only for matching scopes and within the context's sensitivity ceiling. The calling application is responsible for constructing trusted contexts; v1 does not authenticate a principal or offer a consent UI.

## Current records, correction and deletion

Correction uses `BEGIN IMMEDIATE` and an expected version. It updates the predecessor to `superseded` and inserts a linked replacement in one transaction. A superseded record cannot be corrected as current. Deletion hard-removes the selected row and its full-text entry in a transaction. If a replacement is deleted, its predecessor remains superseded and is never reactivated. The supersession foreign key uses `ON DELETE SET NULL` so deleting an older record does not block deletion or retain its content.

SQLite row removal is not secure forensic erasure: content may remain in free pages, WAL files, backups or storage snapshots. No content audit log is written. Backup, retention automation and secure-erasure workflows are outside v1.

## SQLite schema and migration

The store receives an explicit database path, enables SQLite foreign keys, and sets a bounded busy timeout. Ordered schema migrations are recorded in `memory_schema_migrations`, separate from task-store migrations. Each migration and ledger update share a transaction. Opening a current database is idempotent.

The v1 schema represents the prior record layout. Migration v2 adds governance and validity metadata plus indexes. Existing payloads are preserved, but records with unknown governance receive `legacy_unknown` provenance, `legacy_inaccessible` scope, restricted sensitivity and unspecified permission. These rows are not returned through normal retrieval. FTS5 is created and populated in the migration when SQLite supports it; otherwise a normal SQLite table supports a parameterized, escaped `LIKE` fallback. Insert, correction and deletion update the record and search index within the same transaction.

## Retrieval

Search accepts bounded input and result counts. FTS query terms are tokenized and passed as a parameter. The fallback escapes SQL wildcard characters and also binds all terms as parameters. Metadata filters can constrain record type, logical key and provenance. Search and metadata results share authorization and validity predicates; neither can return denied, unspecified, out-of-scope, over-sensitivity, expired, future-valid or superseded records.

## Limitations

V1 does not use embeddings, external services, model calls, background jobs, API routes or task-runtime wiring. SQLite files are not encrypted by this feature. Access contexts are trusted caller inputs, not authenticated identities. There is no backup/restore tooling, automated retention policy, secure deletion guarantee, multi-process conflict workflow beyond SQLite transactions/version checks, or production readiness claim.
