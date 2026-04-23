# RandyTranslation Enterprise Upgrade Roadmap

## Goal

Upgrade the project into an enterprise-grade application that is scalable, consistent, auditable, and product-ready, using:

- FastAPI + DAO + SQLAlchemy + RabbitMQ
- PostgreSQL as transactional source of truth
- S3-compatible OSS for media artifacts
- Qdrant later for curated vector retrieval

The target product flow is:

- sync followed artists from Spotify
- resolve YouTube channel IDs
- fetch latest official MV candidates from YouTube/RSS
- dedupe and manage candidate catalog
- transcribe lyrics/audio
- AI review transcript
- AI audit track fit/taste with RAG support
- optional manual review after taste audit
- AI translate lyrics to Chinese with translation-memory/RAG support
- AI review translated output
- download video
- burn bilingual subtitles with ffmpeg
- curate accepted outputs into library and memory systems

The target user-facing surfaces are:

- `artists`
- `audit queue`
- `pipeline`
- `library`

This means the roadmap must optimize not only for backend scalability, but also for:

- source ingestion reliability
- screen-oriented BFF APIs
- manual review insertion points
- auditable curation loops
- low-risk frontend/backend joint delivery

## Global Delivery Rules

- No phase is complete without tests.
- All new modules must include unit tests.
- Integration tests must pass in Docker Compose before advancing.
- Every migration step must have rollback instructions documented.
- Frontend integration happens only through versioned BFF APIs in `api/`.
- Manual review steps must be represented explicitly in domain state and API contracts before queue/worker fan-out is considered complete.
- RAG write-back actions must be modeled as explicit workflow outcomes, not hidden side effects.
- Security, permissions, and audit logging start with the first real frontend release, not only at final hardening.
- Legacy compatibility must be preserved until final cutover is complete.

---

## Phase 0: Baseline and Safety Setup

### Objective

Establish a stable baseline and safety controls before architecture changes.

### Tasks

1. Freeze current behavior

- Tag current code snapshot in git.
- Capture existing API behavior (`/create_task`, `/check_status`, `/list_tasks`) in baseline tests.

2. Add engineering guardrails

- Add CI pipeline with at least lint, unit tests, and integration test trigger.
- Define branch strategy and PR protection rules.

3. Add environment templates

- Create `.env.example` with current and target infra variables.
- Add startup config validation.

### Deliverables

- Baseline tag and changelog entry.
- CI checks on pull requests.
- Startup config validation.

### Exit Criteria

- Existing behavior reproducible through automated tests.
- CI is mandatory for merge.

### Frontend Joint Testing

- Not required yet.
- Frontend can continue to use mocks or prototypes because backend contracts are not yet stable.

---

## Phase 1: Project Restructure to Layered Architecture

### Objective

Refactor into clear boundaries without changing external behavior.

### Tasks

1. Create target module structure

- `api/`
- `application/`
- `domain/`
- `infrastructure/`

2. Introduce repository and storage interfaces

- Define contracts for artists, videos, subtitles, jobs, outbox, vectors, and media storage.
- Keep SQLite/local implementations behind temporary adapters.

3. Move orchestration logic

- Extract pipeline orchestration from ad-hoc functions into application services.
- Replace process-local mutable state with service methods and repository calls.

### Deliverables

- Layered skeleton with dependency boundaries.
- Temporary adapters compiling and working.

### Exit Criteria

- Existing API behavior still passes baseline tests.
- New architecture lint/type checks pass.

### Frontend Joint Testing

- Not required yet.
- No stable BFF contract exists at this point.

---

## Phase 2: PostgreSQL Foundation and Dual-Write Base

### Objective

Introduce PostgreSQL + SQLAlchemy + Alembic for transactional consistency.

### Tasks

1. Define relational schema in SQLAlchemy

- Core tables: `artists`, `videos`, `subtitles`, `jobs`, `job_events`, `outbox`.
- Add unique keys, foreign keys, non-null critical fields, status constraints, and core indexes.

