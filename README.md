# randyTranslation Backend / 后端服务

## 中文

randyTranslation Backend 是一个面向 Hip-hop MV 翻译工作流的后端服务。它负责同步关注艺人、发现官方 MV 候选视频、管理审核与人工复核状态、调度异步媒体处理流水线、保存产物，并为运营前端提供面向页面的 API。

项目采用分层 FastAPI 架构，核心运行依赖包括 PostgreSQL、RabbitMQ、媒体对象存储，以及可选的向量检索系统。

### 功能

- 从 Spotify 同步关注艺人，并通过 YouTube/RSS 发现 MV 候选视频。
- 维护艺人和候选视频目录，支持去重、分页、筛选和重新同步。
- 编排转录、AI 品味审核、人工复核、AI 翻译、翻译复核和最终视频渲染流程。
- 持久化工作流状态、审核决策、审计日志、阶段执行记录和产物元数据。
- 支持本地文件系统或腾讯云 COS/S3 兼容存储保存生成媒体。
- 通过 RabbitMQ worker 执行异步流水线、失败重试和队列扇出。
- 提供健康检查、就绪检查、可观测性、指标、双写校验和切换就绪检查接口。
- 保留旧版任务接口，同时通过新版 `/v1` API 支撑前端页面。

### 架构

```text
api/                 FastAPI 应用、路由、依赖注入、运行时配置
application/         用例服务与流水线编排
domain/              实体、枚举、消息契约、仓储接口、状态规则
infrastructure/      SQLAlchemy、SQLite、RabbitMQ、媒体存储、向量适配器
workers/             RabbitMQ worker 入口与队列管理
alembic/             数据库迁移
core/                旧版媒体与 AI 流水线组件
services/            外部来源集成辅助模块
scripts/             迁移、冒烟、回填和质量评估工具
docs/                运行手册、架构说明、阶段总结
test/                单元测试和集成测试
```

核心流程：

```text
Spotify 艺人
  -> YouTube/RSS 候选视频
  -> 歌词/音频转录
  -> AI 品味审核
  -> 人工复核
  -> AI 翻译
  -> 翻译复核
  -> 字幕烧录/渲染
  -> 产物入库
```

### 技术栈

- Python 3.12+
- FastAPI / Starlette / Pydantic
- SQLAlchemy 2.x / Alembic
- PostgreSQL，兼容旧版 SQLite 路径
- RabbitMQ，用于阶段扇出和重试调度
- Qdrant 或 SQLite 向量检索
- 本地文件系统或腾讯云 COS 媒体存储
- faster-whisper、yt-dlp、ffmpeg 媒体处理链路
- OpenAI/DeepSeek 兼容的大模型集成

### 本地安装

```bash
cd randyTranslation
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

启动前编辑 `.env`。不要提交真实 API key、数据库密码、浏览器 cookie 或云服务凭证。

现代后端栈的最小配置示例：

```dotenv
JOB_REPOSITORY_BACKEND=sqlalchemy
DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/randy_translation
PHASE6_ASYNC_PIPELINE_ENABLED=true
RABBITMQ_URL=amqp://guest:guest@localhost:5672/
MEDIA_STORAGE_BACKEND=local
MEDIA_TEMP_ROOT=./data/media/temp
MEDIA_OUTPUT_ROOT=./data/media/output
VECTOR_REPOSITORY_BACKEND=sqlite
```

可选集成：

```dotenv
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
OPENAI_API_KEY=
SPOTIPY_CLIENT_ID=
SPOTIPY_CLIENT_SECRET=
SPOTIPY_REDIRECT_URI=http://localhost:8080/callback
QDRANT_URL=http://localhost:6333
COS_SECRET_ID=
COS_SECRET_KEY=
COS_BUCKET=
COS_REGION=
```

### 数据库迁移

```bash
set -a
source .env
set +a

PYTHONPATH=. .venv/bin/alembic upgrade head
```

当前代码仍保留旧版 SQLite/local 兼容路径，用于迁移期校验和回滚。新的开发应优先使用 SQLAlchemy/PostgreSQL 仓储实现。

### 启动 API

请使用 module mode 启动，避免直接运行 `api/service.py` 造成导入路径问题：

```bash
set -a
source .env
set +a

