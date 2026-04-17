# Enterprise Upgrade Draft Plan (FastAPI + DAO + SQLAlchemy + RabbitMQ + PostgreSQL + OSS + Qdrant)

## Summary
Upgrade the current single-process pipeline into a modular, scalable, enterprise-oriented service architecture:
- FastAPI provides versioned command/query and BFF APIs.
- PostgreSQL becomes the source of truth for transactional data.
- RabbitMQ orchestrates asynchronous workflow execution.
- S3-compatible OSS stores media artifacts.
- Qdrant is introduced later for vector retrieval after workflow and curation loops are stable.

Target rollout is **Single VM + Docker** first, with module boundaries kept Kubernetes-ready.  
Migration uses **phased dual-write + reconciliation** to minimize cutover risk.

This project is not a generic media-processing demo. The target product flow is:
- sync followed artists from Spotify
- resolve YouTube channel IDs
- fetch latest official MV candidates from YouTube/RSS
- dedupe and maintain a reviewable candidate catalog
- transcribe lyrics/audio
- AI review transcript
- AI audit track fit/taste with RAG support
- optional manual review after taste audit
- AI translate lyrics to Chinese with translation-memory/RAG support
- AI review translated output
- download video
- burn bilingual subtitles with ffmpeg
- curate accepted outputs into library and memory feedback loops

The target frontend surfaces are:
- `artists`
- `audit queue`
- `pipeline`
- `library`

So the upgrade plan must optimize for:
- backend scalability and consistency
- stable frontend-facing BFF orchestration
- explicit manual review checkpoints
- curation actions that feed taste/translation memory
- auditable human decisions
- low-risk incremental rollout

## Implementation Changes
1. Core architecture and boundaries
- Split into clear layers: `api`, `application`, `domain`, `infrastructure`.
- Keep screen-facing BFF routes in `api/` and do not expose repository shapes directly to frontend.
- Replace direct DB calls and process-local task state with repository interfaces and application services.
- Introduce dependency injection for repositories, queue publishers, vector clients, and storage adapters.

2. Data platform migration (SQLite/Chroma -> PostgreSQL/OSS, Qdrant later)
- Replace SQLite tables with PostgreSQL schema managed by Alembic.
- Normalize core entities first: `artists`, `artist_sync_runs`, `videos`, `video_candidates`, `reviews`, `jobs`, `job_events`, `subtitles`, `outbox`.
- Define canonical lifecycle contracts for:
  - job status
  - stage status
  - review status
  - candidate ingestion status
- Add strict constraints and indexes for concurrency and consistency:
  - unique keys for idempotency (`video_id`, external source keys, review IDs)
  - business-key uniqueness (`subtitles(video_id, line_index)`, stage dedupe keys, outbox dedupe keys)
  - optimistic locking/version columns for high-contention updates
  - foreign keys for workflow traceability
- Start dual-write in shadow mode after PostgreSQL stabilization, with scheduled reconciliation reports across migration windows.

3. Source ingestion and content catalog productization
- Turn Spotify sync, YouTube channel resolution, and RSS discovery into first-class application services:
  - `ArtistSyncService`
  - `ChannelDiscoveryService`
  - `VideoDiscoveryService`
  - `CandidateCatalogService`
- Persist source-job runs, failures, retries, and per-source health status.
- Add deterministic dedupe for repeated RSS discoveries and channel rescans.
- Materialize a candidate catalog that frontend can review before downstream pipeline execution.
- This phase is required before enterprise frontend pages can operate on trustworthy upstream data.

4. Workflow modeling, manual review, and versioned BFF integration
- Introduce workflow-oriented services aligned with product flows:
  - `ArtistService`
  - `AuditService`
  - `PipelineService`
  - `LibraryService`
  - `TranslationService`
- Add explicit workflow states for:
  - transcript review
  - taste audit result
  - manual review after taste audit
  - translation review
  - final library approval / curation
