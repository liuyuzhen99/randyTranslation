# Phase 6 总结：RabbitMQ Async Pipeline and Idempotency

## 一、改造前的状态

Phase 5 结束后，系统已经具备对象存储语义：

- pipeline 产物会上传到本地 OSS-style adapter 或腾讯 COS。
- artifact metadata 会写入 PostgreSQL。
- Library / artifact preview BFF 可以通过 artifact 状态和预签名 URL 工作。

但任务执行本身仍然是进程内后台任务：

- `/create_task` 创建 job 后直接通过 FastAPI `BackgroundTasks` 调用 `PipelineOrchestrator.run`。
- 一个 job 的 download、transcribe、translate、render 都在一个 Python 调用栈里串行执行。
- 如果 API 进程重启，后台任务可能丢失。
- 阶段级 retry、消息重放、DLQ、ack/nack 还没有明确模型。
- outbox 虽然在 Phase 2 建立了原型，但还没有成为 pipeline command 的唯一发布路径。

这对生产化有明显风险：视频和音频处理耗时长、资源重、失败点多，如果继续绑定在 API 进程内，系统很容易出现“请求已经返回但任务丢了”“失败后重复执行产生副作用”“重放消息导致 timeline 重复”等问题。

## 二、为什么要这样改

Phase 6 的目标是把执行模式从进程内调用推进到可靠的消息驱动模型。

本轮没有假设本机已经有可用 RabbitMQ，而是先完成可测试的核心边界：

- 定义稳定的 queue topology。
- 定义 versioned pipeline stage message contract。
- 让 API/application 只写 outbox，不直接 publish。
- 用 `pipeline_stage_executions` 表记录每个 stage 的 dedupe key、attempt、状态和错误。
- worker 通过数据库幂等记录决定 ack、retry、DLQ 或跳过重复消息。
- RabbitMQ publisher 作为可选 infrastructure adapter，仍然挂在 outbox dispatcher 后面。

这样做的原因是：Phase 6 最重要的不是“能连上 RabbitMQ 发一条消息”，而是保证消息系统接入后不会破坏业务一致性。RabbitMQ 可以替换或延迟部署，但 outbox、幂等、重放、DLQ 的语义必须先稳定。

## 三、当前实现内容

### 1. 队列拓扑

新增：

```text
domain/queue_topology.py
```

定义：

```text
pipeline.command
pipeline.stage.download
pipeline.stage.transcribe
pipeline.stage.audit
pipeline.stage.manual_review
pipeline.stage.translate
pipeline.stage.translation_review
pipeline.stage.render
pipeline.dlq
```

同时定义 `STAGE_ORDER` 和 `next_stage`，worker 完成一个阶段后可以按顺序创建下一阶段消息。

### 2. 消息契约

`domain/message_contracts.py` 新增：

- `PipelineStageMessage`
- `RetryContext`
- `ReviewContext`

消息包含：

- `schema_version`
- `message_type`
- `job_id`
- `stage`
- `song_name`
- `dedupe_key`
- `trace_id`
- retry attempt / max attempts / backoff seconds
- candidate / review context
- stage payload

这让 review gate、retry、trace、stage replay 都有明确字段，不需要 worker 从松散 JSON 中猜语义。

### 3. 幂等执行记录

新增领域实体：

```text
PipelineStageExecution
```

新增 repository contract：

```text
PipelineStageExecutionRepository
```

新增 SQLAlchemy model 和 repository：

```text
PipelineStageExecutionModel
SQLAlchemyPipelineStageExecutionRepository
```

新增 Alembic migration：

```text
alembic/versions/20260427_120000_phase6_async_pipeline_schema.py
```

新表：

```text
pipeline_stage_executions
```

关键约束：

- `dedupe_key` 唯一。
- `attempt >= 0`。
- `max_attempts >= 1`。
- `job_id + stage` 索引。
- `status + next_retry_at` 索引，用于后续 retry scheduler。

### 4. Async pipeline application service

新增：

```text
application/services/async_pipeline.py
```

核心类：

- `AsyncPipelineCommandService`
- `PipelineStageWorker`
- `WorkerResult`

`AsyncPipelineCommandService` 只负责写 outbox：

- `enqueue_first_stage`
- `enqueue_stage`
- `enqueue_dlq`

它不会绕过数据库直接 publish，符合 transactional outbox 的要求。

`PipelineStageWorker` 负责处理单条 stage message：

- 第一次处理时写 `processing` execution。
- handler 成功后写 `completed`，并创建下一阶段 outbox message。
- 重复收到已完成消息时返回 `ack_duplicate`，不重复执行 handler，也不重复写下一阶段。
- handler 失败但未超过最大次数时写 `retry_scheduled`，按指数退避创建下一次 attempt 的 outbox message。
- handler 失败且达到最大次数时写 `dlq`，创建 `pipeline.dlq` outbox message，并把 job 标记为 failed。
- 重复收到已安排 retry 或已进入 DLQ 的旧消息时直接 ack 现有状态，避免重复 side effect。

