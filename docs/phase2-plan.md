# Phase 2 实施计划

## 一、这一阶段的目标

Phase 2 的目标是把当前项目从 Phase 1 的“分层架构 + 临时持久化适配层”，推进到：

- PostgreSQL 成为核心 source of truth
- SQLAlchemy 成为主 ORM / DAO 基础
- Job 生命周期和阶段状态具备更严格的一致性约束
- 为后续 RabbitMQ、Outbox、重试、幂等建立可靠数据库基础

这一阶段不能只做“能存数据”，而要优先保证：

- 高并发下状态一致
- 失败后可重试但不乱跳状态
- 事件可以审计
- 数据结构能支撑后续异步和 outbox 模式

---

## 二、当前已经完成的 Phase 2 起步内容

目前已经落下第一批基础：

- Job 生命周期状态机基础规则
- `StageType` / `StageStatus` / `OutboxStatus`
- SQLAlchemy 版核心关系模型
- SQLAlchemy repository 基础实现
- 数据库配置入口与 Session Factory 注入路径
- Alembic 初始化与首个 migration
- Job / JobEvent / Outbox 的最小 shadow write 路径
- shadow write reconcile/report 原型
- outbox dispatcher 原型
- 运行时内部入口：
  - `GET /internal/phase2/reconcile`
  - `POST /internal/phase2/outbox/dispatch`
- `JOB_REPOSITORY_BACKEND=sqlalchemy` 可作为主 job repository 使用
- Phase 2 foundation 测试

这意味着后面的工作不用再从“抽象不清楚”的状态开始，可以直接往 PostgreSQL 和 migration 管理推进。

---

## 三、下一批推荐实施顺序

### 1. 把 SQLAlchemy schema 对齐到 PostgreSQL 语义

当前模型已经有基础约束，但下一步要更明确补齐：

- 更清晰的 enum/check constraint 命名
- PostgreSQL 下的索引策略
- 外键删除/更新策略
- created_at / updated_at 等时间字段策略

### 2. 强化 `job_events` / outbox / shadow write

当前已经有最小闭环、基础 reconcile 和 dispatcher 原型，但还需要继续增强：

- 明确失败重试和补偿策略
- 逐步扩大 shadow write 范围
- 为后续 dispatcher 接 RabbitMQ 留出稳定接口

### 3. 设计 shadow write 的 reconciliation 机制

在真正把 SQLite 全切掉之前，需要先决定：

- 哪些数据先 shadow write 到 SQLAlchemy/PostgreSQL
- 哪些数据暂时仍以旧路径为准
- 如何做 reconcile

建议第一批 shadow write 从：

- jobs
- job_events
- outbox

开始，而不是一上来全量替换所有实体。

---

## 四、这一阶段需要始终坚持的原则

### 1. 先保证状态规则，再谈吞吐量

如果 Job 状态和事件链路不可靠，后面再快也只是更快地产生脏数据。

### 2. 先让 transaction boundary 清晰

数据库层必须明确：

- 一次状态迁移包含哪些写操作
- 哪些写必须在同一事务里
- 哪些失败要整体回滚

### 3. 保持对 Phase 1 API 的兼容

在 Phase 2 里，数据库底层可以变，但旧 API 行为不要随意破坏。

### 4. 优先给后续 Phase 3 铺路

Phase 2 的数据库设计要天然支撑：

- outbox
- retry
- idempotency
- DLQ/replay

---

## 五、当前定义的 Phase 2 第一批完成标准

这一轮已经完成的第一批标准是：

- 有明确的生命周期规则
- 有 SQLAlchemy 关系模型
- 有 repository 基础实现
- 有能跑通的测试

下一批完成标准建议定义为：

- Phase 2 在真实 PostgreSQL 实例上跑通
- Alembic migration 在 clean DB 上完成 upgrade/downgrade
- shadow write reconcile 可以输出可消费报告
- reconcile 报告可以通过运行时入口直接生成，并支持落盘
- outbox dispatcher 可以接入真实 publisher
- Job / JobEvent / Outbox 的事务边界更清晰
- CI 继续覆盖并扩展 Phase 2 tests

---

## 六、总结

Phase 2 现在已经不再是纯设计阶段，而是已经进入“基础模型和规则开始落地”的状态。

后续继续推进时，最推荐的顺序是：

1. PostgreSQL 实例接入与配置清理
2. Alembic 在真实 DB 上验证
3. reconcile 报告持久化或定时任务化
4. outbox dispatcher 接真实 publisher
5. Phase 2 到 Phase 3 的消息发布桥接

这样推进，风险最低，也最符合这个项目对一致性和可演进性的要求。