- Add versioned BFF APIs from the start:
  - `GET /v1/artists`
  - `GET /v1/audit-queue`
  - `GET /v1/pipeline`
  - `GET /v1/library`
  - `POST /v1/reviews/{review_id}/approve`
  - `POST /v1/reviews/{review_id}/reject`
- Standardize DTOs, pagination, filter semantics, and error model before frontend consumes them.
- Make manual review and curation actions auditable with operator identity, reason, timestamp, and affected record version.

5. Security, governance, and auditability
- Move authn/authz boundary forward to the first versioned BFF release instead of treating it as late hardening.
- Define role model at minimum for:
  - viewer
  - reviewer
  - curator
  - admin
- Add audit logs for review decisions, retry actions, memory promotion, and library approval.
- Define data retention, secret rotation, and error sanitization policies before production-like rollout.

6. Workflow orchestration with RabbitMQ
- Convert long-running steps into asynchronous task consumers only after workflow states, review checkpoints, OSS, and BFF boundaries are stable.
- Define queue topology:
  - `pipeline.command`
  - stage queues: `download`, `transcribe`, `audit`, `manual_review`, `translate`, `translation_review`, `render`
  - `pipeline.dlq`
- Enforce transactional outbox as the only publish path.
- Enforce stage-level idempotency, retry with backoff, and replay-safe processing.

7. Storage and media operations
- Move transient and output media paths from local filesystem assumptions to OSS object keys before multi-worker rollout.
- Persist metadata and object URIs in PostgreSQL.
- Keep local filesystem only for short-lived worker scratch space.
- Define artifact retention, versioning, and cleanup rules for temp and final outputs.

8. Vector memory evolution
- Keep a `VectorRepository` abstraction early, but postpone mandatory Qdrant migration until:
  - review workflow is stable
  - curation actions are explicit
  - retrieval quality baselines exist
- Then migrate from Chroma to Qdrant collections:
  - `translation_memory`
  - `audit_style_memory`
- Use deterministic IDs and parity validation for backfill.

9. API and interface upgrades
- FastAPI public APIs become versioned and job-based:
  - `POST /v1/jobs`
  - `GET /v1/jobs/{job_id}`
  - `GET /v1/jobs/{job_id}/events`
  - `POST /v1/videos/{video_id}/retry`
- Add health/readiness endpoints for DB, RabbitMQ, OSS, and later Qdrant.
- Keep existing legacy behavior behind compatibility wrappers during transition.
- Publish a compatibility matrix for `/create_task`, `/check_status/{task_id}`, `/list_tasks`, including deprecation headers and sunset schedule.

10. Observability and release operations
- Add centralized config, structured logging with correlation IDs, metrics/tracing hooks, and audit logs for key state transitions.
- Create Docker Compose stack for local/prod-like validation: API, worker, Postgres, RabbitMQ, OSS emulator, optional Qdrant.
- Define release-blocking thresholds:
  - queue lag
  - retry ceiling
  - p95 stage latency
  - end-to-end success rate
  - discovery freshness SLA
  - manual review aging SLA

## Frontend Joint Testing Checkpoints
1. After source ingestion and candidate catalog are stable
- Need frontend joint testing for `artists` page.
- Verify list shape, artist sync status, latest candidate videos, empty state, retry state, and pagination/filter contracts.
- Reason: this is the first point where frontend stops depending on mock data and starts depending on real upstream catalog semantics.

2. After manual review states and versioned BFF DTOs are defined
- Need frontend joint testing for `audit queue`.
- Verify pending AI audit items, pending manual review items, approve/reject actions, optimistic refresh, and stale-version conflict handling.
- Reason: manual review is a business checkpoint, so UI action semantics and backend state transitions must match exactly.

