# Artist Resync Consistency Update - 2026-05-20

## Summary

This update hardens `CandidateCatalogService.resync_artist()` against two runtime consistency risks:

- concurrent resync calls for the same artist overwriting each other
- crashed or abandoned syncs leaving an artist stuck in `processing`

The implementation keeps the existing short-transaction model. It does not wrap external provider calls in a long database transaction, and it intentionally preserves the existing behavior where a later failure can mark the artist sync as `failed` even if an earlier phase partially succeeded.

## Behavior

Artist resync now uses a lightweight ownership token based on `last_sync_started_at`.

At the start of a resync, the service calls:

```text
ArtistRepository.try_begin_sync(spotify_id, started_at, stale_before)
```

This atomically moves the artist into `processing` only when either:

- the artist is not already `processing`
- `last_sync_started_at` is missing
- the existing `processing` state is older than the stale threshold

If another fresh sync is already running, `resync_artist()` raises:

```text
Artist sync already in progress
```

The existing API behavior maps that error to HTTP `409`.

At successful or failed completion, the service calls:

```text
ArtistRepository.try_finish_sync(spotify_id, started_at, finished_artist)
```

The finish update only applies when the row is still `processing` with the same `last_sync_started_at` token. If another resync has taken over, the old run cannot overwrite the newer artist state.

## Configuration

Added runtime setting:

```text
ARTIST_SYNC_STALE_AFTER_SECONDS=1800
```

The default stale threshold is 30 minutes. Values below 1 second are rejected during settings validation.

## Repository Changes

`ArtistRepository` now exposes two atomic sync methods:

```text
try_begin_sync(spotify_id, started_at, stale_before) -> Artist | None
try_finish_sync(spotify_id, started_at, finished_artist) -> bool
```

`SQLAlchemyArtistRepository` implements these with conditional `UPDATE` statements.

`SQLiteArtistRepository` was updated to satisfy the expanded interface and persist the artist sync fields used by the consistency checks.

## Transaction Notes

The resync flow remains intentionally split across short transactions:

- begin sync ownership
- create/update channel sync run
- create/update candidate discovery run and candidates
- finish artist status

This keeps `processing` visible to readers, avoids holding database locks across provider I/O, and preserves diagnostic run records when later work fails.

The consistency guarantee is scoped to the final artist status row. Intermediate `artist_sync_runs` and discovered candidates are still allowed to persist if an older run loses ownership before finishing.

## Verification

Targeted tests cover:

- successful resync still writes `completed`
- provider failure writes `failed` and `last_sync_error`
- active `processing` artists reject a second resync
- stale `processing` artists can be taken over
- stale finish tokens cannot overwrite newer `processing` state
- API returns `409` for in-progress resync

Commands run:

```text
.venv/bin/python -m py_compile domain/repositories.py infrastructure/persistence/sqlalchemy_repositories.py infrastructure/persistence/sqlite_repositories.py application/services/phase3_catalog_service.py api/config.py test/test_phase3_catalog.py
.venv/bin/python -m unittest discover -s test -p 'test_phase3_catalog.py' -k 'resync' -k 'finish_sync'
.venv/bin/python -m unittest discover -s test -p 'test_phase1_layered_architecture.py'
git diff --check
```

Full `test_phase3_catalog.py` still hits the pre-existing SQLite Alembic migration issue:

```text
ALTER TABLE artist_sync_runs ALTER COLUMN status TYPE VARCHAR(10)
```

SQLite does not support that statement shape. This is unrelated to the resync consistency change.