### 5. RabbitMQ publisher / topology / consumer adapter

新增：

```text
infrastructure/messaging/rabbitmq_publisher.py
infrastructure/messaging/rabbitmq_topology.py
infrastructure/messaging/rabbitmq_consumer.py
workers/phase6_worker.py
```

`RabbitMQPublisher` 使用 `pika` 发布 durable message，但它只实现 `OutboxPublisher` 边界，不会被 API 直接调用。

`RabbitMQTopologyManager` 负责声明：

- durable direct exchange：`pipeline`
- command queue
- 7 个 stage queue
- DLQ
- 非 DLQ queue 的 dead-letter exchange / routing key

`RabbitMQWorkerConsumer` 负责：

- `basic_qos(prefetch_count=1)`，默认每个 worker 一次只拿一条消息。
- 成功持久化 worker outcome 后 `basic_ack`。
- worker 自己无法持久化 retry/DLQ 结果时 `basic_nack(requeue=False)`，让 broker dead-letter。
- worker 成功处理后触发 outbox dispatcher，把下一阶段 outbox message 发布到 RabbitMQ。

新增 worker CLI：

```text
python workers/phase6_worker.py --declare-only
python workers/phase6_worker.py --queue pipeline.command
python workers/phase6_worker.py --queue pipeline.stage.transcribe
python workers/phase6_worker.py --queue pipeline.stage.transcribe --instances 4 --prefetch 1
python workers/phase6_worker.py --schedule-retries --retry-limit 100
```

`--declare-only` 只声明拓扑，不需要 DB；正式消费需要 `DATABASE_URL` 和 `PHASE6_ASYNC_PIPELINE_ENABLED=true`。

CLI 现在支持 `--instances` 在同一个队列上启动多个 worker 进程，也支持 `--prefetch` 控制单实例并发拉取。`--schedule-retries` 是 DB-backed delayed retry scheduler 的一次性运行入口，可由 launchd 或 cron 每分钟触发。

新增运行手册：

```text
docs/phase6-worker-runbook.md
```

手册覆盖非 Docker 场景下的环境变量、拓扑声明、单队列 worker、多实例 worker、bounded smoke drain、DB retry scheduler 和 launchd 示例。

新增依赖：

```text
pika==1.3.2
```

如果 `PHASE6_ASYNC_PIPELINE_ENABLED=true` 且配置了 `RABBITMQ_URL`，`create_phase2_outbox_dispatcher` 可以创建 RabbitMQ publisher。这个分支已经不再依赖 `PHASE2_OUTBOX_DISPATCH_ENABLED=true`，避免 Phase 6 打开后还无法 dispatch outbox。

### 6. API 接入

新增配置：

```text
PHASE6_ASYNC_PIPELINE_ENABLED=false
PHASE6_MAX_STAGE_ATTEMPTS=3
PHASE6_RETRY_BACKOFF_BASE_SECONDS=30
```

当 `PHASE6_ASYNC_PIPELINE_ENABLED=true`：

- `/create_task` 创建 job 后只写 `pipeline.command` outbox，不再启动进程内后台任务。
- `/v1/candidates/{candidate_id}/render` 同样写异步 command outbox。
- `/v1/reviews/{review_id}/approve` 在人工审核通过后会根据 review type 恢复 Phase 6 worker：
  - `manual_review` approve 后 enqueue `translate`
  - `translation_review` approve 后 enqueue `render`
- `/v1/pipeline` 会为每个 candidate 附加 `async_execution`，包含 worker job、stage、attempt、retry/DLQ/error/pause 信息。

新增内部验证接口：

```text
GET /internal/phase6/queue-topology
POST /internal/phase6/worker/handle
```

这些接口用于本阶段 smoke / worker 调试。正式 worker 常驻进程已经有 CLI 入口，后续还需要把真实 stage handler 拆进去。

### 7. 真实 stage handler 和 review gate

新增：

```text
application/services/pipeline_stage_handlers.py
```

当前 handler 拆分为：

- `download`
- `transcribe`
- `audit`
- `manual_review`
- `translate`
- `translation_review`
- `render`

行为：

- `download` 使用 producer backend 下载视频到 task workspace。
- `transcribe` 调用 producer transcribe，并把 transcript 写回 Phase 4 `AutomationService`，自动通过 transcript review。
- `audit` 当前先以 Phase 6 async taste audit passed 的方式记录 taste audit，并自动推进到 manual review。
- `manual_review` 是暂停 gate：如果 manual review 仍 pending，worker 不 enqueue 下一阶段，并在 execution payload 中写 `pause_reason=manual_review_pending`。
- `translate` 调用 producer 生成 bilingual SRT，并把翻译写回 Phase 4 subtitle / translation workflow。
- `translation_review` 是暂停 gate：如果 translation review 仍 pending，worker 不 enqueue render。
- `render` 调用 producer burn/mux 视频，上传 final video 和 subtitle artifact，并写 artifact metadata。

