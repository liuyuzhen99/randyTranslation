# Phase 7 Review Report

## 结论

Phase 7 已经完成 observability、operational readiness、legacy compatibility 和 staging drill 的主线目标。系统现在具备 `/healthz`、`/readyz`、Prometheus 文本指标、队列/阶段执行观测快照、API/worker tracing hook、结构化请求日志、遗留接口 deprecation/sunset headers，以及可重复执行的 smoke/backend drill。

本阶段不只是把健康检查接口补出来，而是把 Phase 6 异步流水线的生产运行面补齐到可演练状态：真实 PostgreSQL、RabbitMQ、Tencent COS staging drill 已通过；RabbitMQ backlog、DLQ、replay 已实战验证；COS outage 场景能让 readiness 降级；前端四屏在正常、后端中断、恢复三种状态下完成联测。

唯一未执行的 live 依赖是 Qdrant，因为本机尚未安装/配置 Qdrant。当前 `/readyz` 对未配置的 Qdrant 明确报告 `skipped`，不阻塞 Phase 7 readiness。

## 评审范围

- API liveness/readiness contract。
- PostgreSQL、RabbitMQ、OSS、Qdrant dependency health checks。
- Phase 6 queue depth、DLQ、stage latency 和 stage status observability。
- Prometheus text metrics exporter。
- API request correlation ID、structured logs 和 tracing hooks。
- Worker stage tracing。
- Legacy `/create_task`、`/check_status/{task_id}`、`/list_tasks` compatibility headers。
- Ops runbook、legacy mapping、Grafana dashboard starter。
- Real staging drills: PostgreSQL + RabbitMQ + COS, backlog/DLQ/replay, COS outage。
- Frontend four-screen joint testing。

## 核心实现

### Health 和 Readiness

[application/services/phase7_health.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/application/services/phase7_health.py) 新增 `Phase7HealthService`，并在 [api/service.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/api/service.py) 暴露：

- `GET /healthz`
- `GET /readyz`

`/healthz` 只表达 API process 存活。`/readyz` 检查 PostgreSQL、RabbitMQ、OSS，并把 Qdrant 作为可选依赖：未配置时返回 `skipped`，配置后探测 `/readyz`。OSS readiness 对 local backend 检查目录可写，对 Tencent COS 会调用 bucket head/existence check，避免只看配置就误报健康。

### Metrics 和 Observability

[application/services/phase7_observability.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/application/services/phase7_observability.py) 汇总运行面快照：

- RabbitMQ queue depth。
- `pipeline.dlq` depth。
- pipeline stage latency count/avg/p95。
- stage/status 聚合。
- retry count。
- discovery freshness 和 review aging。

[application/services/phase7_metrics.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/application/services/phase7_metrics.py) 将快照渲染成 Prometheus text format，并通过 `GET /internal/phase7/metrics` 暴露。原始 JSON 快照通过 `GET /internal/phase7/observability` 暴露。

RabbitMQ queue probe 位于 [infrastructure/messaging/rabbitmq_observability.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/infrastructure/messaging/rabbitmq_observability.py)，按 Phase 6 topology 收集队列深度。

### Correlation、Tracing 和日志

[api/service.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/api/service.py) 新增请求 middleware：

- 接收或生成 `X-Correlation-Id`。
- 同步回写 `X-Request-Id` 和 `X-Correlation-Id`。
- 记录 `event=request_completed correlation_id=... method=... path=... status_code=...` 结构化日志。
- 在 `api.request` tracing span 中包裹请求。

[application/services/phase7_tracing.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/application/services/phase7_tracing.py) 提供轻量 tracing wrapper。worker stage 处理路径在 [application/services/async_pipeline.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/application/services/async_pipeline.py) 中接入 `pipeline.stage.handle` span，为后续接入 OpenTelemetry exporter 留出稳定边界。

### Legacy Compatibility

Phase 7 为旧接口增加 deprecation/sunset headers：

- `/create_task`
- `/check_status/{task_id}`
- `/list_tasks`

兼容映射文档位于 [docs/phase7-legacy-compatibility.md](/Users/randy/Documents/code/randyTranslation/randyTranslation/docs/phase7-legacy-compatibility.md)。测试覆盖了成功和错误响应中的 deprecation header，避免迁移期客户端在异常路径丢失提示。

### Operational Runbooks 和 Drills

运行手册位于 [docs/phase7-ops-runbook.md](/Users/randy/Documents/code/randyTranslation/randyTranslation/docs/phase7-ops-runbook.md)，覆盖：