2. Define canonical lifecycle state machine

- Define `JobStatus`, `StageStatus`, retry semantics, and invalid-transition handling.
- Enforce transition rules through service/repository checks and tests.

3. Add migration management

- Initialize Alembic.
- Create initial migration and verified downgrade path.

4. Implement PostgreSQL repositories

- Replace SQLite-specific logic with SQLAlchemy repositories.

5. Start shadow-write and reconciliation

- Write selected flows to PostgreSQL in shadow mode while legacy remains authoritative.
- Run reconciliation reports with thresholds.

### Deliverables

- Running PostgreSQL schema managed by Alembic.
- PostgreSQL repository implementations and tests.
- Shadow-write and reconciliation foundation.

### Exit Criteria

- CRUD + transaction tests pass for core repositories.
- Migration up/down works in clean environment.
- Invalid transitions are rejected.
- Shadow-write reconciliation passes for selected flows.

### Frontend Joint Testing

- Not required yet.
- Database migration alone should not change frontend-visible behavior.

---

## Phase 3: Source Ingestion and Candidate Catalog

### Objective

Turn Spotify sync, YouTube channel discovery, and RSS scanning into a reliable productized upstream catalog.

### Tasks

1. Productize ingestion services

- Introduce `ArtistSyncService`, `ChannelDiscoveryService`, `VideoDiscoveryService`, `CandidateCatalogService`.
- Persist sync runs, retry state, source health, and failure reasons.

2. Model candidate catalog

- Add candidate records with dedupe rules and ingestion status.
- Preserve source traceability from Spotify artist to YouTube channel to discovered video.

3. Add catalog-facing BFF endpoints

- `GET /v1/artists`
- `GET /v1/artists/{artist_id}/candidates`
- `POST /v1/artists/{artist_id}/resync`

4. Define DTO and pagination contract

- Standardize list filters, sort order, empty state, partial-failure state, and retry metadata.

### Deliverables

- Reliable upstream content catalog.
- First versioned BFF contract for `artists`.

### Exit Criteria

- Artist sync, channel resolution, and RSS discovery are persisted and retryable.
- Candidate dedupe works under repeated scans.
- `artists` screen can consume real backend data without leaking repository internals.

### Frontend Joint Testing

- Required with the `artists` page.
- Verify:
  - artist list shape
  - sync status and timestamps
  - candidate video list
  - pagination/filter behavior
  - retry/resync button semantics
  - partial-failure and empty states
- Why here:
  - this is the first point where frontend stops relying on mock catalog data and starts relying on real upstream workflow semantics

---

## Phase 4: Workflow Modeling, Manual Review, and Secure BFF

### Objective

Build product-facing workflow states, explicit review checkpoints, and secure versioned BFF APIs.

### Tasks

1. Define workflow-oriented domain services

- `ArtistService`
- `AuditService`
- `PipelineService`
- `LibraryService`
- `TranslationService`

2. Model review checkpoints explicitly

- transcript review
- taste audit result
- manual review after taste audit
- translation review
- final asset approval / library curation

3. Add secure versioned BFF endpoints

- `GET /v1/audit-queue`
- `GET /v1/pipeline`
- `GET /v1/library`
- `POST /v1/reviews/{review_id}/approve`
- `POST /v1/reviews/{review_id}/reject`

4. Add security and auditability

- audit logs for decisions, retries, and promotions
- stale-version conflict handling for review actions

5. Define frontend update strategy

- Decide polling vs SSE for each screen.
- Standardize error model and response envelopes.

### Deliverables

- Manual review state model and transitions.
- Secure BFF APIs for `audit queue`, `pipeline`, and `library`.
- Audit trail for human decisions.

### Exit Criteria

- Manual review can pause/resume workflow safely.
- Review and curation actions are persisted and auditable.
- Frontend screens can be served through stable `/v1` BFF APIs.

### Frontend Joint Testing

