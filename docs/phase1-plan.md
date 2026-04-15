# Phase 1 Implementation Plan

## Purpose
Turn the current Phase 1 foundation branch into a mergeable Phase 1 PR that satisfies the roadmap goal:

- introduce layered architecture boundaries
- preserve existing external API behavior
- keep Phase 0 guardrails intact

This plan is based on:

- the definition of Phase 1 in `docs/roadmap.md`
- the current contents of `feature/phase1-foundation-snapshot`
- the Phase 0 review lessons learned

## Phase 1 Objective
Refactor the project into clear architectural layers without changing external behavior.

Target layers:

- `api/`: route handlers and request/response contracts
- `application/`: orchestration and use-case services
- `domain/`: entities, enums, and repository/storage contracts
- `infrastructure/`: adapters for persistence, storage, and legacy producer integration

## Current Branch Snapshot
The branch already contains a strong starting point:

- `application/services/job_service.py`
- `application/services/pipeline_orchestrator.py`
- `domain/entities.py`
- `domain/enums.py`
- `domain/repositories.py`
- `domain/storage.py`
- `infrastructure/persistence/in_memory_job_repository.py`
- `infrastructure/persistence/sqlite_repositories.py`
- `infrastructure/pipeline/legacy_producer_adapter.py`
- `infrastructure/storage/local_media_storage.py`
- `test/test_phase1_layered_architecture.py`

The main gap is that the live FastAPI app is still using the legacy global-state implementation in `api/service.py`.

## Review-Based Adjustments
The Phase 0 review highlighted a few rules we should carry into Phase 1:

1. Do not let documentation claim behavior that the app does not actually enforce.
2. Do not introduce architecture changes without wiring them into real runtime flow and tests.
3. Keep CI aligned with the true scope of the phase.
4. Preserve Phase 0 startup guardrails while restructuring internals.

## Scope For Phase 1
### In Scope
- wire `api/service.py` to the layered architecture
- preserve the current endpoint contracts:
  - `POST /create_task`
  - `GET /check_status/{task_id}`
  - `GET /list_tasks`
- replace direct global task-state mutation with repository-backed services
- keep local filesystem storage as the temporary media adapter
- keep legacy producer logic behind an adapter boundary
- expand tests so Phase 1 proves no behavior regression

### Out of Scope
- PostgreSQL migration
- Alembic
- RabbitMQ
- Qdrant
- OSS/S3 media storage
- API versioning redesign
- changing public endpoint behavior beyond compatibility-preserving internal refactor

## Delivery Plan
### Step 1: Sync Phase 1 With Master
- rebase or merge `master` into `feature/phase1-foundation-snapshot`
- preserve the merged Phase 0 files:
  - `.github/workflows/ci.yml`
  - `api/config.py`
  - startup guardrail tests
  - logger portability/singleton fix

Exit check:
- branch contains both Phase 0 guardrails and Phase 1 foundation files

### Step 2: Finalize Domain and Contract Boundaries
- confirm `Job`, `JobStatus`, repository interfaces, and `MediaStorageService` are the canonical Phase 1 contracts
- keep names stable unless there is a strong reason to rename now
- ensure repository method naming is consistent across in-memory and SQLite adapters
- keep this step intentionally short and practical: stabilize only what is needed before wiring the live API
- defer deeper contract redesign unless Step 3 exposes a real integration problem

Exit check:
- domain and interface modules are stable enough to wire into the API without churn

### Step 3: Wire FastAPI Into Application Services
- refactor `api/service.py` to construct:
  - `InMemoryJobRepository`
  - `JobService`
  - `LocalFilesystemMediaStorage`
  - `create_default_producer_backend()`
  - `PipelineOrchestrator`
- keep startup env validation from Phase 0
- keep endpoint response shapes unchanged
- remove legacy direct task-state handling from the API layer

Exit check:
- API endpoints use the layered services, not legacy global state
- startup guardrails still work

### Step 4: Preserve Baseline API Behavior
- restore API contract tests for:
  - create task response contract
  - check status success/not-found behavior
  - list tasks behavior
- adapt the tests so they patch layered dependencies rather than legacy globals
- ensure old user-visible response fields stay stable

Exit check:
- Phase 0 and Phase 1 API behavior remains compatible from the client’s point of view

### Step 5: Strengthen Adapter Tests
- keep `test_phase1_layered_architecture.py`
- add focused tests for:
  - `InMemoryJobRepository`
  - `SQLiteJobRepository`
  - `LocalFilesystemMediaStorage`
  - `MissingProducerBackend`
  - orchestrator failure and cleanup behavior

Exit check:
- new architecture pieces have direct unit coverage, not just one broad integration-style test

### Step 6: Update CI For Phase 1
- keep the Phase 0 checks:
  - config validation
  - env template coverage
  - startup guardrail
- add Phase 1 checks for:
  - layered architecture tests
  - API compatibility tests on top of the new layering
- keep CI descriptions accurate to what is actually executed

Exit check:
- CI proves both safety and behavior preservation

### Step 7: Prepare The PR
- update docs if any contract or architecture detail changed during implementation
- summarize boundaries clearly in the PR:
  - what moved into `application/`
  - what moved into `domain/`
  - what moved into `infrastructure/`
  - what behavior was intentionally kept unchanged

Exit check:
- PR is easy to review as “internal restructuring with compatibility preserved”

## Recommended File Sequence
To reduce risk, implement in this order:

1. quick Step 2 review across `domain/`, `application/`, and `infrastructure/` contracts
2. `api/service.py`
3. API compatibility tests
4. small fixes in `application/`, `domain/`, and `infrastructure/`
5. adapter-specific tests
6. CI updates
7. docs touch-up

## Risks To Watch
### Behavior Drift
Risk:
- API responses or task lifecycle messages accidentally change during the refactor

Mitigation:
- add baseline-style API contract tests before or during API rewiring

### Legacy Producer Coupling
Risk:
- the producer object assumptions in old code may not match the new adapter boundary exactly

Mitigation:
- keep a narrow adapter seam and test both happy path and failure path

### CI Drift
Risk:
- workflow names or job names stop matching what the tests actually do

Mitigation:
- update CI and docs together in the same change set

### Branch Scope Creep
Risk:
- PostgreSQL/RabbitMQ/Qdrant work leaks into Phase 1

Mitigation:
- explicitly keep all infrastructure migration work out of this PR

## Definition of Done
Phase 1 is complete when:

- the FastAPI app is wired to the layered architecture
- endpoint behavior remains compatible with the pre-Phase-1 API
- Phase 0 startup/config guardrails still pass
- Phase 1 layered architecture tests pass
- CI reflects the real test surface
- the PR contains restructuring only, not Phase 2+ infrastructure changes

## Immediate Next Action
Run a short Step 2 stabilization pass first, then implement the API integration:

- confirm the current contracts are good enough for live API wiring
- rewire `api/service.py` to use `JobService`, `PipelineOrchestrator`, `InMemoryJobRepository`, `LocalFilesystemMediaStorage`, and the producer adapter
- then restore API contract tests against the new layered runtime

This keeps Step 2 respected, but prevents it from turning into a long abstract design pass before the real integration work starts.
