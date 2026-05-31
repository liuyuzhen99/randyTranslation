# Phase 8 Review Report

## 结论

Phase 8 已完成 Qdrant migration and retrieval quality validation 的第一版可交付范围。当前系统已经从“保留 VectorRepository 抽象”推进到“可真实写入 Qdrant、可重复 backfill、可跑 retrieval baseline、可通过 `/readyz` 观察 Qdrant 依赖”的状态。

这次实现没有把业务代码直接绑定到 Qdrant SDK，而是保留 `VectorRepository` 边界，并让 Qdrant adapter 支持两种运行模式：

- 安装 `qdrant-client` 时使用 SDK。
- 未安装 SDK 时使用 Qdrant REST API。

这点很关键，因为当前 `.venv` 里没有 `qdrant-client`，但用户已经用 Docker 启动了 Qdrant；REST fallback 让 live drill 可以直接跑通，不需要先改 Python 依赖环境。

## 评审范围

- `VectorRepository` contract 扩展。
- SQLite legacy vector source enumerate/count/search。
- Qdrant adapter。
- Deterministic point ID strategy。
- Local deterministic embedding provider。
- Backfill/parity service。
- Retrieval quality evaluator。
- Backfill 和 quality CLI scripts。
- Live Docker Qdrant drill。
- Phase 7 `/readyz` 中 Qdrant readiness 子检查。

## 核心实现

### VectorRepository Contract

[domain/repositories.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/domain/repositories.py) 中的 `VectorRepository` 现在包含：

- `upsert(record)`
- `list_by_namespace(namespace, limit, offset)`
- `count_by_namespace(namespace)`
- `search(namespace, text, limit)`

新增 list/count 是 Phase 8 migration 的必要能力。没有可分页枚举，就无法做可重复 backfill；没有 count，就无法形成清晰 parity report。

[domain/entities.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/domain/entities.py) 中的 `VectorRecord` 增加了：

- `embedding`
- `score`

这样同一个实体既能表达 legacy source record，也能表达 Qdrant search result。

### Collection Design

Phase 8 定义两个集合：

- `translation_memory`
- `audit_style_memory`

设计文档位于 [docs/phase8-qdrant-design.md](/Users/randy/Documents/code/randyTranslation/randyTranslation/docs/phase8-qdrant-design.md)。Qdrant 被明确定位为 retrieval/index store，不作为权威业务数据库。

### Deterministic IDs

Backfill 使用 deterministic UUID-shaped point ID：

```text
sha256(namespace + ":" + source_vector_id)[0:32] -> UUID shape
```

这保证：

- 同一 source record 多次 backfill 写入同一个 Qdrant point。
- backfill 可以安全重跑。
- parity 和 quality report 可以稳定引用 expected IDs。
- Qdrant REST API 接受 point ID 格式。

### Embedding Provider

[application/services/phase8_vectors.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/application/services/phase8_vectors.py) 新增 `HashingEmbeddingProvider`。它不是生产 embedding 模型，而是为了 migration/parity/test 提供确定性向量：

- 不依赖外部模型下载。
- 单测和 live drill 可重复。
- Qdrant collection dimension 可控。

生产切换前仍需要确定真正的 embedding provider，并把 provider/model/version/dimension 写入 payload metadata 或迁移报告。

### Backfill 和 Parity

`Phase8VectorMigrationService` 负责：

1. 按 namespace 从 source repository 分页读取。
2. 构造 deterministic ID。
3. 生成 embedding。
4. 写入 target repository。
5. 用目标库 search 回查 migrated point。
6. 输出 `VectorBackfillReport`。

脚本入口位于 [scripts/phase8_qdrant_backfill.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/scripts/phase8_qdrant_backfill.py)，支持 dry-run 和 live Qdrant。

### Retrieval Quality Baseline

`Phase8RetrievalQualityEvaluator` 用代表性 case 检查 expected IDs 是否出现在 top-K 结果中。

脚本入口位于 [scripts/phase8_retrieval_quality.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/scripts/phase8_retrieval_quality.py)。当前 baseline 文件位于 [docs/phase8-retrieval-quality-baseline.json](/Users/randy/Documents/code/randyTranslation/randyTranslation/docs/phase8-retrieval-quality-baseline.json)，覆盖：

- translation memory 的 gritty/cadence query。
- translation memory 的 smooth chorus query。
- audit style memory 的 dense wordplay/high energy query。

