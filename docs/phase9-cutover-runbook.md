# Phase 9 Cutover Runbook

## Preconditions

- Phase 7 readiness is green for DB, RabbitMQ, OSS, and Qdrant where configured.
- Phase 8 Qdrant backfill/parity and retrieval quality report are passing.
- `PHASE9_SCHEMA_FREEZE_ENABLED=true`.
- `PHASE9_ROLLBACK_ENABLED=true`.
- Legacy reads are still available during the emergency rollback window.

## Evidence Collection

1. Generate legacy and target key snapshots.

Expected JSON shape:

```json
{
  "artists": ["artist-id"],
  "videos": ["video-id"],
  "subtitles": ["video-id:line-index"],
  "jobs": ["job-id"],
  "reviews": ["review-id"],
  "vectors": ["qdrant-point-id"]
}
```

2. Generate or fetch the dual-write report.

```bash
curl -s http://127.0.0.1:8000/internal/phase2/reconcile
```

3. Generate shadow traffic report.

Compare representative legacy and target read paths after normalizing timestamps and transport-only metadata.

4. Run cutover report.

```bash
PYTHONPATH=. .venv/bin/python scripts/phase9_cutover_report.py \
  --legacy-snapshot /path/to/legacy-snapshot.json \
  --target-snapshot /path/to/target-snapshot.json \
  --dual-write-report /path/to/dual-write-report.json \
  --shadow-report /path/to/shadow-report.json \
  --schema-freeze \
  --rollback-enabled
```

The command exits `0` only when all gates pass.

## Read-Source Switch

1. Confirm readiness:

```bash
curl -s http://127.0.0.1:8000/internal/phase9/cutover-readiness
```

2. Switch read source:

```bash
PHASE9_CUTOVER_READ_SOURCE=postgres
```

3. Keep rollback enabled:

```bash
PHASE9_ROLLBACK_ENABLED=true
```

4. Run frontend joint testing across:

- `/artists`
- `/queue`
- `/pipeline`
- `/library`

## Rollback

If critical consistency or user-visible workflow regression is detected:

```bash
PHASE9_CUTOVER_READ_SOURCE=legacy
PHASE9_ROLLBACK_ENABLED=true
```

Then rerun the cutover report and inspect failed gates.

## Decommission

Only after the stability window closes without critical consistency incidents:

- Set `PHASE9_ROLLBACK_ENABLED=false`.
- Remove SQLite/Chroma write paths.
- Remove or hard-disable legacy task APIs in the dedicated legacy deprecation PR.
