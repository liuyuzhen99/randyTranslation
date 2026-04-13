# RandyTranslation Enterprise Upgrade Roadmap

## Goal
Upgrade the current project to an enterprise-grade architecture that is scalable, consistent, and extensible, using:
- FastAPI + DAO + SQLAlchemy + RabbitMQ
- PostgreSQL + Qdrant
- S3-compatible OSS for media artifacts

This roadmap is phased, implementation-ready, and includes phase exit criteria to keep delivery reliable.

## Global Delivery Rules (Apply to Every Phase)
- No phase is considered complete without tests.
- All new modules must include unit tests.
- Integration tests must pass in Docker Compose before advancing.
- Backward compatibility must be preserved until Phase 7 cutover.
- Every migration step must have rollback instructions documented.

---

## Phase 0: Baseline and Safety Setup
### Objective
Establish a stable baseline and safety controls before architecture changes.

### Tasks
1. Freeze current behavior
- Tag current code snapshot in git.
- Capture existing API behavior (`/create_task`, `/check_status`, `/list_tasks`) in baseline test cases.

2. Add engineering guardrails
- Add CI pipeline with at least: lint, unit tests, integration test trigger.
- Define branch strategy: `main` + feature branches + PR checks required.

3. Add environment templates
- Create `.env.example` with all current and target infra variables.
- Add config validation at startup (fail fast if required vars are missing).

### Deliverables
- Baseline tag and changelog entry.
- CI checks running on pull requests.
- Startup config validation.

### Exit Criteria
- Existing behavior reproducible through automated tests.
- CI is mandatory for merge.

---

## Phase 1: Project Restructure to Layered Architecture
### Objective
Refactor into clear boundaries without changing external behavior.

### Tasks
1. Create target module structure
- `api/` (routes and request/response contracts)
- `application/` (use cases/services)
- `domain/` (entities, enums, business rules)
- `infrastructure/` (db, queue, vector, storage adapters)

2. Introduce DAO interfaces
- Define repository contracts for artists, videos, subtitles, jobs, outbox, vectors.
- Keep current SQLite logic behind temporary adapters.
- Define `MediaStorageService` interface and local filesystem adapter so pipeline code no longer depends on hardcoded paths.

3. Move orchestration logic
- Extract pipeline orchestration from ad-hoc functions into application services.
- Replace direct global state mutation with service methods.

### Deliverables
- New layered skeleton with dependency boundaries.
- DAO interfaces and temporary adapters compiling and working.

### Exit Criteria
- Existing API behavior still passes baseline tests.
- New architecture lint/type checks pass.

---

## Phase 2: PostgreSQL Foundation (Source of Truth)
### Objective
Introduce PostgreSQL + SQLAlchemy + Alembic for transactional consistency.

### Tasks
1. Define relational schema in SQLAlchemy
- Core tables: `artists`, `videos`, `subtitles`, `jobs`, `job_events`, `outbox`.
- Add required constraints:
  - unique IDs for idempotency
  - business-key uniqueness (e.g., `subtitles(video_id, line_index)`, outbox event ID, stage dedupe key)
  - foreign keys
  - non-null critical fields
  - status enums/check constraints

2. Define canonical lifecycle state machine
- Define `JobStatus` + `StageStatus`, allowed transitions, terminal states, retry semantics, and invalid-transition handling.
- Persist transition guards with DB-level checks and repository/service validation.

3. Add performance indexes
- Status index on `jobs` and `videos`.
- Lookup indexes for `video_id`, `spotify_id`, and event correlation keys.

4. Add migration management
- Initialize Alembic.
- Create initial migration and verified downgrade path.

5. Implement PostgreSQL DAOs
- Replace SQLite-specific SQL with SQLAlchemy repositories.

6. Start shadow-write and reconciliation (early dual-write)
- Start writing selected flows to PostgreSQL in shadow mode while legacy SQLite remains authoritative.
- Run continuous reconciliation reports (counts + key checks + sample payload parity).

### Deliverables
- Running PostgreSQL schema managed by Alembic.
- PostgreSQL DAO implementations and tests.

### Exit Criteria
- CRUD + transaction tests pass for all core repositories.
- Migration up/down works in clean environment.
- Transition rules are enforced through DB constraints/tests and invalid transitions are rejected.
- Shadow-write reconciliation passes for selected flows with documented variance thresholds.

---

## Phase 3: RabbitMQ Async Pipeline and Idempotency
### Objective
Replace in-process task queue/thread model with reliable message-driven processing.

### Tasks
1. Define queue topology
- `pipeline.command`
- stage queues: `download`, `transcribe`, `audit`, `translate`, `render`
- dead-letter queue: `pipeline.dlq`

2. Implement message contracts
- Define strict event schema (`job_id`, `video_id`, stage, retry_count, trace_id, timestamp).
- Version message schema for future compatibility.

3. Build producer/consumer components
- Publisher in API/application layer.
- Worker consumers per stage with explicit ack/nack behavior.
- Require transactional outbox pattern (`DB transaction + outbox record + dispatcher`) as the only publish path.

4. Add idempotency controls
- Dedupe key checks in PostgreSQL before stage execution.
- Safe reprocessing when duplicate messages arrive.

5. Add retry strategy
- Exponential backoff.
- Max retry + DLQ routing.

### Deliverables
- RabbitMQ-driven stage orchestration in Docker Compose.
- End-to-end async flow for one job.

