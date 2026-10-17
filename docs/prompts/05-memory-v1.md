# Phase 05 — Memory v1

Build a small provenance-aware memory layer using SQLite and, if justified, SQLAlchemy. Do not add a vector/graph database yet.

Implement only justified record types: task episodes, explicit user preferences, project facts and artifact references. Store source, timestamps, confidence, scope, sensitivity, permission, validity/expiry, version/supersession, correction and deletion semantics. Distinguish user-confirmed data from model-inferred guesses. Add full-text/metadata retrieval before embeddings; make embeddings an ADR-gated future option.

Acceptance tests: source/time returned; correction supersedes stale facts; deletion removes intended record; expired data not current; scope/sensitivity is enforced; migrations preserve data. No external service in tests. Update status and docs. No commit/push.
