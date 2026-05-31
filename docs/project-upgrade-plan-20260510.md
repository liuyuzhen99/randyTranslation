# Project Upgrade Plan — 2026-05-10

## 背景

基于对代码库的系统审查和 review 修订，本文档是可直接执行的改造手册。目标是将项目从"知道概念"升级为"真正跑通过"。

计划分三个优先级：
- **P0**：当前存在真实风险，阻塞生产可用性
- **P1**：影响可维护性和简历说服力
- **P2**：提升工程规范到中等生产水准

---

## P0：必须补齐

### 任务 1：并发安全 — OutboxRepository.list_pending() 加 FOR UPDATE SKIP LOCKED

**问题**：[infrastructure/persistence/sqlalchemy_repositories.py:926-933](../infrastructure/persistence/sqlalchemy_repositories.py) 中 `list_pending()` 是普通 SELECT，多实例部署时同一条消息会被重复消费。

**改动文件**：`infrastructure/persistence/sqlalchemy_repositories.py`

**步骤**：

1. 在 `SQLAlchemyOutboxRepository.__init__` 中读取方言类型（注意：SQLAlchemy 2.x 中 `session.bind` 已废弃，不能用 `session.bind.dialect.name`，应通过 `session_factory.engine` 访问）：

```python
class SQLAlchemyOutboxRepository:
    def __init__(self, session_factory: SQLAlchemySessionFactory) -> None:
        self.session_factory = session_factory
        self._is_postgres = session_factory.engine.dialect.name == "postgresql"
```

2. 在 `list_pending()` 中条件加锁：

```python
def list_pending(self) -> list[OutboxEvent]:
    with self.session_factory.session_scope() as session:
        stmt = (
            select(OutboxModel)
            .where(OutboxModel.status == "pending")
            .order_by(OutboxModel.event_id.asc())
            .limit(50)
        )
        if self._is_postgres:
            stmt = stmt.with_for_update(skip_locked=True)
        rows = session.execute(stmt).scalars().all()
        return [self._to_entity(row) for row in rows]
```

3. `OutboxModel` 加 `locked_by` / `locked_at` 字段**仅在需要跨进程 crash recovery 时添加**。当前用 supervisor 管理单进程时不需要，FOR UPDATE SKIP LOCKED 已足够。

**验证**：
```bash
# 写并发集成测试：两个线程同时调用 list_pending()，断言总消费条数 == 插入条数
python -m pytest test/ -k "outbox_concurrent" -v
```

---

### 任务 2：事务边界 — execution_repository.upsert 与 outbox_repository.add 同一事务

**问题**：`PipelineStageWorker.handle()` 中 `execution_repository.upsert()`（[application/services/async_pipeline.py:202、221、258、291](../application/services/async_pipeline.py)）和 `_enqueue_message()` 里的 `outbox_repository.add()`（行 121）各自持有独立 `session_scope()`，中间若崩溃会导致 stage 状态已更新但消息未入队（或反之）。

**注意**：这是比原始计划描述更大的改动——需要修改 `SQLAlchemySessionFactory` 使其支持跨 repository 共享 session，而不是每个 repository 自管事务。

**改动文件**：
- `infrastructure/persistence/sqlalchemy_repositories.py`（`SQLAlchemySessionFactory` 新增 `shared_session` 支持）
- `application/services/async_pipeline.py`（`PipelineStageWorker.handle()` 使用共享 session）

**步骤**：

1. 给 `SQLAlchemySessionFactory` 新增 `transactional()` context manager，允许跨 repository 共享：

