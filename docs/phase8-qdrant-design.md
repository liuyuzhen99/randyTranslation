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

The first implementation used `HashingEmbeddingProvider` for deterministic local tests and migration drills. That provider emitted 384-dimensional vectors by default and is not suitable for production semantic retrieval.

The production target is now `BGEEmbeddingProvider` with `BAAI/bge-m3` and 1024-dimensional vectors. Qdrant collection dimensions are immutable for practical migration purposes, so switching providers requires validating the existing collection before any writes.

Before cutover, choose and document the production embedding model and store at minimum:

- embedding provider name
- model/version
- vector dimension
- text normalization rules

## Dimension Migration Guardrail

Before switching production traffic to `BAAI/bge-m3`, confirm each Qdrant collection's `collection_info` vector size in the Qdrant dashboard or API:

- If production Qdrant was written with OpenAI `text-embedding-3-small`, the existing size is 1536. Rebuild the collection and rerun `scripts/phase8_qdrant_backfill.py` before switching.
- If production has always used fallback `HashingEmbeddingProvider`, the existing size is 384. Rebuild the collection as 1024-dimensional and rerun `scripts/phase8_qdrant_backfill.py`.
- If the collection already reports 1024, no dimension rebuild is required, but backfill/parity and retrieval quality checks still need to pass.

The Qdrant adapter rejects writes when an existing collection dimension does not match the configured embedding provider dimension. This is intentional: a dimension mismatch indicates stale collection data and must be resolved by collection rebuild plus backfill, not by mixed writes.

## Backfill Flow

1. Read records from source `VectorRepository` by namespace.
2. Build deterministic point ID.
3. Generate embedding.
4. Upsert to target Qdrant collection.
5. Search back by source text and verify the migrated point is retrievable.
6. Emit `VectorBackfillReport`.

Dry-run mode reads the source and validates counts without requiring Qdrant. Live backfill defaults to 1024-dimensional embeddings unless `VECTOR_EMBEDDING_DIMENSION` or `--embedding-dimension` overrides it for an isolated drill.

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
