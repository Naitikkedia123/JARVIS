# ADR 0002: Metadata and full-text retrieval for memory v1

**Status:** Accepted for Phase 05 v1

## Context

The first memory slice needs local, inspectable retrieval for four small record types while preserving scope, sensitivity, permission, provenance and validity rules. Embeddings would add a model/runtime dependency, vector storage and additional handling for derived representations of private content. No retrieval-quality dataset or resource measurements currently justify that complexity.

## Decision

Use SQLite metadata filters and local full-text search. Use SQLite FTS5 when the runtime provides it and a bounded, parameterized `LIKE` fallback when FTS5 is unavailable. Apply the same store-enforced access, permission, provenance, lifecycle and validity predicates to both retrieval paths. Do not add embeddings or a vector database in v1.

## Consequences

The implementation has no new runtime dependency, network call or model requirement. FTS and metadata indexes stay in the same transactional database as records. The fallback is less capable and may be slower for larger corpora; both paths are lexical rather than semantic. SQLite files are not encrypted by this decision, and deletion does not promise forensic erasure from database pages, WAL files or backups.

## Gate before considering embeddings

Revisit this decision only after a concrete use case demonstrates that metadata and full-text search are insufficient. Require a representative, locally held evaluation set; retrieval-quality comparison against the v1 baseline; privacy and retention review for embedding inputs, derived vectors, deletion and backups; model/license and offline-operation review; and CPU, RAM, disk, latency and index-growth measurements on the supported Windows/Python environment. Any later design must preserve the same authorization filters and deletion/supersession semantics. This ADR does not approve downloading weights, calling hosted model APIs or introducing a vector service.