```python
@contextmanager
def transactional(self):
    """Yield a session for use across multiple repository calls in one transaction."""
    session: Session = self._sessionmaker()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

2. 给各 repository 的写方法新增可选 `session` 参数重载，或拆分为 `_upsert_with_session(session, ...)` 内部方法。

3. 在 `PipelineStageWorker.handle()` 中将 `execution.upsert()` 和 `_enqueue_message()` 包在同一个 `transactional()` 中。

**验证**：写集成测试模拟 outbox add 成功但 execution upsert 触发约束失败，断言两者都回滚。

---

### 任务 3：RAG — 接入真实 Embedding 模型

**问题**：[application/services/phase8_vectors.py:23-42](../application/services/phase8_vectors.py) 中 `HashingEmbeddingProvider` 用 SHA-256 哈希生成伪向量，语义检索完全失效，Few-shot RAG 注入毫无意义。

**重要风险**：从默认 384 维升级到 `text-embedding-3-small` 的 1536 维，必须**删除并重建 Qdrant collection**，现有向量数据全部作废。迁移策略：先建新 collection（`lyrics_v2`），backfill，验证 quality，再切流量，最后删旧 collection。参考已有的 `scripts/phase8_chroma_to_qdrant.py` 模式。

**改动文件**：
- `application/services/phase8_vectors.py`（新增 `OpenAIEmbeddingProvider`）
- `api/config.py`（`create_vector_repository()` 改用真实 embedding，新增 `OPENAI_API_KEY` 到 `KNOWN_ENV_VARS`）
- `.env.example`（新增 `OPENAI_API_KEY`、`VECTOR_EMBEDDING_DIMENSION=1536`）

**步骤**：

1. 新增 `OpenAIEmbeddingProvider`（`openai==2.29.0` 已在 requirements.txt）：

```python
class OpenAIEmbeddingProvider:
    MODEL = "text-embedding-3-small"
    dimension = 1536

    def __init__(self, api_key: str) -> None:
        import openai
        self._client = openai.OpenAI(api_key=api_key)

    def embed(self, text: str) -> list[float]:
        response = self._client.embeddings.create(
            model=self.MODEL,
            input=text.strip() or "empty",
        )
        return response.data[0].embedding
```

2. 在 `api/config.py` 的 `create_vector_repository()` 中：读取 `OPENAI_API_KEY`，有则用 `OpenAIEmbeddingProvider`，无则 fallback 到 `HashingEmbeddingProvider` 并 `logger.warning`。

3. 在 `VectorRecord` 的 payload metadata 中记录 `embedding_model` 版本字段，便于未来检测 collection 是否需要重建。

4. Qdrant 迁移流程：
   ```bash
   # Step 1: 建新 collection（dimension=1536）
   python scripts/phase8_qdrant_backfill.py --collection lyrics_v2
   # Step 2: 跑 retrieval quality evaluation
   python scripts/phase8_retrieval_quality.py --collection lyrics_v2
   # Step 3: 切流量（更新 VECTOR_COLLECTION_NAME=lyrics_v2）
   # Step 4: 确认稳定后删旧 collection
   ```

**验证**：
```bash
python scripts/phase8_retrieval_quality.py
# 补充至少 3 个真实中英文歌词 baseline cases
# 目标：至少 2/3 case 通过（HashingEmbeddingProvider 基准下全部失败）
```

---

## P1：影响可维护性

### 任务 4：api/service.py 拆分为 FastAPI APIRouter

**问题**：[api/service.py](../api/service.py) 1792 行，`create_app()` 内嵌套定义 helper，`build_runtime_services()` 14 元素 tuple 返回值，难以维护和测试。

**执行策略**：分两步，不要一次性重构：
- **Step A（半天）**：`build_runtime_services()` 返回值改为 dataclass，消除位置解包。可独立合并。
- **Step B（两天）**：按资源拆分 router，引入 `api/dependencies.py`。

**改动文件**：
- `api/service.py`（精简为 app 工厂 + lifespan）
- 新建 `api/dependencies.py`
- 新建 `api/serializers.py`
- 新建 `api/routers/artists.py`
- 新建 `api/routers/pipeline.py`
- 新建 `api/routers/reviews.py`
- 新建 `api/routers/artifacts.py`
- 新建 `api/routers/internal.py`

**Step A — `RuntimeServices` dataclass**：
```python
@dataclass
class RuntimeServices:
    job_repository: JobRepository
    job_service: JobService
    media_storage: MediaStorageService
    phase4_workflow_services: Phase4WorkflowServices
    # ... 每个字段命名，消除 tuple[0]、tuple[1] 位置解包
```

**Step B — dependencies + router**：
```python
# api/dependencies.py
def get_job_service(request: Request) -> JobService:
    return request.app.state.services.job_service

# api/routers/artists.py
router = APIRouter(tags=["artists"])

