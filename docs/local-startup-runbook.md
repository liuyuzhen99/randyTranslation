# Local Startup Runbook

This runbook starts the local randyTranslation stack:

- FastAPI backend on `http://127.0.0.1:8000`
- Next.js frontend on `http://localhost:3000`
- RabbitMQ Phase 6 workers
- PostgreSQL, RabbitMQ, Qdrant, and COS readiness checks

Run commands from the backend project root unless noted:

```bash
cd /Users/randy/Documents/code/randyTranslation/randyTranslation
```

## 1. Prerequisites

Confirm `.env` contains production-like local settings:

```bash
JOB_REPOSITORY_BACKEND=sqlalchemy
DATABASE_URL=postgresql+psycopg://randy:Liuyuzhen9@localhost:5432/randy_translation
PHASE6_ASYNC_PIPELINE_ENABLED=true
RABBITMQ_URL=amqp://guest:guest@localhost:5672/
VECTOR_REPOSITORY_BACKEND=qdrant
QDRANT_URL=http://localhost:6333
VECTOR_EMBEDDING_PROVIDER=bge
VECTOR_EMBEDDING_DIMENSION=1024
```

Required local services:

- PostgreSQL listening on `localhost:5432`
- RabbitMQ listening on `localhost:5672`
- Qdrant listening on `localhost:6333`

Quick dependency probe:

```bash
set -a; source .env; set +a
PYTHONPATH=. .venv/bin/python -c "from api.config import load_runtime_settings, create_sqlalchemy_session_factory; from sqlalchemy import text; s=load_runtime_settings(); sf=create_sqlalchemy_session_factory(runtime_settings=s); cm=sf.session_scope(); session=cm.__enter__(); print(session.execute(text('select 1')).scalar()); cm.__exit__(None,None,None)"
curl -s http://127.0.0.1:6333/readyz
```

Expected:

- PostgreSQL prints `1`
- Qdrant prints `all shards are ready`

## 2. Prepare RabbitMQ

Declare exchanges, queues, bindings, and DLQ:

```bash
set -a; source .env; set +a
PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --declare-only
```

Expected:

```text
{'exchange': 'pipeline', 'queues': 9}
```

## 3. Start Backend

Use module mode. Do not run `python api/service.py`; direct script mode can trigger circular imports.

```bash
set -a; source .env; set +a
PYTHONPATH=/Users/randy/Documents/code/randyTranslation/randyTranslation \
.venv/bin/python -m uvicorn api.service:app --host 127.0.0.1 --port 8000
```

Expected:

```text
Application startup complete.
Uvicorn running on http://127.0.0.1:8000
```

## 4. Start Frontend

Use a separate terminal:

```bash
cd /Users/randy/Documents/code/randyTranslation/vibeFrontTranslation/auditflow-app
RANDY_TRANSLATION_API_BASE_URL=http://127.0.0.1:8000 npm run dev
```

Expected:

```text
Local: http://localhost:3000
Ready
```

## 5. Start Workers

For a minimal local smoke run, start one command worker:

```bash
cd /Users/randy/Documents/code/randyTranslation/randyTranslation
set -a; source .env; set +a
PYTHONPATH=/Users/randy/Documents/code/randyTranslation/randyTranslation \
.venv/bin/python workers/phase6_worker.py --queue pipeline.command --prefetch 1
```

For full pipeline processing, run one worker per active stage queue in separate terminals:

```bash
PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --queue pipeline.command --prefetch 1
PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --queue pipeline.stage.download --prefetch 1
PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --queue pipeline.stage.transcribe --prefetch 1
PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --queue pipeline.stage.audit --prefetch 1
PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --queue pipeline.stage.manual_review --prefetch 1
PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --queue pipeline.stage.translate --prefetch 1
PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --queue pipeline.stage.translation_review --prefetch 1
PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --queue pipeline.stage.render --prefetch 1
```

Scale busy queues with `--instances`, for example:

```bash
PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --queue pipeline.stage.transcribe --instances 4 --prefetch 1
```