PYTHONPATH=. .venv/bin/python -m uvicorn api.service:app --host 127.0.0.1 --port 8000
```

健康检查：

```bash
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/readyz
```

API 文档：

- `http://127.0.0.1:8000/docs`
- `http://127.0.0.1:8000/redoc`

### 启动 Worker

声明 RabbitMQ exchange、queue、binding 和 DLQ：

```bash
set -a
source .env
set +a

PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --declare-only
```

启动最小 command worker：

```bash
PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --queue pipeline.command --prefetch 1
```

完整流水线可按阶段分别启动 worker：

```bash
PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --queue pipeline.stage.download --prefetch 1
PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --queue pipeline.stage.transcribe --prefetch 1
PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --queue pipeline.stage.audit --prefetch 1
PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --queue pipeline.stage.manual_review --prefetch 1
PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --queue pipeline.stage.translate --prefetch 1
PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --queue pipeline.stage.translation_review --prefetch 1
PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --queue pipeline.stage.render --prefetch 1
```

调度到期重试：

```bash
PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --schedule-retries --retry-limit 100
```

### API 概览

艺人与候选目录：

- `GET /v1/artists`
- `GET /v1/artists/{artist_id}/candidates`
- `POST /v1/artists/{artist_id}/resync`

审核工作流：

- `GET /v1/audit-queue`
- `GET /v1/audit-log`
- `POST /v1/reviews/{review_id}/approve`
- `POST /v1/reviews/{review_id}/reject`
- `POST /v1/candidates/{candidate_id}/transcript`
- `POST /v1/candidates/{candidate_id}/taste-audit`
- `POST /v1/candidates/{candidate_id}/translation`

流水线与产物库：

- `POST /v1/candidates/{candidate_id}/pipeline`
- `POST /v1/candidates/{candidate_id}/pipeline/retry`
- `GET /v1/candidates/{candidate_id}/workflow-detail`
- `POST /v1/candidates/{candidate_id}/render`
- `GET /v1/pipeline`
- `GET /v1/library`

产物：

- `GET /v1/artifacts/{artifact_id}`
- `POST /v1/artifacts/{artifact_id}/refresh-url`
- `GET /v1/artifacts/{artifact_id}/preview-url`
- `GET /v1/artifacts/{artifact_id}/download`

运维：

- `GET /healthz`
- `GET /readyz`
- `GET /internal/phase6/queue-topology`
- `GET /internal/phase7/observability`
- `GET /internal/phase7/metrics`
- `GET /internal/phase9/cutover-readiness`

旧版兼容接口：

- `POST /create_task`
- `GET /check_status/{task_id}`
- `GET /list_tasks`

### 测试

运行完整 Python 测试：

```bash
PYTHONPATH=. .venv/bin/python -m unittest discover -s test -p 'test*.py'
```

运行重点测试：

```bash
PYTHONPATH=. .venv/bin/python -m unittest test/test_phase0_config_validation.py
PYTHONPATH=. .venv/bin/python -m unittest test/test_phase3_catalog.py
PYTHONPATH=. .venv/bin/python -m unittest test/test_phase6_async_pipeline.py
PYTHONPATH=. .venv/bin/python -m unittest test/test_phase9_cutover.py
```

部分集成测试依赖 PostgreSQL、RabbitMQ、Qdrant、ffmpeg 或云媒体凭证。请通过本地环境变量配置，不要提交真实凭证。

### 运行手册

- `docs/local-startup-runbook.md` - 本地 API、前端、worker 和就绪检查流程
- `docs/phase6-worker-runbook.md` - RabbitMQ worker 操作说明
- `docs/phase7-ops-runbook.md` - 可观测性和运维检查
- `docs/phase9-cutover-runbook.md` - 双写校验和切换就绪流程
- `docs/roadmap.md` - 分阶段架构路线图

### 公开仓库注意事项

- 确认 `.env`、本地数据库、日志、生成媒体、cookie 和云凭证不会被提交。
- 发布前检查 `data/`、`logs/`、`subtitles/` 和生成产物目录。
- 将文档中的本机绝对路径改为相对路径或通用示例路径。
- 确认示例媒体、歌词和字幕内容适合按目标 license 公开。

