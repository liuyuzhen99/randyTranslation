**Phase 2 详细 Review 报告**

Phase 2 的目标是把项目从 Phase 1 的“分层架构 + 临时存储适配层”，推进到“以 PostgreSQL 为核心 source of truth 的事务型基础层”。我这一阶段做的事，核心可以归成 6 大块：领域模型、SQLAlchemy 持久化、Alembic 迁移、shadow write、reconcile/outbox、真实 PostgreSQL 验证。

**1. 先做了领域层收口：把状态和事件定义清楚**
这一步主要改的是：

- [domain/enums.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/domain/enums.py)
- [domain/entities.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/domain/entities.py)
- [domain/job_lifecycle.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/domain/job_lifecycle.py)
- [domain/exceptions.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/domain/message_contracts.py)

我先补了 `StageType`、`StageStatus`、`OutboxStatus`，再扩展了 `Job`、`JobEvent`、`OutboxEvent`。这么做的原因是：如果领域对象本身没有表达出“当前阶段”“重试次数”“事件记录”“待发布消息”，后面的数据库设计只能是拍脑袋拼字段，后续一定返工。

然后我把 Job 生命周期抽成独立规则模块 `job_lifecycle.py`，明确：

- 哪些状态迁移允许
- 哪些非法
- `failed -> processing` 算 retry
- retry 次数如何累计

为什么这么做：

- 生命周期规则不能散落在 service、repository、worker 里各写一份
- 领域规则必须先稳定，数据库层和应用层才有统一依据

后面我又加了 [domain/message_contracts.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/domain/message_contracts.py)，把 `job.lifecycle` 的 outbox payload 结构定成明确契约，而不是随手拼 JSON。这样做是为了给 Phase 3 接 RabbitMQ 留稳定边界，避免消息格式以后再大改。

**2. 然后做了 SQLAlchemy 模型和 repository，把 PostgreSQL Foundation 真正落成代码**
这部分主要改的是：

- [infrastructure/persistence/sqlalchemy_models.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/infrastructure/persistence/sqlalchemy_models.py)
- [infrastructure/persistence/sqlalchemy_repositories.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/infrastructure/persistence/sqlalchemy_repositories.py)
- [domain/repositories.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/domain/repositories.py)

我建了这几张核心表的 SQLAlchemy 模型：

- `artists`
- `videos`
- `subtitles`
- `jobs`
- `job_events`
- `outbox`

同时加了：

- 主键
- 外键
- 唯一约束
- 状态枚举约束
- 索引
- retry_count 非负检查

为什么这么做：

- Phase 2 的重点不是“能写数据库”，而是“数据库结构本身能保护一致性”
- 比如 `subtitles(video_id, line_index)` 唯一、`outbox.dedupe_key` 唯一、`jobs.status` 有索引，这些都是后面高并发、重试、去重的基础

然后我实现了 SQLAlchemy 版 repository：

- `SQLAlchemyJobRepository`
- `SQLAlchemyJobEventRepository`
- `SQLAlchemyOutboxRepository`
- 以及 artist/video/subtitle 的 repository

尤其关键的是，`SQLAlchemyJobRepository.update()` 不再是盲写，而是会校验状态迁移是否合法。这样做是因为 Phase 2 要求“数据库层也参与状态一致性保障”，不能完全靠应用层自觉。

**3. 接着做了 Alembic 迁移体系，而不是只停留在 ORM**
这部分主要改的是：

- [alembic.ini](/Users/randy/Documents/code/randyTranslation/randyTranslation/alembic.ini)
- [alembic/env.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/alembic/env.py)
- [alembic/versions/20260415_220500_phase2_initial_schema.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/alembic/versions/20260415_220500_phase2_initial_schema.py)
- [infrastructure/persistence/alembic_runtime_config.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/infrastructure/persistence/alembic_runtime_config.py)

我先把 Alembic 初始化起来，再补了初始 migration。后面为了真实接 PostgreSQL，我又做了两件关键修正：

- Alembic 不再死读 `alembic.ini` 的 SQLite URL，而是优先读项目 `.env` 里的 `DATABASE_URL`
- `alembic/env.py` 启动时会把项目根目录加到 `sys.path`，避免运行时找不到项目模块

为什么这么做：

- 如果 Alembic 还停留在写死 SQLite，真实 PostgreSQL 验证根本做不下去
- 迁移系统必须跟运行时配置对齐，否则你以为迁移的是 PostgreSQL，实际上跑的是别的库