- Required with `audit queue` and initial `pipeline` views.
- Verify:
  - pending AI audit items
  - pending manual review items
  - approve/reject flows
  - stale client version conflict behavior
  - permission-based visibility
  - pipeline stage progress contract
- Why here:
  - review is the first real business decision point, so backend state transitions and UI actions must be validated together

---

## Phase 5: OSS Media Storage and Artifact Delivery

### Objective

Move media artifacts from local paths to durable object storage before full async worker rollout.

### Tasks

1. Implement media storage service

- Upload/download/delete and pre-signed URL support.
- Standard object key strategy.

2. Update pipeline outputs

- Store metadata and object URIs in PostgreSQL.
- Keep local temp files ephemeral.

3. Add lifecycle policies

- Temp retention rules.
- Final artifact retention and versioning.

### Deliverables

- OSS adapter integrated with pipeline.
- Artifact metadata stored in PostgreSQL.

### Exit Criteria

- End-to-end job produces outputs retrievable from OSS.
- No production dependency on hardcoded local paths.

### Frontend Joint Testing

- Required with `library` and media preview flows.
- Verify:
  - preview/playback URL behavior
  - artifact readiness timing
  - expired URL refresh behavior
  - missing or failed artifact fallback state
- Why here:
  - artifact storage changes directly affect how frontend previews and downloads media

---

## Phase 6: RabbitMQ Async Pipeline and Idempotency

### Objective

Replace in-process execution with reliable message-driven processing once workflow contracts and media storage are stable.

### Tasks

1. Define queue topology

- `pipeline.command`
- stage queues: `download`, `transcribe`, `audit`, `manual_review`, `translate`, `translation_review`, `render`
- `pipeline.dlq`

2. Implement message contracts

- Versioned event schema with job, stage, retry, trace, and review context.

3. Build producer/consumer components

- Publisher in API/application layer.
- Worker consumers per stage with explicit ack/nack behavior.
- Transactional outbox as the only publish path.

4. Add idempotency and retry controls

- Dedupe keys in PostgreSQL before stage execution.
- Exponential backoff and DLQ routing.

### Deliverables

- RabbitMQ-driven stage orchestration in Docker Compose.
- End-to-end async flow for one job including review gates.

### Exit Criteria

- Replay tests prove no double-processing side effects.
- Failed jobs route to DLQ and can be replayed safely.
- Crash-recovery tests prove no lost or phantom events.

### Frontend Joint Testing

- Required with `pipeline`.
- Verify:
  - polling or SSE update cadence
  - stage transitions under async processing
  - retry/replay visibility
  - no duplicated timeline items after message replay
- Why here:
  - execution mode changes, but frontend contract should remain stable; joint testing confirms the contract survived the infrastructure change

---

## Phase 7: Observability, Compatibility, and Ops Hardening

### Objective

Ship production-grade operational controls and compatibility guarantees.

### Tasks

1. Add health/readiness probes

- DB, RabbitMQ, OSS, and optional Qdrant checks.

2. Add observability

- Structured logs with correlation IDs.
- Metrics for queue depth, stage latency, success/failure rate, retry count, discovery freshness, and review aging.
- Basic tracing across API -> queue -> worker.

3. Add operational runbooks

- Incident response for queue backlog, DB contention, OSS outages, and replay handling.

4. Add compatibility controls

- Publish legacy compatibility mapping for `/create_task`, `/check_status/{task_id}`, `/list_tasks`.
- Add deprecation headers and sunset milestone.

5. Run staging drills

- Failure simulation, rollback rehearsal, and operational smoke tests.

### Deliverables

- Monitoring dashboards and runbooks.
- Legacy compatibility documentation and tests.

### Exit Criteria

- SLO-aligned smoke/perf tests pass in staging.
- Runbook tested with at least one simulated failure drill.
- Legacy compatibility contract tests pass.
- Security and audit baseline are verified.

### Frontend Joint Testing

