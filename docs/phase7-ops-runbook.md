# Phase 7 Ops Runbook

## Probes

- `GET /healthz`
  - Liveness only.
  - Returns `200` when the API process can serve requests.

- `GET /readyz`
  - Readiness for production traffic.
  - Checks DB, RabbitMQ, OSS, and optional Qdrant.
  - Returns `200` only when required dependencies are `ok`.
  - Returns `503` with `status=degraded` when a required dependency is failed or not configured.
  - Qdrant is optional in Phase 7 and reports `skipped` when `QDRANT_URL` is empty.

## Observability

Snapshot endpoint:

```bash
curl http://127.0.0.1:8000/internal/phase7/observability
```

Prometheus text endpoint:

```bash
curl http://127.0.0.1:8000/internal/phase7/metrics
```

The snapshot includes:

- queue depth per Phase 6 queue
- DLQ count
- stage latency `avg` and `p95`
- stage status counts
- stage success/failure rates
- retry count by stage
- discovery freshness
- pending review aging

Every response includes `X-Request-Id` and `X-Correlation-Id`. Send either header from callers to keep API, outbox, and worker traces aligned.

Tracing hooks are installed around API requests and worker stage handling. Configure the OpenTelemetry SDK/exporter in the runtime environment when an OTLP collector is available; without a collector the hooks behave as no-op spans.

Grafana starter dashboard:

```text
docs/phase7-grafana-dashboard.json
```

## Queue Backlog Incident

1. Check `/readyz` and `/internal/phase7/observability`.
2. Compare `queue_depth` across `pipeline.command` and `pipeline.stage.*`.
3. Scale workers with:

```bash
python workers/phase6_worker.py --queue pipeline.stage.download --instances 2
```

4. Run delayed retry scheduling:

```bash
python workers/phase6_worker.py --schedule-retries --retry-limit 100
```

5. If `pipeline.dlq` grows, inspect stage error messages in `pipeline_stage_executions` before replaying.

## DB Contention Incident

1. Check `/readyz` DB status.
2. Inspect slow stage transitions via `stage_latency_seconds`.
3. Pause non-critical retry scheduling if contention is active.
4. Keep legacy reads available through `/check_status/{task_id}` and `/list_tasks` until the rollback window closes.

## OSS Outage

1. Check `/readyz` OSS status and storage provider.
2. Failed artifact operations should surface as stage failures and eventually retry/DLQ through Phase 6.
3. Do not mark review decisions complete for missing render artifacts.
4. After recovery, run the retry scheduler and verify artifact preview/download endpoints.

## Replay Handling

1. Confirm the failed execution is idempotent by checking `dedupe_key`.
2. Requeue only messages whose execution status is `retry_scheduled` or `dlq`.
3. Preserve `trace_id` and `correlation_id` when replaying.
4. Verify that downstream review/audit side effects are not duplicated.

## Smoke Drill

Minimum staging drill before cutover:

1. Start API with PostgreSQL, RabbitMQ, and selected OSS backend.
2. Verify `/healthz`, `/readyz`, `/internal/phase7/observability`, and `/internal/phase7/metrics`.
3. Submit one render through `/v1/candidates/{candidate_id}/render`.
4. Run workers through download, transcribe, audit, manual review, translate, translation review, and render.
5. Simulate one worker failure and confirm retry scheduling.
6. Confirm `/v1/pipeline`, `/v1/audit-queue`, `/v1/library`, and `/v1/audit-log` remain consistent.

Smoke command:

```bash
python scripts/phase7_smoke_drill.py --base-url http://127.0.0.1:8000 --require-ready
```

Use `--require-ready` in staging where DB, RabbitMQ, and OSS must all be available. Omit it during local development if RabbitMQ is intentionally not running.