后来在真实 PostgreSQL 上还暴露了一个很真实的问题：`job_events` 表里 `from_status` 和 `to_status` 复用了同名约束，PostgreSQL 会报 `DuplicateObject`。我就把 [sqlalchemy_models.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/infrastructure/persistence/sqlalchemy_models.py) 改成每个状态列生成独立约束名。为什么这样改：

- SQLite 里不一定暴露这个问题
- 但 PostgreSQL 会严格检查约束命名
- 这正是“真实库验证”的价值，不是纸上谈兵

**4. 然后我把 shadow write 接进应用层，让 Phase 2 不只是表结构**
这部分主要改的是：

- [application/services/phase2_shadow_write_service.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/application/services/phase2_shadow_write_service.py)
- [application/services/job_service.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/application/services/job_service.py)
- [application/services/pipeline_orchestrator.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/application/services/pipeline_orchestrator.py)

`Phase2ShadowWriteService` 的职责是：

- job 创建时同步写入 shadow 库
- job 更新时同步写入 shadow 库
- 同事务里同时写：
  - `jobs`
  - `job_events`
  - `outbox`

为什么这么做：

- 如果只写 `jobs`，没有 `job_events/outbox`，那后续审计、消息发布、replay 都没基础
- 如果这三类写不在同一事务边界里，一旦中间失败，就会出现“job 更新了，但事件没记”“事件记了，但 outbox 没落”的裂缝

在 `JobService.create_job()` 和 `PipelineOrchestrator._update_job()` 里，我把 shadow write 接进去，让 Phase 2 真正开始跑起来，而不是只在测试里存在。

**5. 再往前做了 reconcile/report 和 outbox dispatcher 原型**
这部分主要改的是：

- [application/services/phase2_reconcile_service.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/application/services/phase2_reconcile_service.py)
- [application/services/outbox_dispatcher.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/application/services/outbox_dispatcher.py)
- [api/service.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/api/service.py)
- [api/config.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/api/config.py)

`Phase2ReconcileService` 做的是对账：

- 主存储里有多少 jobs
- shadow 库里有多少 jobs
- 哪些 job 缺失
- 哪些字段漂移
- pending outbox 数量
- outbox payload 是否非法
- outbox payload 是否和当前 job 状态不一致

后来我又加了 `Phase2ReconcileThresholds`，把“允许偏差阈值”做成配置，而不是只写在文档里。原因是 roadmap 里提了 variance threshold，这种东西如果不进代码，就不算真正实现。

`OutboxDispatcher` 则负责：

- 拉取 pending outbox
- 调 publisher
- 成功标记 `published`
- 失败标记 `failed`

但这里我后来**主动回撤过一次**。一开始我做过一个默认 `LoggingOutboxPublisher`，会在没有真实消息系统时把 outbox 事件也标成 `published`。后来按你的要求复盘时，我判断这属于“假设外部工具已经成功运行”的实现，所以把它撤掉了。现在的规则是：

- 没有真实 publisher，就不启用 dispatcher
- `/internal/phase2/outbox/dispatch` 默认返回 `503`
- 只有显式注入 publisher 时才会真正 dispatch

为什么这样改：

- 这更真实
- 不会在没有 RabbitMQ 的情况下制造“消息已经发出”的假象
- 更符合你对 Phase 2 纯代码边界的要求

同时我在 [api/service.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/api/service.py) 里加了两个内部入口：

- `GET /internal/phase2/reconcile`
- `POST /internal/phase2/outbox/dispatch`

这样做的原因是：

- 让 Phase 2 能通过 app 本身被触发和验证
- 不需要手写脚本直接调 service 类
- 但又不破坏旧 API 兼容性

**6. 配置层和运行时装配也做了工程化处理**
这部分主要改的是：

- [api/config.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/api/config.py)
- [.env.example](/Users/randy/Documents/code/randyTranslation/randyTranslation/.env.example)

我加了这些能力：

- `JOB_REPOSITORY_BACKEND=sqlalchemy`
- `DATABASE_URL` / `POSTGRES_*`
- `PHASE2_SHADOW_WRITE_ENABLED`
- `PHASE2_RECONCILE_ENABLED`
- `PHASE2_RECONCILE_REPORT_PATH`
- reconcile thresholds
- `PHASE2_OUTBOX_DISPATCH_ENABLED`

后面又补了一个重要点：`load_runtime_settings()` 现在会自动加载项目 `.env`。这一步是在真实 PostgreSQL 验证时发现问题后加的，因为当时 app 实际仍然拿默认值，根本没走 PostgreSQL backend。