@router.get("/artists")
def list_artists(svc = Depends(get_artist_service)): ...
```

**验证**：
```bash
uvicorn api.service:create_app --factory --reload
# 访问 /docs 确认所有路由正常
python -m pytest test/ -x
```

---

### 任务 5：Phase 9 — 补字段级一致性校验并生成真实 snapshot

**问题**：`Phase9ReconciliationService.compare_snapshots()` 只做 key set 对比（[application/services/phase9_cutover.py:60-83](../application/services/phase9_cutover.py)），双写未真正验证字段值一致性。

**注意**：扩展 `EntitySnapshot` 时，`@dataclass(frozen=True)` 不能直接持有 `dict` 字段（`dict` 不可哈希）。需要去掉 `frozen=True` 或给 payload 字段加 `field(compare=False, hash=False)`。

**改动文件**：`application/services/phase9_cutover.py`、`scripts/phase9_cutover_report.py`

**步骤**：

1. 扩展数据结构：
```python
@dataclass
class EntitySnapshot:          # 去掉 frozen=True
    entity: str
    keys: set[str]
    payloads: dict[str, dict] = field(default_factory=dict, compare=False, hash=False)
```

2. `EntityParityReport` 添加 `field_mismatches: list[dict]`。

3. 在 `compare_snapshots()` 中对 key 重叠的记录逐字段比对，记录差异到 `field_mismatches`。

4. 在 `scripts/phase9_cutover_report.py` 中生成真实 snapshot：查询 PostgreSQL 里的 artists/candidates/jobs/reviews/artifacts 表。

**验证**：
```bash
python scripts/phase9_cutover_report.py
# 输出 JSON 报告应包含 field_mismatches 字段
```

---

### 任务 6：补充 Pipeline 端到端集成测试

**问题**：现有测试大量使用 `py_compile` 做语法验证（`test_phase1_layered_architecture.py`），行为路径覆盖偏少。

**改动文件**：新建 `test/test_pipeline_e2e.py`

**步骤**：

1. SQLite 内存库 + mock 外部依赖，跑完整 7-stage 流程：
```python
@pytest.fixture
def pipeline_worker(tmp_path):
    session_factory = SQLAlchemySessionFactory("sqlite:///:memory:")
    session_factory.create_schema()
    # 构建完整依赖树...
    return worker

def test_full_pipeline_7_stages(pipeline_worker, monkeypatch):
    monkeypatch.setattr("core.ytbAVDownloader.download", mock_download)
    monkeypatch.setattr("core.aiTranslator.translate", mock_translate)
    # 断言 job.status == COMPLETED
    # 断言每个 stage execution.status == COMPLETED
```

2. FastAPI `TestClient` 联调测试：Artists → Candidates → Pipeline start → Poll → Review 完整路径。

**验证**：
```bash
python -m pytest test/test_pipeline_e2e.py -v
```

---

### 任务 7：shadow_write 失败可观测性

**问题**：`job_service.py` 中 shadow write 失败目前用 `logger.exception` 静默处理，没有 API 层面的降级标记。

**注意**：`phase7_metrics.py` 是一个**渲染器**（`render_prometheus_metrics(snapshot: dict) -> str`），没有 `Counter`/`Gauge` 对象和 `.inc()` 方法，项目也没有引入 `prometheus_client`。不能按原计划写 `phase7_metrics.shadow_write_failure_total.inc()`。

**可行方案**：
1. 将 `logger.exception` 升级为 `logger.error`（语义更清晰，会被日志告警系统捕获）。
2. 在 API 创建 job 的 response 里加 `meta.shadow_write_degraded: true` 字段（当 shadow write 不可用时）。
3. 若后续引入 `prometheus_client`，再补 Counter；现阶段不强依赖。

**改动文件**：`application/services/job_service.py`、对应的 API response schema

---

## P2：提升工程质量

### 任务 8：数据库索引 — 先 EXPLAIN，再补缺

**重要修正**：原始计划说"reviews 表缺少组合索引"，但代码已有：
```python
# sqlalchemy_models.py:319-320（已存在）
Index("ix_review_items_subject", "subject_kind", "subject_id"),
Index("ix_review_items_status_created_at", "status", "created_at"),
```

**正确做法**：先跑 `EXPLAIN ANALYZE` 找真实热路径，而不是预设结论。候选缺口：
- `outbox_events`：当前只有 `ix_outbox_status` 单列索引，`list_pending()` 的 `ORDER BY event_id` 可能需要 `(status, event_id)` 复合索引
- 审核队列按 `(status, subject_kind)` 的查询路径（视实际查询而定）

**步骤**：
```sql
-- 先找慢查询
EXPLAIN ANALYZE SELECT * FROM outbox_events WHERE status = 'pending' ORDER BY event_id LIMIT 50;
EXPLAIN ANALYZE SELECT * FROM review_items WHERE status = 'pending' ORDER BY created_at;
-- 根据结果决定补哪些索引
```

然后在 `sqlalchemy_models.py` 中添加，生成 Alembic migration。

**验证**：migration 执行无报错，`EXPLAIN` 显示 Index Scan 而非 Seq Scan。

---

### 任务 9：AI 调用加 structured output + timeout + retry

**问题**：`core/aiTranslator.py` 中 DeepSeek 调用直接解析字符串，无 schema 约束，无 timeout，AI 超时会导致整个 stage hang。

**改动文件**：`core/aiTranslator.py`

**步骤**：

1. 定义 Pydantic 输出 schema：
```python
class TranslationLine(BaseModel):
    line_index: int
    zh_text: str
    confidence: float = 1.0

