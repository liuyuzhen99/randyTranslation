# 工程质量提升改动计划 P2

**基于**: `docs/project-upgrade-plan-20260510.md` P0/P1 后续阶段  
**评审修订日期**: 2026-05-11  
**原计划修订原因**: 代码实测后发现 6 处关键错误，见各任务"修订原因"说明

---

## 任务 1：数据库索引 — outbox 复合索引

**问题**：`outbox` 表的 `list_pending()` 按 `status='pending' ORDER BY event_id ASC` 查询（[sqlalchemy_repositories.py:954](../infrastructure/persistence/sqlalchemy_repositories.py#L954)），现有只有 `ix_outbox_status`（单列），缺少 `(status, event_id)` 复合索引。

**修订**：原计划将表名写为 `outbox_events`，实际表名为 `outbox`（[sqlalchemy_models.py:268](../infrastructure/persistence/sqlalchemy_models.py#L268)）。索引名不变，只修正表名描述。

**改动文件**：
- `infrastructure/persistence/sqlalchemy_models.py` — `OutboxModel.__table_args__` 添加复合索引（约第 269 行）
- 新建 Alembic migration

**步骤**：

1. `OutboxModel.__table_args__` 中添加：
```python
Index("ix_outbox_status_event_id", "status", "event_id"),
```

2. 生成 migration：
```bash
alembic revision --autogenerate -m "add outbox status event_id composite index"
```

3. 检查生成的 migration 文件含 `create_index("ix_outbox_status_event_id", "outbox", ["status", "event_id"])`

**验证**：migration 文件生成无报错，包含正确的 `op.create_index` 操作

---

## 任务 2：AI 调用加 timeout + retry（移除 structured output）

**问题**：`core/aiTranslator.py` 的 DeepSeek 调用无 timeout（AI 超时会 hang 整个 stage）、无 retry（瞬时故障即失败）。

**修订（关键）**：原计划要加 Pydantic `_TranslationOutput` schema，但当前 prompt 要求模型返回 `<R1>...</R1>` XML 格式，非 JSON。在不改 prompt 的前提下，`response_format={"type":"json_object"}` 不会生效，Pydantic schema 无法校验字符串输出——两者混用会导致 fallback 路径永远生效、结构化校验形同虚设。**本次只加 timeout + retry，不改输出格式和解析逻辑。**

`tenacity==9.1.4` 已在 `requirements.txt`，无需新增依赖。

**改动文件**：
- `core/aiTranslator.py` — 提取 `_call_deepseek` 私有方法，加 timeout + retry 装饰器
- 新建 `prompts/translation_v1.txt` — 提取 prompt template，用绝对路径加载

**步骤**：

1. 在 `aiTranslator.py` 顶部添加：
```python
from tenacity import retry, stop_after_attempt, wait_exponential
```

2. 将 `self.client.chat.completions.create(...)` 调用提取到私有方法：
```python
@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
def _call_deepseek(self, prompt: str) -> str:
    response = self.client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": "You are a lyric synchronization expert."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        stream=False,
        timeout=30,
    )
    return response.choices[0].message.content
```

3. 将 prompt template（`f"""你是一个专业的...{anchored_block}"""`）提取到 `prompts/translation_v1.txt`，占位符保留 `{dynamic_few_shot}` 和 `{anchored_block}`。

4. 加载方式使用锚定路径（**不用 `Path("prompts/...")` 相对路径**）：
```python
_PROMPT_TEMPLATE = (Path(__file__).parent.parent / "prompts" / "translation_v1.txt").read_text(encoding="utf-8")
```
在 `generate_bilingual_srt` 中用 `_PROMPT_TEMPLATE.format(dynamic_few_shot=..., anchored_block=...)` 替换原 f-string。

5. 替换原 `self.client.chat.completions.create(...)` 调用为 `self._call_deepseek(prompt)`。

**验证**：mock `_call_deepseek` 抛 `openai.APIError` 时，确认 retry 触发 3 次后向上传播异常

---

## 任务 3：配置管理迁移到 pydantic-settings

**问题**：`api/config.py` 的 `load_runtime_settings()` 手动解析 40+ 环境变量（约 120 行重复 `source.get(...)` 逻辑）。`pydantic-settings==2.13.1` 已在 `requirements.txt`。

**修订（关键）**：原计划写 `AppRuntimeSettings.model_validate(dict(environ))` 用于测试注入，这是错误的——`BaseSettings.model_validate` 仍会读取 `os.environ`（环境变量源优先级问题）。正确方式：构建字段名映射后直接传构造参数，或在测试中 `patch.dict(os.environ, environ)`（测试已经这样做了，见 `test_phase0_config_validation.py`），所以 `load_runtime_settings` 的测试注入路径只需：
```python
# environ 传入时，pydantic-settings 通过 _env_settings_source 读取，
# 但需要用 model_config 中的 env_parse_enums 支持；
# 更简单：直接 mock os.environ（测试已在做），让 AppRuntimeSettings() 自动读取
```

另：原计划未提到 `PHASE6_MAX_STAGE_ATTEMPTS >= 1` 的 runtime 校验，需在 `@field_validator` 中保留。

**改动文件**：`api/config.py`

**步骤**：

1. `AppRuntimeSettings` 从 `@dataclass(frozen=True)` 改为继承 `pydantic_settings.BaseSettings`：
```python
from pydantic_settings import BaseSettings, SettingsConfigDict

class AppRuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )
    # 字段名需与环境变量名（小写）对应，pydantic-settings 自动映射大写 env key
    job_repository_backend: str = "memory"
    # ... 其余字段与现有 dataclass 完全相同
```

2. 将 POSTGRES_* 组合 DATABASE_URL 的逻辑迁移到 `@model_validator(mode="after")`

3. 将 backend 合法值校验迁移到 `@field_validator`（`job_repository_backend`, `media_storage_backend`, `vector_repository_backend`, `phase9_cutover_read_source`）

4. 保留 `PHASE6_MAX_STAGE_ATTEMPTS >= 1` 校验（`@field_validator("phase6_max_stage_attempts")`）

5. `load_runtime_settings(environ)` 改为：
   - 若 `environ` 为 None：`return AppRuntimeSettings()`（pydantic-settings 自动读 .env + os.environ）
   - 若传入自定义 environ（仅测试使用）：将 dict 转为小写键再传构造参数：
     ```python
     field_map = {k.lower(): v for k, v in environ.items()}
     return AppRuntimeSettings.model_validate(field_map)
     ```
     注意：此处 `model_validate` 接收的是字段名（小写），而非环境变量名，pydantic 不会再读 os.environ，行为正确。

6. 删除 `_read_non_negative_int`、`_read_non_negative_float`、`_read_bool` helper 函数

7. `validate_startup_env()` 保持不变

**验证**：
```bash
.venv/bin/python -c "from api.config import AppRuntimeSettings; print(AppRuntimeSettings())"
.venv/bin/python -m pytest test/test_phase0_config_validation.py -xvs
```

---

## 任务 4：统一错误处理

**问题**：`api/routers/pipeline.py` 中混用 `HTTPException(404)` 和 `KeyError` 转 404 等模式。

**修订**：原计划引用 `api/service.py:511` 和 `:543`——实测确认 `create_app()` 在 [service.py:403](../api/service.py#L403)，`exception_handler(HTTPException)` 在 [:511](../api/service.py#L511)，`exception_handler(Exception)` 在 [:543](../api/service.py#L543)，行号正确。

除 `check_status` 的 line 284 外，pipeline.py 还有 line 314、367、389、402 共 4 处 `HTTPException(404)`。本次只改 `check_status`（line 284）作为示范，其余在 task 5 测试验证后再逐步迁移，避免一次性改动过多影响测试稳定性。

**改动文件**：
- `domain/exceptions.py` — 扩展异常类
- `api/service.py` — 注册 `NotFoundError` 全局 handler
- `api/routers/pipeline.py` — `check_status` 的 404 改为领域异常

**步骤**：

1. `domain/exceptions.py` 添加：
```python
class NotFoundError(Exception):
    def __init__(self, resource: str, id: str) -> None:
        self.resource = resource
        self.id = id
        super().__init__(f"{resource} not found: {id}")

class ConflictError(Exception):
    pass

class DomainValidationError(Exception):
    pass
```

2. `api/service.py` 的 `create_app()` 中，在 `@app_instance.exception_handler(HTTPException)` 之前插入：
```python
from domain.exceptions import NotFoundError
from fastapi.responses import JSONResponse

@app_instance.exception_handler(NotFoundError)
async def not_found_handler(request: Request, exc: NotFoundError):
    return JSONResponse(
        status_code=404,
        content={"code": "not_found", "resource": exc.resource, "id": exc.id},
    )
```

3. `api/routers/pipeline.py` 的 `check_status`（line 284）：
```python
# 改前
raise HTTPException(status_code=404, detail="任务不存在")
# 改后
from domain.exceptions import NotFoundError
raise NotFoundError("job", task_id)
```

**验证**：
```bash
.venv/bin/python -m pytest test/test_phase0_api_baseline.py -k "check_status_not_found" -xvs
```
（需同时更新测试断言，见任务 5）

---

## 任务 5：修复 3 个预存测试失败

**文件**：`test/test_phase0_api_baseline.py`

**修订（关键）**：实测后确认 3 个测试的真实失败原因，原计划猜测原因不准确：

### test_create_task_contract（line 41）
**真实失败原因**：断言 `data["message"] == "任务已启动，请稍后通过 ID 查询进度"` 但 P1 后默认走 phase6 async 路径，实际返回 `"任务已写入异步 pipeline，请稍后通过 ID 查询进度"`（pipeline.py:262）。  
**修复**：更新断言匹配实际 message，或明确将测试环境的 `PHASE6_ASYNC_PIPELINE_ENABLED=false` 以固定测试路径（推荐后者，语义更清晰）。

### test_phase2_outbox_dispatch_endpoint_returns_503_without_real_publisher（line 152）
**真实失败原因**：实测这个测试 **通过**，无需修复。（`publisher=None` 时 `create_phase2_outbox_dispatcher` 返回 `None`，endpoint 返回 503 符合预期）

### test_phase2_outbox_dispatch_endpoint_works_with_injected_publisher（line 175）
**真实失败原因**：`attempted=0 != 1`。根本原因：`create_task` handler 在写入 outbox 后会调用 `_dispatch_outbox_if_available(outbox_dispatcher)`（pipeline.py:260），由于测试注入了 `Publisher()`，outbox event 在 create_task 时已被即时消费、状态变更为非 pending，后续显式调用 `/internal/phase2/outbox/dispatch` 找不到 pending 事件，返回 `attempted=0`。  
**修复**：测试改为断言 `published_calls` 长度（而非 endpoint 的 `attempted`），或将 `PHASE6_ASYNC_PIPELINE_ENABLED=false` 避免 create_task 触发 inline dispatch，让 outbox event 留给显式 dispatch endpoint 消费。推荐后者，验证 dispatch endpoint 的独立功能。

**步骤**：

1. `test_create_task_contract`：在 `env` dict 中加 `"PHASE6_ASYNC_PIPELINE_ENABLED": "false"`，同时更新 `message` 断言为 `"任务已启动，请稍后通过 ID 查询进度"`（或反过来，固定期望 phase6 路径）

2. `test_check_status_not_found`（line 62）：任务 4 完成后，需将断言从 `{"detail": "任务不存在"}` 更新为 `{"code": "not_found", "resource": "job", "id": ...}`

3. `test_phase2_outbox_dispatch_endpoint_works_with_injected_publisher`：在 `env` dict 中加 `"PHASE6_ASYNC_PIPELINE_ENABLED": "false"` 以禁止 create_task 的 inline dispatch，保留 `outbox_publisher=Publisher()` 注入

**验证**：
```bash
.venv/bin/python -m pytest test/test_phase0_api_baseline.py -xvs
```

---

## 任务 6：RAG embedding 换为 BAAI/bge-m3（替代原计划的 all-MiniLM-L6-v2）

**问题**：`OpenAIEmbeddingProvider` 使用 `text-embedding-3-small`（1536-dim），需 OpenAI API key。

**修订（关键）**：原计划推荐 `all-MiniLM-L6-v2`，但该模型中文词汇表极少，中文语义质量接近随机，不适合本项目（translation_memory 是中英双语内容，user_taste_v1 是混合语言风格描述）。  
**改为推荐 `BAAI/bge-m3`（1024-dim，MIT license）**，理由：
- 中英双语 SOTA，适合 translation_memory 的跨语言检索
- 支持 8192 token 长文本，适合完整歌词片段
- 维度 1024，语义空间足够丰富，对 music taste 风格相似度检索效果更好

**⚠️ 维度变更注意**：切换到 bge-m3（1024-dim）前必须通过 Qdrant dashboard/API 的 `collection_info` 确认当前 collection 实际维度。若生产 Qdrant 曾用 OpenAI 写入数据，当前维度通常是 1536，必须重建 collection 并用 `scripts/phase8_qdrant_backfill.py` 重跑 backfill。若生产一直走 `HashingEmbeddingProvider` fallback，当前维度通常是 384，也必须重建为 1024 后再 backfill。只有已确认 collection 维度为 1024 时，才允许跳过维度重建。

**改动文件**：
- `application/services/phase8_vectors.py` — 新增 `BGEEmbeddingProvider` 类
- `api/config.py` — `create_vector_repository()` 替换为优先用 `BGEEmbeddingProvider`；`VECTOR_EMBEDDING_DIMENSION` 默认值改为 1024
- `requirements.txt` — 添加 `sentence-transformers>=3.0.0`

**步骤**：

1. `application/services/phase8_vectors.py` 添加：
```python
class BGEEmbeddingProvider:
    """Local open-source embedding using BAAI/bge-m3 (1024-dim, MIT license).

    Bilingual (ZH+EN) SOTA, suited for translation memory and music taste retrieval.
    Imports are deferred to avoid startup cost when using HashingEmbeddingProvider.
    """

    MODEL = "BAAI/bge-m3"
    dimension = 1024

    def __init__(self, model: str = MODEL) -> None:
        import torch
        from transformers import AutoModel, AutoTokenizer
        self._torch = torch
        self._tokenizer = AutoTokenizer.from_pretrained(model)
        self._model = AutoModel.from_pretrained(model)
        self._model.eval()

    def embed(self, text: str) -> list[float]:
        ...
```

2. `api/config.py` 的 `AppRuntimeSettings.vector_embedding_dimension` 默认值改为 `1024`

3. `api/config.py` 的 `create_vector_repository()` 修改为：
```python
if settings.vector_repository_backend == "qdrant":
    from application.services.phase8_vectors import BGEEmbeddingProvider
    embedding_provider = BGEEmbeddingProvider()
    return QdrantVectorRepository(...)
```
删除 `openai_api_key` 判断逻辑和 `OpenAIEmbeddingProvider` 导入，删除 HashingEmbeddingProvider fallback 警告（fallback 留给异常处理）。

4. `requirements.txt` 添加：
```
sentence-transformers>=3.0.0
```

5. 检查 `test/test_phase8_qdrant_migration.py`，确认测试使用 `HashingEmbeddingProvider`，不受影响

**验证**：
```bash
.venv/bin/python -c "
from application.services.phase8_vectors import BGEEmbeddingProvider
p = BGEEmbeddingProvider()
v = p.embed('test')
print(len(v))  # 预期: 1024
"
```

---

## 执行顺序

| 批次 | 任务 | 原因 |
|------|------|------|
| 并行批次 A | 1、2、4 | 互不依赖，文件无冲突 |
| 串行 B1 | 3（config 全文重构） | 修改 config.py 主体，与任务 6 有文件冲突 |
| 串行 B2 | 6（embedding + config.py 局部） | 依赖 B1 完成后的 config.py 状态 |
| 串行 C | 5（测试修复） | 依赖任务 4 的 NotFoundError handler 已存在 |

---

## 关键文件索引

| 任务 | 主要文件 | 实际行号 |
|------|----------|----------|
| 1 | `infrastructure/persistence/sqlalchemy_models.py` | :269（OutboxModel.__table_args__） |
| 2 | `core/aiTranslator.py`, `prompts/translation_v1.txt`（新建） | :183（原 create 调用） |
| 3 | `api/config.py` | :129（AppRuntimeSettings）, :161（load_runtime_settings） |
| 4 | `domain/exceptions.py`, `api/service.py`, `api/routers/pipeline.py` | service.py:511, pipeline.py:284 |
| 5 | `test/test_phase0_api_baseline.py` | :41, :62, :175 |
| 6 | `application/services/phase8_vectors.py`, `api/config.py`, `requirements.txt` | phase8_vectors.py:187（新增类） |
