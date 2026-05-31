# Phase 9 Legacy Decommission Plan

## Current State

Legacy paths remain available during the Phase 9 rollback window:

- SQLite job repository mode.
- Chroma/SQLite vector source.
- Legacy task APIs:
  - `POST /create_task`
  - `GET /check_status/{task_id}`
  - `GET /list_tasks`

Phase 7 already adds deprecation and sunset headers for legacy task APIs. Phase 9 does not remove them automatically because rollback must remain available during cutover.

## Removal Preconditions

Do not remove legacy paths until all conditions are true:

- Phase 9 cutover report passes.
- Four-screen frontend joint testing passes before and after read-source switch.
- 7-day stability window closes with no critical consistency incident.
- `PHASE9_ROLLBACK_ENABLED=false`.
- Product owner accepts that rollback to legacy reads is no longer required.

## Decommission PR Scope

The dedicated legacy deprecation PR should:

- Remove SQLite/Chroma write paths from active runtime wiring.
- Keep read-only archival tooling if needed for audit/export.
- Disable or remove legacy task APIs.
- Remove legacy compatibility headers once endpoints are gone.
- Remove legacy-only tests or rewrite them against `/v1` APIs.
- Update docs and runbooks to point at PostgreSQL/OSS/Qdrant paths.

## Verification Before Merge

- API baseline uses `/v1` routes, not legacy task routes.
- Phase 9 cutover report remains green.
- Qdrant retrieval baseline remains green.
- Frontend four-screen joint testing remains green.
- No production config references `JOB_REPOSITORY_BACKEND=sqlite` or legacy Chroma runtime paths.