class TranslationOutput(BaseModel):
    lines: list[TranslationLine]
```

2. 加 timeout + retry（`tenacity==9.1.4` 已在 requirements.txt）：
```python
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
def _call_deepseek(prompt: str, timeout: int = 30) -> TranslationOutput:
    ...
```

3. 建立 `prompts/` 目录，将 prompt template 迁移到文件（`prompts/translation_v1.txt`），代码中引用版本号，不硬编码。

**验证**：mock DeepSeek 返回格式错误的 JSON，断言 Pydantic `ValidationError` 正确抛出。

---

### 任务 10：配置管理迁移到 pydantic-settings

**问题**：`api/config.py` 的 `load_runtime_settings()` 手动解析 40+ 环境变量（行 160-276）。`pydantic-settings==2.13.1` 已在 requirements.txt，可直接用。

**改动文件**：`api/config.py`

**步骤**：

1. `AppRuntimeSettings` 从普通 `@dataclass` 改为 `pydantic_settings.BaseSettings`，字段自动从环境变量读取：
```python
from pydantic_settings import BaseSettings, SettingsConfigDict

class AppRuntimeSettings(BaseSettings):
    job_repository_backend: str = "memory"
    database_url: str = ""
    phase2_shadow_write_enabled: bool = False
    openai_api_key: str = ""
    # ... 所有字段

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
```

2. 删除 `load_runtime_settings()` 中的手动解析逻辑（约 120 行），改为 `return AppRuntimeSettings()`。

3. 将 `validate_startup_env()` 的必填校验迁移到 `@model_validator`，启动时统一报错。

**验证**：
```bash
python -c "from api.config import AppRuntimeSettings; print(AppRuntimeSettings())"
# 故意缺少必填变量，断言 pydantic ValidationError
```

---

### 任务 11：统一错误处理

**问题**：`HTTPException`、`KeyError` 转 404、静默 `except Exception` 三种模式混用，`vector_repository` 失败被静默吞掉。

**改动文件**：`domain/exceptions.py`（扩展）、`api/service.py`（全局 handler）

**步骤**：

1. 扩展 `domain/exceptions.py`：
```python
class NotFoundError(Exception):
    def __init__(self, resource: str, id: str) -> None:
        self.resource = resource
        self.id = id

class ConflictError(Exception): ...
class DomainValidationError(Exception): ...
```

（避免和 `pydantic.ValidationError` 同名，用 `DomainValidationError`）

2. 在 `create_app()` 注册全局 handler：
```python
@app.exception_handler(NotFoundError)
async def not_found_handler(request: Request, exc: NotFoundError):
    return JSONResponse(status_code=404, content={"code": "not_found", "resource": exc.resource, "id": exc.id})