当前 baseline 是合成代表样本，用来验证迁移和检索机制；生产 cutover 前需要替换或扩展为真实 curated representative dataset。

### Qdrant Adapter

[infrastructure/vector/qdrant_repository.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/infrastructure/vector/qdrant_repository.py) 实现 `QdrantVectorRepository`。

它支持：

- 自动创建 collection。
- upsert point。
- namespace count。
- scroll/list。
- vector search。
- SDK 和 REST fallback。

REST fallback 使用 Qdrant HTTP endpoints，因此在当前 `.venv` 没有 `qdrant-client` 的情况下也可以完成 live drill。

### Config

[api/config.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/api/config.py) 新增 Phase 8 配置：

- `VECTOR_REPOSITORY_BACKEND`
- `VECTOR_EMBEDDING_DIMENSION`
- `QDRANT_COLLECTION_PREFIX`

[.env.example](/Users/randy/Documents/code/randyTranslation/randyTranslation/.env.example) 已同步补齐，配置契约测试也已覆盖。

## Live Drill

用户已用 Docker 启动 Qdrant，容器暴露：

```text
0.0.0.0:6333-6334 -> 6333-6334/tcp
```

沙箱外访问：

```text
http://127.0.0.1:6333/readyz -> all shards are ready
```

Live drill 使用隔离 prefix：

```text
phase8_live_translation_memory
phase8_live_audit_style_memory
```

Backfill 结果：

- `translation_memory`: source_count 2, upserted 2, parity ok。
- `audit_style_memory`: source_count 1, upserted 1, parity ok。

Retrieval baseline：

- 3 / 3 representative cases passed。

Readiness：

- `/readyz` 的 Qdrant 子检查为 `ok`。
- 隔离 readiness 命令整体 HTTP 503 是预期的，因为该命令刻意不配置 RabbitMQ；这不影响 Qdrant 子检查结果。

## 验证结果

已完成测试：

- `test/test_phase8_qdrant_migration.py`：7 passed。
- `test/test_phase0_config_validation.py`：27 passed。
- `test/test_phase0_env_template_contract.py`：1 passed。
- `test/test_phase0_api_baseline.py`：9 passed。
- `test/test_phase1_layered_architecture.py`：11 passed。
- `test/test_phase2_postgres_foundation.py`：19 passed。
- `test/test_phase3_catalog.py`：7 passed。
- `test/test_phase4_workflow.py`：5 passed。
- `test/test_phase5_cos_storage.py`：5 passed。
- `test/test_phase6_async_pipeline.py`：15 passed。
- Phase 8 py_compile：通过。
- Phase 8 dry-run backfill：通过。
- Phase 8 live Qdrant backfill/parity：通过。
- Phase 8 live retrieval quality baseline：通过。
- Phase 8 live Qdrant readiness 子检查：通过。

## 风险与限制

1. 当前 embedding 是 deterministic test baseline  
   `HashingEmbeddingProvider` 适合迁移验证，不适合最终检索质量。生产前必须选定真实 embedding 模型。

2. 当前 representative dataset 是合成样本  
   机制已经具备，但正式质量验收需要用户认可的真实 curated translation/audit memory case。

3. Qdrant collection schema 仍是 payload-flexible  
   这适合迁移初期兼容 legacy Chroma/SQLite source，但后续应该把 payload 中的关键字段和 embedding model metadata 固化。

4. Live drill 使用隔离 collection  
   这避免污染真实数据，但也意味着还没有对完整历史 vector corpus 做 backfill。

## 后续建议

- 准备真实 curated retrieval quality dataset，并把 expected IDs 纳入 review gate。
- 选定生产 embedding provider，记录 model/version/dimension。
- 对完整 legacy Chroma/SQLite vector corpus 跑 backfill。
- 在 Phase 9 dual-write/cutover 前，对 Qdrant target count、sample IDs、top-K quality 做持续 parity report。

## 总体评价

Phase 8 的实现方向是正确的：Qdrant 被接入为可替换 adapter，而不是侵入业务服务；backfill 是幂等和可重复的；quality gate 是显式脚本和文档化 case，而不是人工临时试查。

这已经满足 roadmap 中 Phase 8 的核心交付：Qdrant repositories、migration scripts、retrieval parity and quality report。后续进入 Phase 9 前，需要把当前合成 baseline 升级为真实 curated baseline，并用生产 embedding 模型重跑一轮质量验收。