这意味着 Phase 6 已经有真实业务 handler 框架，且人工审核点不是隐藏在 worker 内部自动跳过，而是会明确暂停，等待前端审核动作恢复。

### 8. DB retry scheduler

本轮补齐了正式 delayed delivery 机制中的 DB scheduler 方案：

```text
application/services/retry_scheduler.py
```

worker 处理失败但未达到最大次数时，只把当前 execution 写成 `retry_scheduled`，并设置 `next_retry_at`，不会立刻把 retry message 写入 outbox。scheduler 扫描到期记录后才创建下一次 attempt 的 outbox message，并清空旧 execution 的 `next_retry_at`，避免 scheduler 重复投递。

这让重试延迟由数据库可观测字段控制，不依赖 RabbitMQ delayed exchange 插件，也不需要 TTL retry queue。

### 9. Phase 7 observability 接入

新增：

```text
application/services/phase7_observability.py
infrastructure/messaging/rabbitmq_observability.py
GET /internal/phase7/observability
```

当前 snapshot 包含：

- `queue_depth`：按 Phase 6 topology 读取 RabbitMQ queue message count。
- `dlq_count`：读取 `pipeline.dlq` 当前深度。
- `stage_latency_seconds`：从 `pipeline_stage_executions.locked_at/completed_at` 计算 stage count、avg、p95。
- `stage_status_counts`：按 stage/status 聚合 DB execution 状态。

## 四、为什么代码这样写

### 1. outbox 是唯一发布路径

Phase 6 没有让 API 直接调用 RabbitMQ。API 只创建 job 和 outbox event，是否真的发布由 outbox dispatcher 决定。

这样可以避免典型双写问题：

- job 写库成功但 RabbitMQ publish 失败。
- RabbitMQ publish 成功但 job 写库失败。

后续只要 dispatcher 可重试，就能从 pending outbox 中恢复发布。

### 2. 幂等记录按 stage dedupe key 建模

每条 stage message 都有稳定 dedupe key：

```text
pipeline:<job_id>:<stage>:attempt:<attempt>
```

worker 不依赖 RabbitMQ 的“只投一次”。RabbitMQ 至少一次投递时，重复消息会先查 `pipeline_stage_executions`：

- completed：直接 ack duplicate。
- retry_scheduled：ack 当前旧消息，不重复创建 retry。
- dlq：ack 当前旧消息，不重复创建 DLQ。

这就是业务层 exactly-once effect 的基础。

### 3. DLQ 仍然通过 outbox 表示

进入 DLQ 不是隐藏 side effect，而是显式写出 `pipeline.dlq` outbox event。这样运维和 replay 工具后续可以只围绕 outbox 和 execution 表工作。

### 4. review context 被放进消息

Phase 4 已经把 manual review、translation review、final approval 建成显式 workflow。Phase 6 的 stage message 保留 `ReviewContext`，是为了后续 worker 到达 review gate 时能携带 candidate/review/version 信息，不需要临时查 UI 状态或依赖前端传参。

## 五、测试结果

新增测试：

```text
test/test_phase6_async_pipeline.py
```

覆盖：

- queue topology 是否符合 roadmap。
- `PipelineStageMessage` 是否能 round-trip retry/review context。
- command service 是否只写 outbox。
- worker replay 是否不会重复执行 handler 或重复创建下一阶段消息。
- worker 失败后是否指数退避 retry，达到上限后是否进入 DLQ。
- `/create_task` 在 Phase6 async 模式下是否只写 `pipeline.command` outbox。

已跑通过：

```text
.venv/bin/python -m unittest discover -s test -p 'test_phase6_async_pipeline.py'
.venv/bin/python -m unittest discover -s test -p 'test_phase0_config_validation.py'
.venv/bin/python -m unittest discover -s test -p 'test_phase1_layered_architecture.py'
.venv/bin/python -m unittest discover -s test -p 'test_phase2_postgres_foundation.py'
.venv/bin/python -m unittest discover -s test -p 'test_phase3_catalog.py'
.venv/bin/python -m unittest discover -s test -p 'test_phase4_workflow.py'
.venv/bin/python -m unittest discover -s test -p 'test_phase5_cos_storage.py'
.venv/bin/python -m py_compile domain/message_contracts.py domain/queue_topology.py domain/entities.py domain/enums.py domain/repositories.py application/services/async_pipeline.py application/services/outbox_dispatcher.py api/config.py api/service.py infrastructure/messaging/rabbitmq_publisher.py infrastructure/persistence/sqlalchemy_models.py infrastructure/persistence/sqlalchemy_repositories.py
```