- smoke drill。
- queue backlog。
- DLQ/replay。
- DB contention。
- OSS outage。
- rollback rehearsal。

脚本：

- [scripts/phase7_smoke_drill.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/scripts/phase7_smoke_drill.py)：检查 health、ready、observability、metrics。
- [scripts/phase7_backend_drill.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/scripts/phase7_backend_drill.py)：执行真实 PostgreSQL/RabbitMQ/COS drill，并覆盖 backlog、DLQ、replay 和 cleanup。

Grafana starter dashboard 位于 [docs/phase7-grafana-dashboard.json](/Users/randy/Documents/code/randyTranslation/randyTranslation/docs/phase7-grafana-dashboard.json)。

### Frontend Joint Testing

前端四屏联测覆盖：

- `/artists`
- `/queue`
- `/pipeline`
- `/library`

联测发现 footer 之前硬编码 `Backend: Operational`，在后端中断时会误导用户。已在 [status-footer.tsx](/Users/randy/Documents/code/randyTranslation/vibeFrontTranslation/auditflow-app/src/components/layout/status-footer.tsx) 改为读取后端 `/readyz`：

- 200 -> `operational`
- 非 200 -> `degraded`
- fetch failure -> `offline`

四屏均在正常、后端 outage、恢复后重新加载三种状态下通过。

## 验证结果

已完成的后端验证：

- `PYTHONPATH=. .venv/bin/python test/test_phase6_async_pipeline.py`：15 passed。
- `PYTHONPATH=. .venv/bin/python -m py_compile api/service.py application/services/phase7_health.py application/services/phase7_metrics.py application/services/phase7_observability.py application/services/phase7_tracing.py scripts/phase7_smoke_drill.py scripts/phase7_backend_drill.py`：通过。
- `scripts/phase7_smoke_drill.py --require-ready`：真实 API staging smoke 通过，DB/RabbitMQ/OSS 为 `ok`，Qdrant 为 `skipped`。
- `scripts/phase7_backend_drill.py`：真实 PostgreSQL + RabbitMQ + Tencent COS 通过。
- COS outage drill：使用不存在的 COS bucket 验证 `/readyz` 返回 503，OSS status 为 `failed`。

已完成的前端验证：

- `npm run typecheck`：通过。
- `npm run test`：38 files / 168 tests passed。
- Browser joint testing：四屏正常、后端中断、恢复状态均通过。

## 风险与限制

1. Qdrant live readiness 尚未执行  
   本机没有安装/配置 Qdrant。Phase 7 当前正确行为是 `skipped`，Phase 8 开始后需要引入 Qdrant adapter、migration 和 live readiness drill。

2. Metrics exporter 是 starter，不是完整可观测平台  
   Prometheus text exporter 和 Grafana starter dashboard 已具备，但生产还需要 scrape job、alert rules、retention 和 dashboard ownership。

3. Tracing hook 已有边界，但还没有 exporter wiring  
   当前 tracing wrapper 保持轻量兼容。后续如果要进 Jaeger/Tempo/OpenTelemetry Collector，需要补 exporter 配置和采样策略。

4. Drill 依赖真实环境配置  
   PostgreSQL、RabbitMQ、COS drill 已在本机真实依赖上通过；CI 默认不应强制跑这些测试，除非提供外部服务和安全的 credentials。

## 后续建议

- Phase 8 先实现 `VectorRepository` 抽象和 Qdrant adapter，再做 backfill/parity，不要直接把业务检索逻辑绑死在 Qdrant SDK 上。
- 为 Qdrant readiness 增加 live drill，一旦 `QDRANT_URL` 配置，必须纳入 `/readyz` 验证。
- 将 Phase 7 metrics 接入真实 Prometheus/Grafana 环境后，再补 alert rule 和 dashboard 截图验证。
- 把 staging drill 纳入 release checklist，但保持 credentials 和外部依赖 opt-in。

## 总体评价

Phase 7 达到了 roadmap 对“operability before cutover”的要求。系统现在能回答生产化最关键的几个问题：服务是否存活、依赖是否可用、队列是否积压、DLQ 是否增长、阶段执行是否变慢、旧接口迁移期是否有明确提示、前端在后端异常时是否诚实降级。

这为 Phase 8 的 Qdrant 迁移提供了必要前提：接下来引入新的向量依赖时，可以通过现有 readiness、metrics、drill 和 report 机制验证迁移质量，而不是把 Qdrant 作为黑盒直接切入主链路。
