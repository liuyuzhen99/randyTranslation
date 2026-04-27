# Phase 6 Review Report

## 结论

Phase 6 的异步流水线主干已经交付：后端完成了 RabbitMQ 拓扑、阶段消息契约、阶段执行记录、幂等 worker、事务型 outbox 发布路径、人工审核闸门恢复逻辑，以及前端 Pipeline 页面中的异步执行状态展示。经过本地 RabbitMQ、Postgres 迁移、后端 API、前端 BFF 与浏览器页面联调，核心链路可以从命令消息进入 worker，完成阶段执行记录，并将下一阶段消息写入队列。

本阶段当前达到“可联调、可演示、具备生产骨架”的状态，但仍有几项生产化收尾建议：AI/RAG 审核逻辑仍是占位实现，延迟重试调度还需要正式的 delayed delivery 机制，worker 运行方式需要纳入部署脚本或 Compose，RabbitMQ 拓扑和 worker 健康检查也建议接入 CI/CD 与运维监控。

## 评审范围

- 后端 Phase 6 异步流水线设计与实现。
- RabbitMQ 拓扑声明、发布、消费、ack/nack 行为。
- Pipeline 阶段执行持久化和幂等性。
- 人工审核与翻译审核两个暂停/恢复闸门。
- API 与前端 Pipeline 页面联调展示。
- 本地 RabbitMQ、Postgres 迁移、浏览器端到端烟测。

## 核心实现

### 队列拓扑与消息契约

队列拓扑集中在 [domain/queue_topology.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/domain/queue_topology.py)，使用一个 durable direct exchange `pipeline`，并定义了命令队列、各阶段队列和 DLQ：

- `pipeline.command`
- `pipeline.stage.download`
- `pipeline.stage.transcribe`
- `pipeline.stage.audit`
- `pipeline.stage.manual_review`
- `pipeline.stage.translate`
- `pipeline.stage.translation_review`
- `pipeline.stage.render`
- `pipeline.dlq`

阶段顺序由 `STAGE_ORDER` 和 `next_stage` 固化，避免 worker 侧分散硬编码。消息契约位于 [domain/message_contracts.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/domain/message_contracts.py)，新增 `PipelineStageMessage`、`RetryContext` 和 `ReviewContext`，为阶段执行、重试次数、审核上下文提供稳定结构。

### 阶段执行记录与幂等性

新增的 `PipelineStageExecution` 位于 [domain/entities.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/domain/entities.py)，仓储接口位于 [domain/repositories.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/domain/repositories.py)，SQLAlchemy 实现位于 [infrastructure/persistence/sqlalchemy_repositories.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/infrastructure/persistence/sqlalchemy_repositories.py)。

`PipelineStageWorker` 在 [application/services/async_pipeline.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/application/services/async_pipeline.py) 中负责阶段幂等控制。它会以消息和阶段为粒度记录执行状态，已完成的消息再次投递时会走 duplicate ack，不重复执行业务副作用；失败超过最大次数后写入 DLQ outbox；可重试失败则记录 retry 状态并写入下一次投递消息。

该设计把 RabbitMQ 的“至少一次投递”转化为业务侧“有效一次执行”，这是 Phase 6 里最关键的可靠性基础。

### 事务型 Outbox 发布

Phase 6 没有在业务事务中直接调用 RabbitMQ。命令入队、下一阶段入队、DLQ 入队都通过 outbox 写入，再由 dispatcher 发布。相关 wiring 在 [api/config.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/api/config.py)，当 `PHASE6_ASYNC_PIPELINE_ENABLED=true` 且配置了 `RABBITMQ_URL` 时，会使用 RabbitMQ publisher。

这个路径能保证“业务状态提交”和“消息待发布记录”处于同一个数据库事务边界里，避免 API 已返回但消息丢失，或消息已发但业务记录未落库的常见问题。

### RabbitMQ 基础设施

RabbitMQ 相关实现位于 [infrastructure/messaging](/Users/randy/Documents/code/randyTranslation/randyTranslation/infrastructure/messaging)：