本机 RabbitMQ smoke 已通过：

```text
brew services start rabbitmq
.venv/bin/python workers/phase6_worker.py --declare-only
```

输出：

```text
{'exchange': 'pipeline', 'queues': 9}
```

真实 round-trip smoke 已通过：

- 清空 Phase 6 队列。
- 创建临时 SQLite DB。
- 创建 job。
- 写入 `pipeline.command` outbox。
- 通过 `OutboxDispatcher + RabbitMQPublisher` 发布到 RabbitMQ。
- `RabbitMQWorkerConsumer` 消费 `pipeline.command`。
- `PipelineStageWorker` 完成 `download` execution。
- worker 触发 outbox dispatcher 发布下一阶段消息。
- 验证 `pipeline.stage.transcribe` 队列中出现 1 条消息。

结果摘要：

```text
dispatch_before: published=1 failed=0
consume_result: processed=1
job_status: processing
current_stage: download
executions: [('download', 'completed')]
pending_after: 0
transcribe_queue_messages: 1
```

新增后端 review-gate 测试已通过：

- candidate render 写入 async command
- worker 依次处理 download / transcribe / audit / manual_review
- manual_review stage 暂停，不 enqueue translate
- `/v1/pipeline` 返回 `async_execution.pause_reason=manual_review_pending`
- 调用 review approve 后恢复并写出 `translate` outbox message
- resume message 保留 transcribe 阶段产生的 `segments` payload

前端联调已通过：

- `npm run typecheck`
- `npm test -- --run src/components/features/pipeline/pipeline-dashboard-client.test.tsx src/lib/schemas/domain-schemas.test.ts src/lib/adapters/index.test.ts`
- 浏览器打开 `http://localhost:3000/pipeline`
- 后端 unavailable 消失
- pending candidate 显示 `Worker: Stage done`
- 展开详情后显示 `Worker Execution`
- 详情文案显示 `manual_review · attempt 1/3 · manual review pending`

测试中有两个预期日志噪声：

- Phase 1 cleanup failure 用例会故意打印 cleanup 异常。
- Phase 6 DLQ 用例会故意打印 stage handler failure，用来验证 retry/DLQ 分支。

## 六、当前限制和后续建议

本轮完成的是 Phase 6 的可靠异步基础、RabbitMQ consumer loop、真实 stage handler 框架、DB retry scheduler、worker 多实例运行方式、Phase 7 observability snapshot 和前端 pipeline worker 状态联调，但还不是最终生产 worker 集群：

- `audit` handler 当前只完成 workflow 接入和自动通过语义，还没有真正调用 AI auditor/RAG 打分。
- 真实 RabbitMQ + PostgreSQL + worker 集成测试已经补齐为 opt-in 测试，默认不会在无外部服务的本地/CI 环境运行。
- retry delayed delivery 采用 DB scheduler 方案，后续可按生产运维偏好替换为 RabbitMQ delayed exchange 或 TTL retry queue。
- worker 已支持多进程实例和 launchd runbook；优雅关闭、长期运行健康检查和 dashboard 仍建议在 Phase 7 继续强化。

建议 Phase 6 下一步继续做：

1. 把 AI auditor/RAG 接入 `audit` handler，替换当前自动通过占位逻辑。
2. 在 CI/CD 中提供可选 RabbitMQ + PostgreSQL 服务后启用 opt-in 集成测试。
3. 为 `/internal/phase7/observability` 增加 Prometheus/OpenTelemetry exporter。
4. 为 worker 增加健康检查、优雅关闭和 per-stage dashboard。

## 七、最终结论

Phase 6 已开始并完成第一批核心交付：

- queue topology
- versioned stage message contract
- transactional outbox command path
- stage execution idempotency table
- worker ack / duplicate ack / retry / DLQ 语义
- RabbitMQ publisher adapter
- RabbitMQ topology declaration
- RabbitMQ worker consumer loop
- worker CLI
- worker CLI 多实例运行和 DB retry scheduler
- 本机 RabbitMQ round-trip smoke
- RabbitMQ + PostgreSQL + worker opt-in 集成测试
- download/transcribe/audit/manual_review/translate/translation_review/render stage handler 框架
- manual review / translation review 暂停 gate
- 人工审核 approve 后恢复 Phase 6 worker
- pipeline BFF `async_execution` contract
- 前端 Pipeline worker badge/detail 联调
- API async mode flag
- Phase6 单元测试和 Phase0-5 回归
- Phase7 queue depth / stage latency / DLQ count observability snapshot

系统现在已经具备接入 RabbitMQ worker 的可靠基础，可以继续推进真实 consumer loop 和 stage handler 拆分。