### License

当前仓库尚未包含 license 文件。正式开源前请添加 `LICENSE`。

---

## English

randyTranslation Backend is the backend service for an AI-assisted hip-hop music video translation workflow. It syncs followed artists, discovers official MV candidates, manages audit and review states, runs an asynchronous media pipeline, stores generated artifacts, and exposes screen-oriented APIs for an operations frontend.

The project is a layered FastAPI application backed by PostgreSQL, RabbitMQ, media storage, and optional vector retrieval.

### Features

- Sync followed artists from Spotify and discover YouTube/RSS MV candidates.
- Maintain a deduplicated artist and candidate catalog with pagination, filters, and manual resync.
- Orchestrate transcription, AI taste audit, manual review, AI translation, translation review, and final video rendering.
- Persist workflow state, review decisions, audit logs, stage executions, and artifact metadata.
- Store generated media in the local filesystem or Tencent COS/S3-compatible storage.
- Execute asynchronous pipeline stages, retries, and queue fan-out through RabbitMQ workers.
- Expose health, readiness, observability, metrics, reconciliation, and cutover-readiness endpoints.
- Keep legacy task APIs while newer `/v1` APIs support frontend screens.

### Architecture

```text
api/                 FastAPI app, routers, dependencies, runtime configuration
application/         Use-case services and pipeline orchestration
domain/              Entities, enums, message contracts, repository interfaces, state rules
infrastructure/      SQLAlchemy, SQLite, RabbitMQ, media storage, vector adapters
workers/             RabbitMQ worker entrypoints and queue management
alembic/             Database migrations
core/                Legacy media and AI pipeline components
services/            Source integration helpers
scripts/             Migration, smoke, backfill, and quality tools
docs/                Runbooks, architecture notes, phase summaries
test/                Unit and integration tests
```

Runtime flow:

```text
Spotify artists
  -> YouTube/RSS candidates
  -> transcript
  -> AI taste audit
  -> manual review
  -> AI translation
  -> translation review
  -> subtitle burn/render
  -> artifact library
```

### Tech Stack

- Python 3.12+
- FastAPI / Starlette / Pydantic
- SQLAlchemy 2.x / Alembic
- PostgreSQL, with legacy SQLite compatibility paths
- RabbitMQ for stage fan-out and retry scheduling
- Qdrant or SQLite-backed vector retrieval
- local filesystem or Tencent COS media storage
- faster-whisper, yt-dlp, ffmpeg-based media processing
- OpenAI/DeepSeek-compatible LLM integrations

### Local Setup

```bash
cd randyTranslation
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` before starting the service. Do not commit real API keys, database passwords, browser cookies, or cloud credentials.

Minimum settings for the modern backend stack:

```dotenv
JOB_REPOSITORY_BACKEND=sqlalchemy
DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/randy_translation
PHASE6_ASYNC_PIPELINE_ENABLED=true
RABBITMQ_URL=amqp://guest:guest@localhost:5672/
MEDIA_STORAGE_BACKEND=local
MEDIA_TEMP_ROOT=./data/media/temp
MEDIA_OUTPUT_ROOT=./data/media/output
VECTOR_REPOSITORY_BACKEND=sqlite
```

Optional integrations:

```dotenv
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
OPENAI_API_KEY=
SPOTIPY_CLIENT_ID=
SPOTIPY_CLIENT_SECRET=
SPOTIPY_REDIRECT_URI=http://localhost:8080/callback
QDRANT_URL=http://localhost:6333
COS_SECRET_ID=
COS_SECRET_KEY=
COS_BUCKET=
COS_REGION=
```

### Database Migration

```bash
set -a
source .env
set +a

PYTHONPATH=. .venv/bin/alembic upgrade head
```

The codebase still contains legacy SQLite/local compatibility paths for migration validation and rollback. New development should prefer the SQLAlchemy/PostgreSQL repositories.

### Start the API

Use module mode to keep imports consistent:

```bash
set -a
source .env
set +a

PYTHONPATH=. .venv/bin/python -m uvicorn api.service:app --host 127.0.0.1 --port 8000
```

Health checks:

```bash
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/readyz
```

API docs:

- `http://127.0.0.1:8000/docs`
- `http://127.0.0.1:8000/redoc`

### Start Workers

Declare RabbitMQ exchanges, queues, bindings, and DLQ:

```bash
set -a
source .env
set +a

PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --declare-only
```

Start a minimal command worker:

```bash
PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --queue pipeline.command --prefetch 1
```

For full pipeline processing, run one worker per active stage queue:

```bash
PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --queue pipeline.stage.download --prefetch 1
PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --queue pipeline.stage.transcribe --prefetch 1
PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --queue pipeline.stage.audit --prefetch 1
PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --queue pipeline.stage.manual_review --prefetch 1
PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --queue pipeline.stage.translate --prefetch 1
PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --queue pipeline.stage.translation_review --prefetch 1
PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --queue pipeline.stage.render --prefetch 1
```

Schedule due retries:

```bash
PYTHONPATH=. .venv/bin/python workers/phase6_worker.py --schedule-retries --retry-limit 100
```

### API Overview

Artist and candidate catalog:

- `GET /v1/artists`
- `GET /v1/artists/{artist_id}/candidates`
- `POST /v1/artists/{artist_id}/resync`

Review workflow:

- `GET /v1/audit-queue`
- `GET /v1/audit-log`
- `POST /v1/reviews/{review_id}/approve`
- `POST /v1/reviews/{review_id}/reject`
- `POST /v1/candidates/{candidate_id}/transcript`
- `POST /v1/candidates/{candidate_id}/taste-audit`
- `POST /v1/candidates/{candidate_id}/translation`

Pipeline and library:

- `POST /v1/candidates/{candidate_id}/pipeline`
- `POST /v1/candidates/{candidate_id}/pipeline/retry`
- `GET /v1/candidates/{candidate_id}/workflow-detail`
- `POST /v1/candidates/{candidate_id}/render`
- `GET /v1/pipeline`
- `GET /v1/library`

Artifacts:

- `GET /v1/artifacts/{artifact_id}`
- `POST /v1/artifacts/{artifact_id}/refresh-url`
- `GET /v1/artifacts/{artifact_id}/preview-url`
- `GET /v1/artifacts/{artifact_id}/download`

Operations:

- `GET /healthz`
- `GET /readyz`
- `GET /internal/phase6/queue-topology`
- `GET /internal/phase7/observability`
- `GET /internal/phase7/metrics`
- `GET /internal/phase9/cutover-readiness`

Legacy compatibility APIs:

- `POST /create_task`
- `GET /check_status/{task_id}`
- `GET /list_tasks`

### Tests

Run the full Python test suite:

```bash
PYTHONPATH=. .venv/bin/python -m unittest discover -s test -p 'test*.py'
```

Run focused suites:

```bash
PYTHONPATH=. .venv/bin/python -m unittest test/test_phase0_config_validation.py
PYTHONPATH=. .venv/bin/python -m unittest test/test_phase3_catalog.py
PYTHONPATH=. .venv/bin/python -m unittest test/test_phase6_async_pipeline.py
PYTHONPATH=. .venv/bin/python -m unittest test/test_phase9_cutover.py
```

Some integration tests require PostgreSQL, RabbitMQ, Qdrant, ffmpeg, or cloud/media credentials. Keep those configured through local environment variables, not committed secrets.

### Runbooks

- `docs/local-startup-runbook.md` - local API, frontend, worker, and readiness flow
- `docs/phase6-worker-runbook.md` - RabbitMQ worker operations
- `docs/phase7-ops-runbook.md` - observability and operational checks
- `docs/phase9-cutover-runbook.md` - dual-write and cutover readiness
- `docs/roadmap.md` - phased architecture roadmap

### Public Repository Notes

- Ensure `.env`, local databases, logs, generated media, cookies, and cloud credentials are ignored.
- Review `data/`, `logs/`, `subtitles/`, and generated artifact directories before publishing.
- Replace local-only absolute paths in documentation with relative or generic example paths.
- Confirm media, lyrics, and subtitle samples are safe to publish under the intended license.

### License

No license file is included yet. Add a `LICENSE` file before relying on this project as open source.