```

3. 将各 router 中的 `raise HTTPException(status_code=404)` 替换为 `raise NotFoundError(...)`。

4. RAG 不可用时在 response meta 中加 `degraded: true` 而不是静默 skip。

**验证**：`TestClient` 请求不存在资源，断言返回统一格式的错误 JSON。

---

## P3：加分项（有余力可做）

| 项目 | 文件 | 说明 |
|------|------|------|
| `docker-compose.yml` | 根目录 | PostgreSQL + RabbitMQ + Qdrant 一键启动 |
| WebSocket 实时推送 | `api/routers/pipeline.py` | 替换 15s 轮询，用 `fastapi.WebSocket` |
| Grafana dashboard 跑通 | `scripts/phase7_smoke_drill.py` | 真实连接 Prometheus + Grafana |
| Artist resync 速率限制 | `application/services/phase3_catalog_service.py` | `asyncio.Semaphore` 控制 Spotify API 并发 |

---

## 执行时间线

```
Week 1: 任务1(FOR UPDATE SKIP LOCKED) + 任务3(Embedding 接入，先不做维度迁移)
Week 2: 任务2(事务边界，较大重构) + 任务4-Step A(RuntimeServices dataclass)
Week 3: 任务4-Step B(router 拆分) + 任务5(Phase9字段校验) + 任务6(E2E 测试)
Week 4: 任务7(shadow_write 告警) + 任务8(EXPLAIN 先行再补索引) + 任务9+10+11
        任务3-维度迁移(在 Week 1 Embedding 接入稳定后，独立窗口执行)
