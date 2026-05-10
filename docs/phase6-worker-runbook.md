# Phase 6 Worker Runbook

This runbook assumes RabbitMQ and PostgreSQL are already running outside Docker.

## Required Environment

```bash
export PHASE6_ASYNC_PIPELINE_ENABLED=true
export JOB_REPOSITORY_BACKEND=sqlalchemy
export DATABASE_URL='postgresql+psycopg://user:password@127.0.0.1:5432/randy_translation'
export RABBITMQ_URL='amqp://guest:guest@127.0.0.1:5672/'
```

Run migrations before starting workers:

```bash
alembic upgrade head
```

Declare RabbitMQ topology:

```bash
PYTHONPATH=. python workers/phase6_worker.py --declare-only
```

## Run Workers

Run one worker on the command queue:

```bash
PYTHONPATH=. python workers/phase6_worker.py --queue pipeline.command
```

Run four worker instances on a stage queue:

```bash
PYTHONPATH=. python workers/phase6_worker.py --queue pipeline.stage.transcribe --instances 4 --prefetch 1
```

Run one bounded drain for smoke tests:

```bash
PYTHONPATH=. python workers/phase6_worker.py --queue pipeline.command --max-messages 1
```

## Retry Scheduler

Phase 6 uses a DB-backed delayed retry scheduler. Failed attempts are stored as
`retry_scheduled` with `next_retry_at`; they are not immediately republished.

Run the scheduler once:

```bash
PYTHONPATH=. python workers/phase6_worker.py --schedule-retries --retry-limit 100
```

For local operation, run this from cron or launchd every minute. The scheduler is idempotent for already-enqueued retries because it clears `next_retry_at` after writing the retry outbox event.

## launchd Examples

Worker plist command:

```xml
<array>
  <string>/bin/zsh</string>
  <string>-lc</string>
  <string>cd /Users/randy/Documents/code/randyTranslation/randyTranslation && source .env && PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --queue pipeline.stage.transcribe --instances 2 --prefetch 1</string>
</array>
```

Retry scheduler plist command:

```xml
<key>StartInterval</key>
<integer>60</integer>
<key>ProgramArguments</key>
<array>
  <string>/bin/zsh</string>
  <string>-lc</string>
  <string>cd /Users/randy/Documents/code/randyTranslation/randyTranslation && source .env && PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --schedule-retries --retry-limit 100</string>
</array>
```

Use one launchd job per busy queue, for example `pipeline.command`, `pipeline.stage.download`, `pipeline.stage.transcribe`, and `pipeline.stage.render`.

## Observability

Phase 7 snapshot endpoint:

```bash
curl http://127.0.0.1:8000/internal/phase7/observability
```

The response includes:

- `queue_depth`: RabbitMQ message count per Phase 6 queue.
- `dlq_count`: RabbitMQ `pipeline.dlq` depth.
- `stage_latency_seconds`: completed stage count, average latency, and p95 latency.
- `stage_status_counts`: DB counts grouped by stage and execution status.

## Integration Test

The RabbitMQ + PostgreSQL + worker integration test is opt-in:

```bash
RUN_PHASE6_RABBITMQ_POSTGRES_INTEGRATION=true \
DATABASE_URL='postgresql+psycopg://user:password@127.0.0.1:5432/randy_translation' \
RABBITMQ_URL='amqp://guest:guest@127.0.0.1:5672/' \
PYTHONPATH=. .venv/bin/python test/test_phase6_rabbitmq_postgres_integration.py
```