Run delayed retry scheduling periodically:

```bash
PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --schedule-retries --retry-limit 100
```

## 6. Health Checks

Backend readiness:

```bash
curl -s http://127.0.0.1:8000/readyz
```

Healthy response should include:

```json
{
  "status": "ok",
  "checks": {
    "db": {"status": "ok"},
    "rabbitmq": {"status": "ok"},
    "oss": {"status": "ok"},
    "qdrant": {"status": "ok"}
  }
}
```

Frontend HTTP:

```bash
curl -s -I http://127.0.0.1:3000
```

The root route currently redirects to `/artists`, so `307 Temporary Redirect` is normal.

Frontend-to-backend BFF check:

```bash
curl -s 'http://127.0.0.1:3000/api/artists?page=1&pageSize=3'
```

Expected: JSON with `stats`, `items`, and `pagination`.

Observability snapshot:

```bash
curl -s http://127.0.0.1:8000/internal/phase7/observability
```

## 7. Queue State

`/readyz` includes RabbitMQ queue depth:

- `pipeline.command`
- `pipeline.stage.download`
- `pipeline.stage.transcribe`
- `pipeline.stage.audit`
- `pipeline.stage.manual_review`
- `pipeline.stage.translate`
- `pipeline.stage.translation_review`
- `pipeline.stage.render`
- `pipeline.dlq`

Main and stage queues should normally drain to `0` when workers are running.

If `pipeline.dlq` is non-zero, inspect the failed records before replaying. A common local issue is historical RabbitMQ messages whose `job_id` no longer exists in PostgreSQL. Those messages will fail with a foreign-key violation on `pipeline_stage_executions.job_id` and should not be blindly replayed.

## 8. Common Problems

### Backend Fails With Circular Import

Symptom:

```text
AttributeError: partially initialized module 'api.routers.pipeline' has no attribute 'router'
```

Cause: backend was started with direct script mode.

Fix:

```bash
PYTHONPATH=/Users/randy/Documents/code/randyTranslation/randyTranslation \
.venv/bin/python -m uvicorn api.service:app --host 127.0.0.1 --port 8000
```

### Worker Immediately Emits Foreign-Key Errors

Symptom:

```text
pipeline_stage_executions_job_id_fkey
Key (job_id)=... is not present in table "jobs"
```

Cause: RabbitMQ contains old messages from a previous DB state.

Action:

1. Stop the worker.
2. Check `/readyz` queue depths.
3. Confirm whether bad messages moved to `pipeline.dlq`.
4. Do not replay DLQ until the referenced jobs exist or the messages are intentionally repaired.

### Qdrant Dimension Mismatch

Symptom:

```text
Qdrant collection ... has vector dimension 384, but the configured embedding provider emits 1024
```

Fix:

1. Confirm collection dimension in Qdrant dashboard or API.
2. Rebuild the collection as 1024-dimensional.
3. Rerun:

```bash
PYTHONPATH=. .venv/bin/python scripts/phase8_qdrant_backfill.py --source-sqlite data/jobs.db --qdrant-url http://127.0.0.1:6333
```

### First BGE Startup Is Slow

`BAAI/bge-m3` must be downloaded or loaded from Hugging Face cache on first use. Once cached, `BGEEmbeddingProvider` should emit 1024-dimensional normalized vectors:

```bash
PYTHONPATH=. .venv/bin/python -c "from application.services.phase8_vectors import BGEEmbeddingProvider; p=BGEEmbeddingProvider(); v=p.embed('test lyrics 中文检索'); print(p.dimension, len(v), round(sum(x*x for x in v), 6))"
```

Expected:

```text
1024 1024 1.0
```

## 9. Shutdown

Stop foreground processes with `Ctrl-C`:

- FastAPI terminal
- Next.js terminal
- each worker terminal

If a worker is processing bad backlog and does not stop cleanly, identify the PID and stop it:

```bash
ps -ef | grep phase6_worker
kill <pid>
```