### Exit Criteria
- Replay/duplicate tests prove no double-processing side effects.
- Failed jobs route to DLQ and can be replayed safely.
- Crash-recovery tests prove no lost or phantom events (including crash-before-publish and crash-after-publish cases).

---

## Phase 4: Qdrant Migration (Vector Memory)
### Objective
Migrate vector workloads from ChromaDB to Qdrant without quality loss.

### Tasks
1. Design Qdrant collections
- `translation_memory`
- `audit_style_memory`
- Payload schema: `video_id`, `artist`, source type, quality tags, created_at.

2. Implement vector adapter
- Upsert/search APIs with consistent abstraction via `VectorRepository`.
- Keep feature parity with current retrieval use cases.

3. Build migration/backfill job
- Read existing vector sources (Chroma + DB-linked data).
- Write into Qdrant with deterministic IDs.
- Start vector shadow-write after PostgreSQL stabilization and run continuous parity checks during migration window.

4. Validate retrieval parity
- Run side-by-side query comparison on representative dataset.

### Deliverables
- Qdrant repositories and migration scripts.
- Query parity report.

### Exit Criteria
- Retrieval quality accepted by defined baseline checks.
- Backfill is repeatable and idempotent.

---

## Phase 5: OSS Media Storage Integration
### Objective
Move media artifacts from local absolute paths to durable object storage.

### Tasks
1. Implement media storage service
- Upload/download/delete and pre-signed URL support.
- Standard object key strategy (`env/project/date/job_id/...`).

2. Update pipeline outputs
- Store only URIs/keys in PostgreSQL.
- Keep local temp files ephemeral.

3. Add lifecycle policies
- Temp artifact retention rules.
- Final artifact retention and versioning policy.

### Deliverables
- OSS adapter integrated with pipeline.
- DB stores metadata + object keys only.

### Exit Criteria
- End-to-end job produces output fully retrievable from OSS.
- No production dependency on hardcoded local paths.

---

## Phase 6: API v1, Observability, and Ops Hardening
### Objective
Ship production-grade API contracts and operational controls.

### Tasks
1. Implement v1 APIs
- `POST /v1/jobs`
- `GET /v1/jobs/{job_id}`
- `GET /v1/jobs/{job_id}/events`
- `POST /v1/videos/{video_id}/retry`

2. Add health/readiness probes
- DB, RabbitMQ, Qdrant, OSS checks.

3. Add observability
- Structured logs with correlation IDs.
- Metrics: queue depth, stage latency, success/failure rate, retry count.
- Basic tracing across API -> queue -> worker.
- Define release-blocking thresholds (queue lag, retry ceiling, p95 stage latency, error rate).

4. Add operational runbooks
- Incident response for queue backlog, DB contention, vector outages.
- DLQ replay process.

5. Add compatibility and security controls
- Publish legacy compatibility contract mapping `/create_task`, `/check_status/{task_id}`, `/list_tasks` to `/v1/*` with response/status/error mapping.
- Add deprecation headers and sunset milestone for legacy endpoints.
- Define minimum security baseline: authn/authz boundary, rate-limit policy, secret rotation policy, and data retention rules.

### Deliverables
- Stable v1 API with docs.
- Monitoring dashboards and runbooks.

### Exit Criteria
- SLO-aligned smoke/perf tests pass in staging.
- On-call runbook tested with at least one simulated failure drill.
- Legacy compatibility contract tests pass for response/status/error mapping.
- Security baseline controls are implemented and verified.

---

## Phase 7: Dual-Write, Validation, and Cutover
### Objective
Execute low-risk migration from old stack to new stack.

### Tasks
1. Promote dual-write to cutover readiness
- Use already-running dual-write/shadow-write evidence from earlier phases as cutover gate.
- Freeze schema/interface changes during final cutover window.

2. Reconciliation checks
- Compare counts and key consistency:
  - artists/videos/subtitles/jobs
  - vector point counts and sample retrieval results

3. Shadow traffic validation
- Run new pipeline path without user-visible cutover.
- Compare latency, success rate, and output consistency.

4. Production cutover
- Switch reads to PostgreSQL/Qdrant.
- Keep fallback switch for emergency rollback window.

5. Decommission legacy paths
- Remove SQLite/Chroma write paths after stability window.

### Deliverables
- Cutover report with parity evidence.
- Legacy deprecation PR.

### Exit Criteria
- 7-day stability window with no critical data-consistency incident.
- Rollback no longer required; legacy paths removed.

---

## Reliability Checklist (Must Pass Before Production)
1. Data consistency
- Exactly-once effect at business level for each stage outcome.
- No orphan job records or missing stage events.

2. Concurrency
- Target concurrent submissions handled without queue collapse.
- DB locking/contention remains within acceptable threshold.

3. Recovery
- Worker crash recovery verified.
- Broker interruption recovery verified.
- Qdrant/OSS transient failure retry verified.

4. Security and governance
- Secrets only via env/secret manager.
- API input validation and error sanitization.
- Audit log coverage for critical state transitions.

## Suggested Execution Order by Milestone
1. M1: Phase 0-1 complete
2. M2: Phase 2 complete
3. M3: Phase 3 complete
4. M4: Phase 4-5 complete
5. M5: Phase 6 complete
6. M6: Phase 7 cutover complete

## Notes for Implementation Start
- Start implementation from Phase 0 and do not skip phase gates.
- If any exit criteria fails, fix within the same phase before moving forward.
- Keep feature development minimal during Phase 2-7 to reduce migration risk.