- `rabbitmq_topology.py`：声明 exchange、queue、binding 和 DLX 设置。
- `rabbitmq_publisher.py`：发布 outbox 消息到 RabbitMQ。
- `rabbitmq_consumer.py`：消费单个队列，执行 worker 后 ack，未持久化异常时 nack 到 DLQ。

worker CLI 位于 [workers/phase6_worker.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/workers/phase6_worker.py)，支持 `--declare-only` 声明拓扑，也支持 `--queue` 指定消费队列。declare-only 模式默认使用本地 `amqp://guest:guest@localhost:5672/`，方便本机 RabbitMQ 快速烟测。

### 阶段处理器

阶段处理器集中在 [application/services/pipeline_stage_handlers.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/application/services/pipeline_stage_handlers.py)。目前包含：

- `download`：下载与准备媒体。
- `transcribe`：调用 producer transcribe，写入 transcript，并自动生成通过的 transcript review。
- `audit`：写入 taste audit 审核占位结果，并进入人工审核闸门。
- `manual_review_gate`：如果人工审核仍 pending，则暂停流水线。
- `translate`：生成双语字幕，并写入翻译审核。
- `translation_review_gate`：如果翻译审核仍 pending，则暂停流水线。
- `render`：烧录/封装视频，上传产物，并写入 artifact metadata。

这里的工程拆分是合理的：worker 只负责编排、幂等、重试和消息推进；具体业务副作用由 handler 承担。需要注意的是，`audit` 当前仍是 workflow placeholder，尚未接入真实 AI/RAG taste audit。

### 审核闸门与恢复

API 在审核通过时会检测 Phase 6 开关，并根据审核类型恢复流水线：

- `manual_review` 通过后，入队 `translate`。
- `translation_review` 通过后，入队 `render`。

这部分逻辑位于 [api/service.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/api/service.py)。恢复时会读取最近阶段执行记录的 `result_payload`，尽量保留 `segments`、`video_ref`、`subtitle_file` 等上下文，避免人工审核暂停后丢失后续阶段需要的输入。

### API 与前端联调

后端 `/v1/pipeline` 现在会附带 `async_execution` 字段，包含当前 worker 阶段、状态、尝试次数、最大尝试次数、更新时间和暂停原因。前端 BFF 在 [src/app/api/pipeline/route.ts](/Users/randy/Documents/code/randyTranslation/vibeFrontTranslation/auditflow-app/src/app/api/pipeline/route.ts) 中将其转为 camelCase 的 `asyncExecution`。

前端类型、schema、adapter 和展示分别更新：

- [src/types/pipeline.ts](/Users/randy/Documents/code/randyTranslation/vibeFrontTranslation/auditflow-app/src/types/pipeline.ts)
- [src/lib/schemas/pipeline.ts](/Users/randy/Documents/code/randyTranslation/vibeFrontTranslation/auditflow-app/src/lib/schemas/pipeline.ts)
- [src/lib/adapters/pipeline.ts](/Users/randy/Documents/code/randyTranslation/vibeFrontTranslation/auditflow-app/src/lib/adapters/pipeline.ts)
- [src/lib/status/pipeline.ts](/Users/randy/Documents/code/randyTranslation/vibeFrontTranslation/auditflow-app/src/lib/status/pipeline.ts)
- [src/components/features/pipeline/pipeline-dashboard-client.tsx](/Users/randy/Documents/code/randyTranslation/vibeFrontTranslation/auditflow-app/src/components/features/pipeline/pipeline-dashboard-client.tsx)

浏览器联调确认 Pipeline 页面可以展示 `Worker: Stage done` badge，展开后能看到 `Worker Execution` 详情，以及 `manual_review · attempt 1/3 · manual review pending` 等信息。

## 迁移与联调发现

联调过程中暴露了三类 Postgres 迁移问题，并已通过增量迁移修复：

