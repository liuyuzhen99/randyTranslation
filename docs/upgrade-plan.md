# Enterprise Upgrade Draft Plan (FastAPI + DAO + SQLAlchemy + RabbitMQ + PostgreSQL + Qdrant)

## Summary
Upgrade the current single-process pipeline into a modular, scalable service architecture: FastAPI handles command/query APIs, RabbitMQ handles asynchronous workflow orchestration, PostgreSQL becomes the source of truth for transactional data, and Qdrant serves vector retrieval for audit/translation memory.  
Target rollout is **Single VM + Docker** first, with structure kept Kubernetes-ready. Media files are stored in **S3-compatible OSS**. Migration uses **phased dual-write** to minimize risk.

## Implementation Changes
1. Core architecture and boundaries
- Split into clear layers: `api` (FastAPI routes), `application` (use cases/services), `domain` (entities/value objects), `infrastructure` (DAO repositories, SQLAlchemy models, RabbitMQ publishers/consumers, Qdrant adapter, OSS adapter).
- Replace direct DB calls and global task dictionaries with DAO interfaces and explicit application services.
- Introduce dependency injection for repositories/clients so new functions can be added without rewriting core flows.

2. Data platform migration (SQLite/Chroma -> PostgreSQL/Qdrant)
- Replace SQLite tables with PostgreSQL schema managed by Alembic.
- Normalize job lifecycle with explicit tables: `jobs`, `job_events`, `artists`, `videos`, `subtitles`, plus optional `translation_memory_index`.
- Define canonical lifecycle contract for `JobStatus` + `StageStatus`, including allowed transitions, terminal states, retry semantics, and invalid-transition rejection.
- Add strict constraints and indexes for concurrency and consistency:
1. unique keys for idempotency (`video_id`, external event IDs),
2. business-key uniqueness (`subtitles(video_id, line_index)`, outbox event ID, stage dedupe key),
3. status transition guards (pending -> processing -> completed/failed),
4. optimistic locking/version columns for high-contention updates.
- Replace Chroma usage with Qdrant collections:
1. `translation_memory`,
2. `audit_style_memory`,
3. payload fields for `artist`, `video_id`, quality/status tags, timestamps.
- Start dual-write in shadow mode after Phase 2 stabilization (legacy remains authoritative first), then maintain continuous reconciliation cadence (counts/checksums/sample payload parity) across the migration window.

3. Workflow orchestration with RabbitMQ
- Convert long-running steps (download, transcribe, audit, translate, render) into asynchronous task consumers.
- Define queue topology:
1. `pipeline.command` (incoming jobs),
2. stage queues (`download`, `transcribe`, `audit`, `translate`, `render`),
3. `pipeline.dlq` for poison messages.
- Enforce message idempotency using dedupe keys in PostgreSQL and consumer-side retry policy with backoff.
- Require transactional outbox pattern (PostgreSQL outbox table + dispatcher) as the only publish path to keep DB state changes and RabbitMQ publishes consistent.

4. API and interface upgrades
- FastAPI public APIs become job-based:
1. `POST /v1/jobs` (submit task),
2. `GET /v1/jobs/{job_id}` (status/progress),
3. `GET /v1/jobs/{job_id}/events` (timeline),
4. `POST /v1/videos/{video_id}/retry` (controlled reprocess).
- Pydantic request/response contracts with explicit enums for states and standardized error model.
- Add health/readiness endpoints for DB, RabbitMQ, Qdrant, and OSS connectivity.
- Keep existing behavior behind compatibility wrappers during transition, then deprecate old entrypoints.
- Add legacy-to-v1 compatibility matrix for `/create_task`, `/check_status/{task_id}`, `/list_tasks` with response/status/error mapping, deprecation headers, and a defined sunset schedule.

5. Storage and operations
- Move transient and output media paths from local absolute filesystem to OSS object keys.
- Persist only metadata + object URIs in PostgreSQL.
- Add centralized config (`.env` + typed settings), structured logging with correlation IDs, metrics/tracing hooks, and audit logs for key state transitions.
- Create Docker Compose stack for local/prod-like validation (FastAPI, worker, Postgres, RabbitMQ, Qdrant, MinIO-compatible OSS).
- Define release-blocking observability thresholds (queue lag, retry ceiling, p95 stage latency, error rate) and minimum security baseline (authn/authz boundary, rate limits, secret rotation, data retention).

## Public APIs / Interfaces / Types
- New DAO interfaces:
1. `ArtistRepository`,
2. `VideoRepository`,
3. `SubtitleRepository`,
4. `JobRepository`,
5. `OutboxRepository`,
6. `VectorRepository`.
- New service interfaces:
1. `PipelineOrchestrator`,
2. `TranslationMemoryService`,
3. `AuditService`,
4. `MediaStorageService`.
- New shared types:
1. `JobStatus` enum,
2. `PipelineStage` enum,
3. `RetryPolicy` config type,
4. `StageExecutionKey` (dedupe identity for stage execution attempts),
5. compatibility DTOs for legacy endpoint responses during transition,
6. versioned error model policy and event payload schemas for RabbitMQ messages.

## Test Plan
1. Unit tests
- DAO tests for CRUD, idempotency, optimistic locking, and status transition guards.
- Service tests for orchestration logic, retry behavior, and failure branching.
- Qdrant adapter tests for upsert/query consistency and payload filtering.

2. Integration tests
- API + PostgreSQL + RabbitMQ + Qdrant + OSS (containerized) end-to-end happy path.
- Duplicate message replay test confirms no duplicate rows or double-processed jobs.
- Partial-failure tests:
1. DB commit succeeds but broker unavailable (outbox dispatcher recovers),
2. worker crash mid-stage (message requeue + idempotent resume),
3. vector write fail while transaction data remains consistent and recoverable.
4. crash-before-publish and crash-after-publish outbox recovery with no lost/phantom events.

3. Concurrency and performance tests
- High-concurrency job submission and polling load.
- Worker throughput tests with configurable consumer concurrency.
- Data consistency assertions under concurrent updates and retries.

4. Migration validation tests
- Dual-write parity checks between old and new stores.
- Backfill correctness for subtitles/lyrics/vector payloads.
- Cutover rehearsal with rollback drill.
- Continuous parity checks across the dual-write window with documented reconciliation cadence and thresholds.

5. Contract and state-machine tests
- Invalid transition rejection tests for lifecycle state machine (DB + service layer).
- Legacy compatibility contract tests for `/create_task`, `/check_status/{task_id}`, `/list_tasks` mapped behavior during transition.

## Assumptions and Defaults
- Deployment phase 1 uses **Single VM + Docker**, but module boundaries and infra config remain Kubernetes-ready.
- Media files use **S3-compatible OSS** as primary storage; local disk only for short-lived temp buffers.
- Migration strategy is **phased dual-write** with parity checks before final read-switch.
- Dual-write starts after Phase 2 stabilization in shadow mode and remains active until cutover validation is complete.
- PostgreSQL is the single source of truth for transactional state; Qdrant is for retrieval/indexing, not authoritative business records.
- All newly introduced modules must include tests (unit + integration), and CI must block merge on test failures.
- Legacy compatibility window is time-bound and removed only after sunset criteria and contract tests are satisfied.
- SLO thresholds and security baseline are release blockers for production cutover.
