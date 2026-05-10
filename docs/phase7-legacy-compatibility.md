# Phase 7 Legacy Compatibility

Phase 7 keeps the legacy task APIs available during the migration window while making their deprecation explicit.

## Deprecation Headers

Legacy endpoints return:

- `Deprecation: true`
- `Sunset: Thu, 31 Dec 2026 00:00:00 GMT`
- `Link: <...>; rel="successor-version", </docs/phase7-legacy-compatibility.md>; rel="deprecation"`

## Endpoint Mapping

| Legacy endpoint | Compatibility behavior | Successor |
| --- | --- | --- |
| `POST /create_task` | Creates a job and, when Phase 6 async mode is enabled, writes the first pipeline command to the outbox. `candidate_id` is accepted for migration callers. | `POST /v1/candidates/{candidate_id}/render` |
| `GET /check_status/{task_id}` | Returns the legacy job DTO from the configured job repository. | `GET /v1/pipeline` |
| `GET /list_tasks` | Returns the legacy task map keyed by task id. | `GET /v1/pipeline` |

## Correlation Contract

- Clients may send `X-Request-Id` or `X-Correlation-Id`.
- API responses echo both headers.
- Async outbox events use the same value as `correlation_id` and message `trace_id`.
- If the caller sends neither header, the API generates a `req-YYYYMMDD...` value.

## Contract Tests

Covered by `test/test_phase6_async_pipeline.py`:

- legacy create task writes a Phase 6 command outbox event
- legacy endpoints publish deprecation and sunset headers
- correlation IDs are echoed and persisted into outbox events
- Phase 7 health/readiness probes report dependency status