3. Before RabbitMQ cut-in
- Need frontend joint testing for `pipeline`.
- Verify job timeline DTOs, stage progress polling or SSE contract, retry/cancel/approve visibility rules, and backward-compatible status mapping.
- Reason: frontend should be stabilized on a workflow contract before execution mode changes from in-process to queue-driven.

4. After OSS media integration
- Need frontend joint testing for `library`.
- Verify final asset retrieval, preview URLs, artifact availability timing, and failure fallback when render/output is not yet ready.
- Reason: once artifact storage changes, UI media access patterns and permission boundaries need confirmation.

5. Before production cutover
- Need end-to-end joint testing across all four screens.
- Verify login/role behavior, audit logging side effects, retry/replay visibility, and that legacy and `/v1` responses remain usable during migration window.
- Reason: this is the final contract check that the product works as one workflow, not just as separate backend modules.

## Public APIs / Interfaces / Types
- Repositories:
  - `ArtistRepository`
  - `VideoRepository`
  - `CandidateRepository`
  - `ReviewRepository`
  - `SubtitleRepository`
  - `JobRepository`
  - `OutboxRepository`
  - `VectorRepository`
- Services:
  - `ArtistSyncService`
  - `ChannelDiscoveryService`
  - `VideoDiscoveryService`
  - `CandidateCatalogService`
  - `PipelineOrchestrator`
  - `ArtistService`
  - `AuditService`
  - `PipelineService`
  - `LibraryService`
  - `TranslationService`
  - `MediaStorageService`
- Shared types:
  - `JobStatus`
  - `StageStatus`
  - `ReviewStatus`
  - `CandidateStatus`
  - `RetryPolicy`
  - `StageExecutionKey`
  - screen-oriented DTOs for `artists`, `audit queue`, `pipeline`, `library`
  - compatibility DTOs for legacy endpoint responses
  - versioned error model and message payload schemas

## Test Plan
1. Unit tests
- DAO tests for CRUD, idempotency, optimistic locking, and state transition guards.
- Service tests for orchestration logic, retry behavior, and failure branching.
- Catalog/discovery tests for source dedupe and candidate freshness.

2. Integration tests
- API + PostgreSQL + RabbitMQ + OSS end-to-end happy path.
- BFF API tests for `artists`, `audit queue`, `pipeline`, `library`.
- Manual review pause/resume tests.
- Duplicate message replay test confirms no duplicate rows or double-processed jobs.
- Partial-failure tests:
  - DB commit succeeds but broker unavailable
  - worker crash mid-stage
  - OSS write fail while transaction data remains recoverable

3. Concurrency and performance tests
- High-concurrency job submission and polling load.
- Worker throughput tests with configurable consumer concurrency.
- Data consistency assertions under concurrent updates and retries.
- Catalog discovery burst tests for repeated artist/channel scans.

4. Migration validation tests
- Dual-write parity checks between old and new stores.
- Backfill correctness for subtitles, candidate records, and vector payloads.
- Cutover rehearsal with rollback drill.
- Continuous parity checks across dual-write windows with documented thresholds.

5. Frontend contract tests
- Screen DTO contract snapshots for `artists`, `audit queue`, `pipeline`, `library`.
- Review-decision contract tests for approve/reject/resume paths.
- Conflict tests for stale client version on review/curation actions.
- Media URL/access contract tests after OSS integration.

## Assumptions and Defaults
- Deployment phase 1 uses **Single VM + Docker**, but structure remains Kubernetes-ready.
- PostgreSQL is the single source of truth for transactional state.
- OSS is introduced before multi-worker asynchronous rollout.
- Qdrant is for retrieval/indexing, not authoritative business records.
- Vector migration is intentionally later than workflow and review stabilization.
- Manual review is a first-class workflow step, not a temporary UI workaround.
- Frontend integration happens only through versioned BFF APIs in `api/`.
- Security and auditability begin with the first real frontend release, not only at final hardening.
- All newly introduced modules must include tests, and CI must block merge on failures.