为什么这样做：

- 配置必须真正能驱动运行时，不然 `.env` 只是摆设
- 本地开发和真实验证必须靠同一套配置入口

**7. 测试是怎么补的，为什么这么补**
这部分主要改的是：

- [test/test_phase0_config_validation.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/test/test_phase0_config_validation.py)
- [test/test_phase0_api_baseline.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/test/test_phase0_api_baseline.py)
- [test/test_phase2_postgres_foundation.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/test/test_phase2_postgres_foundation.py)
- [test/test_phase2_alembic_env.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/test/test_phase2_alembic_env.py)

我补的测试，不是只测 happy path，而是围绕真正容易出问题的点：

- 生命周期非法迁移
- SQLAlchemy repository round-trip
- outbox 去重键唯一
- shadow write 是否真的写 `jobs/job_events/outbox`
- reconcile 是否能发现缺失、漂移、非法 payload
- dispatcher 成功/失败状态变化
- Alembic upgrade/downgrade
- Alembic 是否优先读取 `.env` 的 `DATABASE_URL`
- app rebuild 后 SQLAlchemy backend 持久化是否还在
- 默认无真实 publisher 时 dispatch 是否正确 `503`

为什么这样测：

- Phase 2 最怕的是“代码看起来很完整，但一致性断点都没测到”
- 这类项目的风险主要在状态迁移、双写、对账、迁移、真实库兼容

**8. 真实 PostgreSQL 验证时，我具体做了什么**
这一步是 Phase 2 最重要的收尾。

我做的顺序是：

1. 确认本机有 PostgreSQL 客户端和服务
2. 确认项目 `.env` 已经配置好 `DATABASE_URL`
3. 修 Alembic，让它真正吃 `.env`
4. 安装 `psycopg`
5. 运行真实 `alembic upgrade head`
6. 修复 PostgreSQL 下暴露的 `job_events` 约束重名问题
7. 再次运行 migration 成功
8. 查 PostgreSQL，确认表存在
9. 用真实 app 配置做 smoke test
10. 查 PostgreSQL，确认 `jobs / job_events / outbox` 实际写入
11. 跑 `/internal/phase2/reconcile`，确认报告一致

真实验证得到的结论是：

- Phase 2 不只是“本地测试过”
- 而是已经在你机器上的真实 PostgreSQL 上：
  - 迁移成功
  - 数据入库成功
  - reconcile 成功

**9. 我为什么说 Phase 2 到这里基本完成**
因为 roadmap 里属于 Phase 2 的核心内容，已经具备：

- SQLAlchemy 关系模型
- 生命周期状态机
- Alembic
- PostgreSQL DAO
- shadow write
- reconciliation
- 真实 PostgreSQL 验证

剩下没做的，已经明显属于下一阶段：

- 真实 RabbitMQ publisher
- outbox 到 broker 的真实发送链路
- worker ack/nack
- retry / DLQ

这些已经是 Phase 3 了，不应该在 Phase 2 里继续靠“纯代码想象”推进。

**10. 这一阶段最关键的几次纠偏**
如果从 review 角度看，最重要的不是“做了多少”，而是“哪些地方及时纠偏了”：

- 我撤掉了默认假发布的 `LoggingOutboxPublisher`
  原因：不能在没有真实消息系统时把事件标记为 `published`
- 我修了 Alembic 仍然死读 SQLite 的问题
  原因：否则真实 PostgreSQL 验证根本不成立
- 我修了运行时不读 `.env` 的问题
  原因：否则 app 看似支持 PostgreSQL，实际仍跑默认内存路径
- 我修了 PostgreSQL 下约束重名问题
  原因：SQLite 下过得去，不代表真实 PostgreSQL 能过

这几次调整，本质上都是为了让 Phase 2 更真实、更少自欺。

**11. 当前最终结论**
如果用一句话概括 Phase 2：

Phase 2 已经从“PostgreSQL Foundation 的代码雏形”推进到“真实 PostgreSQL 上已验证可工作的事务型基础层”。

包括：

- 代码完成
- 测试完成
- 真实迁移完成
- 真实写入完成
- 真实 reconcile 完成

如果你愿意，我下一步可以继续把这份 review 报告整理成一份正式文档，比如：

- `docs/phase2-review-report.md`
  或者我也可以直接帮你把当前 Phase 2 分支整理成 PR 描述。
