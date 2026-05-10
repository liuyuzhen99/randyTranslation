# Phase 8 Qdrant Design

## Collections

Phase 8 introduces two Qdrant collections:

- `translation_memory`
- `audit_style_memory`

Qdrant is an index/retrieval store only. PostgreSQL/SQLite source records remain authoritative during migration and cutover validation.

## Point ID Strategy

Backfill uses deterministic point IDs:

```text
uuid-format sha256(namespace + ":" + source_vector_id)[0:32]
```

This makes the migration idempotent:

- rerunning backfill updates the same point;
- duplicate source rows do not create duplicate Qdrant points;
- parity reports can compare source count to target count repeatably.

The original source ID is preserved in payload metadata:

- `phase8_source_namespace`
- `phase8_deterministic_id`

## Payload Contract

Every point payload contains:

- `vector_id`
- `namespace`
- `text`
- source metadata fields such as artist, video ID, line range, BPM, energy, or review context

The current implementation keeps metadata flexible because legacy translation and audit memory sources have different shapes.

## Embeddings

The first implementation uses `HashingEmbeddingProvider` for deterministic local tests and migration drills. This is not the final production embedding model.

Before cutover, choose and document the production embedding model and store at minimum:

- embedding provider name
- model/version
- vector dimension
- text normalization rules

## Backfill Flow

1. Read records from source `VectorRepository` by namespace.
2. Build deterministic point ID.
3. Generate embedding.
4. Upsert to target Qdrant collection.
5. Search back by source text and verify the migrated point is retrievable.
6. Emit `VectorBackfillReport`.

Dry-run mode reads the source and validates counts without requiring Qdrant.

## Quality Gate

`Phase8RetrievalQualityEvaluator` runs representative query cases:

- namespace
- query text
- expected migrated IDs
- top-K limit

The gate passes only when every expected ID appears in the retrieved result set. This is intentionally strict for migration acceptance; broader relevance scoring can be layered on once curated datasets exist.

## Live Drill Prerequisites

Live Qdrant drills require:

- Qdrant server reachable at `QDRANT_URL`
- optional `QDRANT_API_KEY`
- optional Python `qdrant-client` installed in the runtime environment

If `qdrant-client` is unavailable, the adapter falls back to Qdrant REST endpoints. Docker Qdrant validation has been completed against `http://127.0.0.1:6333` with isolated `phase8_live_*` collections.