- Phase 6 表缺失：新增 [20260427_120000_phase6_async_pipeline_schema.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/alembic/versions/20260427_120000_phase6_async_pipeline_schema.py) 创建 `pipeline_stage_executions`。
- 阶段字段长度不足：新增 [20260427_130000_phase6_stage_column_lengths.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/alembic/versions/20260427_130000_phase6_stage_column_lengths.py) 调整 stage/status 字段长度。
- 旧枚举 check constraint 不包含新增阶段：新增 [20260427_140000_phase6_stage_constraints.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/alembic/versions/20260427_140000_phase6_stage_constraints.py) 更新 Postgres 约束。

这些问题说明 Phase 6 的 enum 扩展不仅要改 Python domain 层，也必须同步检查数据库 check constraint。当前迁移对 SQLite 做了兼容跳过，对 Postgres 执行真实约束调整。

## 验证结果

已完成的后端测试：

- `test_phase0_config_validation.py`
- `test_phase1_layered_architecture.py`
- `test_phase2_postgres_foundation.py`
- `test_phase3_catalog.py`
- `test_phase4_workflow.py`
- `test_phase5_cos_storage.py`
- `test_phase6_async_pipeline.py`

已完成的前端验证：

- `npm run typecheck`
- `npm test -- --run src/components/features/pipeline/pipeline-dashboard-client.test.tsx src/lib/schemas/domain-schemas.test.ts src/lib/adapters/index.test.ts`

已完成的集成烟测：

- 本机 RabbitMQ 拓扑声明成功，结果为 `{'exchange': 'pipeline', 'queues': 9}`。
- RabbitMQ round-trip 成功：`pipeline.command` 被发布，consumer 处理命令，`download` 阶段完成，并发布下一阶段到 `pipeline.stage.transcribe`。
- 后端 API 与前端 BFF 联通，浏览器访问 `http://localhost:3000/pipeline` 后不再出现 backend unavailable。
- Pipeline 页面成功展示 worker 异步执行状态。

## 风险与限制

1. AI/RAG audit 尚未生产化  
   `audit` 阶段目前是 workflow placeholder，会写入通过状态并推进人工审核。真实 taste audit 需要接入 Phase 5 的检索、评分和可解释结果。

2. 延迟重试还不是完整调度能力  
   当前 worker 会记录 retry 和 backoff 上下文，但生产环境建议接入 RabbitMQ delayed exchange、TTL retry queue 或独立 scheduler，保证重试延迟可控且可观测。

3. Worker 部署仍需补齐  
   CLI 已可运行，但还需要把多个阶段 worker 的启动方式纳入 Docker Compose、systemd、launchd 或生产编排系统，并配置并发、prefetch、日志和健康检查。

4. Queue observability 需要继续增强  
   建议为队列长度、DLQ 数量、阶段耗时、失败率、重试次数、暂停数量增加 metrics 和 dashboard。

5. 审核恢复依赖 result payload 完整性  
   当前恢复逻辑会复用最近阶段执行记录中的 payload。后续如果 handler payload 结构继续扩展，建议为每个阶段的输入/输出加 schema 测试，避免人工审核暂停后恢复缺字段。

## 后续建议

- 将 `audit` handler 接入真实 AI/RAG taste audit，并把审核解释写入 review metadata。
- 为 retry 引入正式 delayed delivery 机制。
- 增加 worker 运行文档和一键启动脚本。
- 在 CI 中增加 Alembic upgrade head、Phase 6 单测和前端 Pipeline schema 测试。
- 增加 `/internal/phase6` 或运维页面的队列健康、DLQ 查看、手动重放能力。
- 为 Phase 6 增加一条更完整的端到端测试：创建任务、跑到 manual review 暂停、审批恢复、跑到 translation review 暂停、审批恢复、render 产物落库。

## 总体评价

Phase 6 的实现方向是健康的：它没有把异步化做成 API 侧的后台线程，而是建立了真正可扩展的消息队列、outbox、幂等执行表和 worker 模型。前端也已经能把异步执行状态反馈给用户，避免流水线进入 worker 后变成黑盒。

剩余工作主要是生产化深水区：真实 AI 审核、延迟重试、部署监控和更完整的端到端覆盖。以当前代码状态继续推进，这些工作可以在现有架构上增量完成，不需要推翻 Phase 6 主体设计。