```

**里程碑**：
- P0 三项（任务1+2+3 embedding 部分）完成 → 核心风险消除
- P1 三项（任务4+5+6）完成 → 简历可写"重构 API 层、端到端测试覆盖"
- P2 四项完成 → 工程规范达到中等生产水准

---

## 关键文件索引

| 文件 | 当前问题 | 任务 |
|------|---------|------|
| [infrastructure/persistence/sqlalchemy_repositories.py:926](../infrastructure/persistence/sqlalchemy_repositories.py) | `list_pending()` 无锁 | 任务1 |
| [application/services/async_pipeline.py:120-131](../application/services/async_pipeline.py) | outbox.add 与 execution.upsert 不同事务 | 任务2 |
| [application/services/phase8_vectors.py:23](../application/services/phase8_vectors.py) | `HashingEmbeddingProvider` 伪向量 | 任务3 |
| [api/config.py:320](../api/config.py) | `create_vector_repository()` 用假 embedding | 任务3 |
| [api/service.py](../api/service.py) | 1792 行单文件 | 任务4 |
| [application/services/phase9_cutover.py:60](../application/services/phase9_cutover.py) | 只做 key set 对比，frozen dataclass 加 dict 字段会报错 | 任务5 |
| [api/config.py:160](../api/config.py) | 手动解析 120 行环境变量 | 任务10 |
| [domain/exceptions.py](../domain/exceptions.py) | 需扩展业务异常层级 | 任务11 |

---

## 实施进展记录（2026-05-10）

### 分支：`feature/project-upgrade-p0-p1`

基于计划执行后的实际改动记录，含所有与原计划的偏差说明。

---

#### commit 1 — `9583570` `docs: add project upgrade plan 20260510`

生成本文件，包含所有 review 修正（索引目标描述有误、`phase7_metrics` 框架不存在、`session.bind` 废弃、Qdrant 维度迁移破坏性、`frozen=True` + dict 字段兼容性）。

**状态**：完成

---

#### commit 2 — `d101d30` `fix(P0): outbox concurrent safety + atomic upsert-enqueue transaction`

**任务 1（并发安全）实际改动**：

- [infrastructure/persistence/sqlalchemy_repositories.py](../infrastructure/persistence/sqlalchemy_repositories.py)
  - `SQLAlchemyOutboxRepository.__init__` 新增 `self._is_postgres = session_factory.engine.dialect.name == "postgresql"`（通过 `engine` 访问，非废弃的 `session.bind`）
  - `list_pending()` 改为：先构造 `stmt`，若 `_is_postgres` 则 `.with_for_update(skip_locked=True)`，同时加 `.order_by(event_id.asc()).limit(50)` 使行为更确定

**任务 2（事务边界）实际改动**：

- [infrastructure/persistence/sqlalchemy_repositories.py](../infrastructure/persistence/sqlalchemy_repositories.py)
  - `SQLAlchemySessionFactory` 新增 `transactional()` context manager（与 `session_scope()` 语义相同，但用途语义更清晰，供跨 repository 共享调用）
  - `SQLAlchemyPipelineStageExecutionRepository` 新增 `upsert_with_session(session, execution)` 和私有 `_upsert_with_session(session, execution)`，原 `upsert()` 委托给私有方法
  - `SQLAlchemyOutboxRepository` 新增 `add_with_session(session, event)` 和私有 `_add_with_session(session, event)`，原 `add()` 委托给私有方法

- [application/services/async_pipeline.py](../application/services/async_pipeline.py)
  - `PipelineStageWorker.__init__` 新增可选参数 `session_factory: SQLAlchemySessionFactory | None = None`
  - 新增私有方法 `_atomic_upsert_and_enqueue(execution, enqueue_fn)`：当 `session_factory` 非 None 且两个 repository 均为 SQLAlchemy 实现时，在同一 `transactional()` session 内完成 execution upsert + outbox add；否则 fallback 到分离调用（兼容内存 repository 的测试场景）
  - `handle()` 中成功路径和 DLQ 路径均改用 `_atomic_upsert_and_enqueue`

- [api/config.py](../api/config.py)
  - `PipelineStageWorker(...)` 构造调用新增 `session_factory=active_session_factory`

**与原计划的偏差**：`_atomic_upsert_and_enqueue` 通过临时替换 `outbox_repository.add` 的方式共享 session，避免了修改 `OutboxRepository` 抽象接口。这比计划里"给 `PipelineStageWorker.handle()` 传入共享 session 参数"的方案侵入性更小，接口更稳定。

**测试结果**：18 个 phase6 async pipeline 测试全部通过。

**状态**：完成

---

#### commit 3 — `c48e6a0` `feat(P0): add OpenAIEmbeddingProvider, replace HashingEmbeddingProvider in prod`

**任务 3（RAG Embedding）实际改动**：

- [application/services/phase8_vectors.py](../application/services/phase8_vectors.py)
  - 新增 `OpenAIEmbeddingProvider`（`text-embedding-3-small`，1536 维），`import openai` 延迟到 `__init__` 内避免不必要的导入开销
  - `HashingEmbeddingProvider` 文档注释标注为 test-only

- [api/config.py](../api/config.py)
  - `create_vector_repository()` 读取 `OPENAI_API_KEY`：有则使用 `OpenAIEmbeddingProvider`，无则 `logger.warning` + fallback 到 `HashingEmbeddingProvider`
  - `OPENAI_API_KEY` 加入 `KNOWN_ENV_VARS`

- [.env.example](../.env.example)
  - 新增 `OPENAI_API_KEY=`（触发了 `test_phase0_env_template_contract` 合约测试，确保模板覆盖所有已知变量）

**待完成（需独立操作窗口）**：Qdrant collection 从 384 维重建为 1536 维。参考任务 3 中的四步迁移 runbook，当前代码侧已就绪，维度切换是运维操作。

**测试结果**：7 个 phase8 测试全部通过，`KNOWN_ENV_VARS` 合约测试通过。

**状态**：代码完成，Qdrant 维度迁移待执行

---

#### commit 4 — `75bf7a7` `refactor(P1): build_runtime_services() returns RuntimeServices dataclass`

**任务 4-Step A（RuntimeServices dataclass）实际改动**：

- [api/service.py](../api/service.py)
  - 新增 `@dataclass class RuntimeServices`，15 个具名字段对应原 tuple 每个位置
  - `build_runtime_services()` 返回值从 15 元素 tuple 改为 `RuntimeServices(...)`
  - `create_app()` 改为 `svc = build_runtime_services(...)`，所有 `app_instance.state.xxx = ...` 改为 `svc.xxx` 属性访问，消除位置解包

**状态**：完成

---

### 待续任务

| 任务 | 状态 | 预计工作量 |
|------|------|-----------|
| 任务 4-Step B：api/service.py 按资源拆分 router | 未开始 | 2 天 |
| 任务 5：Phase 9 字段级一致性校验 | 未开始 | 半天 |
| 任务 6：Pipeline 端到端集成测试 | 未开始 | 1 天 |
| 任务 7：shadow_write 失败可观测性 | 未开始 | 半天 |
| 任务 8：数据库索引（先 EXPLAIN 再补） | 未开始 | 半天 |
| 任务 9：AI 调用 structured output + timeout | 未开始 | 1 天 |
| 任务 10：配置管理迁移 pydantic-settings | 未开始 | 1 天 |
| 任务 11：统一错误处理 | 未开始 | 半天 |
| Qdrant 维度迁移（运维操作） | 待执行 | — |