- Required for cross-screen regression.
- Verify:
  - all four screens under staging conditions
  - degraded-state messaging
  - legacy-to-v1 compatibility during migration period
  - permission and audit-log side effects remain correct
- Why here:
  - this is the last safe point to catch operational contract drift before cutover

---

## Phase 8: Qdrant Migration and Retrieval Quality Validation

### Objective

Migrate vector workloads only after workflow, curation, and feedback loops are stable enough to define quality baselines.

### Tasks

1. Design Qdrant collections

- `translation_memory`
- `audit_style_memory`

2. Implement vector adapter

- `VectorRepository` abstraction with Qdrant implementation.

3. Build migration/backfill job

- Read existing vector sources.
- Write deterministic IDs into Qdrant.
- Run parity checks during migration window.

4. Validate retrieval quality

- Run side-by-side evaluation on curated representative datasets.

### Deliverables

- Qdrant repositories and migration scripts.
- Retrieval parity and quality report.

### Exit Criteria

- Retrieval quality accepted by defined baseline checks.
- Backfill is repeatable and idempotent.

### Frontend Joint Testing

- Usually not required for first pass.
- Required only if frontend exposes memory promotion status, retrieval explanations, or curator-facing memory management UI.

---

## Phase 9: Dual-Write Validation and Final Cutover

### Objective

Execute low-risk migration from old stack to new stack.

### Tasks

1. Promote dual-write to cutover readiness

- Use already-running dual-write evidence from earlier phases as cutover gate.
- Freeze schema/interface changes during final cutover window.

2. Reconciliation checks

- Compare counts and key consistency for artists, videos, subtitles, jobs, reviews, and vector points where applicable.

3. Shadow traffic validation

- Run new pipeline path without user-visible cutover.
- Compare latency, success rate, output consistency, and UI-level workflow integrity.

4. Production cutover

- Switch reads to PostgreSQL/OSS/Qdrant where applicable.
- Keep rollback switch for emergency window.

5. Decommission legacy paths

- Remove SQLite/Chroma write paths after stability window.

### Deliverables

- Cutover report with parity evidence.
- Legacy deprecation PR.

### Exit Criteria

- 7-day stability window with no critical data-consistency incident.
- Rollback no longer required.
- Legacy paths removed.

### Frontend Joint Testing

- Required end-to-end across all four screens before and after cutover.
- Verify:
  - real login/role behavior
  - end-to-end content flow from artist discovery to library
  - review decision persistence
  - artifact preview and playback
  - no user-visible regression during read-source switch
- Why here:
  - final cutover must be validated as one product workflow, not as isolated backend features

---

## Reliability Checklist

1. Data consistency

- Exactly-once effect at business level for each stage outcome.
- No orphan job records, review records, or missing stage events.

2. Concurrency

- Target concurrent submissions handled without queue collapse.
- DB locking/contention remains within threshold.

3. Recovery

- Worker crash recovery verified.
- Broker interruption recovery verified.
- OSS transient failure retry verified.

4. Security and governance

- Secrets only via env/secret manager.
- API input validation and error sanitization.
- Audit log coverage for critical state transitions and review decisions.

5. Product value

- Candidate discovery freshness meets SLA.
- Manual review backlog stays within target aging threshold.
- End-to-end job success rate and processing cost are observable.

## Suggested Execution Order by Milestone

1. M1: Phase 0-1 complete
2. M2: Phase 2 complete
3. M3: Phase 3 complete
4. M4: Phase 4 complete
5. M5: Phase 5-6 complete
6. M6: Phase 7 complete
7. M7: Phase 8 complete
8. M8: Phase 9 cutover complete

## Notes for Implementation Start

- Start from Phase 0 and do not skip phase gates.
- If any exit criteria fails, fix within the same phase before moving forward.
- Keep feature development minimal during Phase 2-9 to reduce migration risk.
- Frontend joint testing should be scheduled as part of phase exit criteria, not as an afterthought after backend merge.
