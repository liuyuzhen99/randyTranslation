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
- Phase 2 foundation 测试

这意味着后面的工作不用再从“抽象不清楚”的状态开始，可以直接往 PostgreSQL 和 migration 管理推进。

---

## 三、下一批推荐实施顺序

### 1. 建立数据库配置入口

新增明确的数据库配置，例如：

- `DATABASE_URL`
- Phase 2 使用的 SQLAlchemy engine/session factory 注入入口

目标：

- 不再只靠临时 repository 构造
- 让 app / service / repository 都能通过统一入口接数据库

### 2. 初始化 Alembic

需要补上：

- `alembic.ini`
- `alembic/env.py`
- 初始 migration

要求：

- upgrade 可执行
- downgrade 可执行
- 测试中至少验证一次迁移来回

### 3. 把 SQLAlchemy schema 对齐到 PostgreSQL 语义

当前模型已经有基础约束，但下一步要更明确补齐：

- 更清晰的 enum/check constraint 命名
- PostgreSQL 下的索引策略
- 外键删除/更新策略
- created_at / updated_at 等时间字段策略

### 4. 引入 `job_events` 的写入路径

当前已经有 `JobEvent` 模型和 repository，但还没真正接进 application service。

下一步建议：

- Job 创建时记事件
- Job 状态跳转时记事件
- retry 时记事件

这样后面才能有可靠的任务审计链路。

### 5. 引入 outbox 的最小落地

当前已经有 outbox 模型和 repository，下一步建议先做最小闭环：

- 允许 application service 写入 outbox
- 保证 dedupe key/correlation id 真实使用
- 先不发消息，只做事务内持久化

这一步会是后续接 RabbitMQ 最关键的桥梁。

### 6. 设计 shadow write 切入点

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

- Alembic 初始化并可 upgrade/downgrade
- SQLAlchemy repository 接入真实数据库配置
- Job / JobEvent / Outbox 至少形成一个真实事务写入闭环
- CI 覆盖 Phase 2 foundation tests

---

## 六、总结

Phase 2 现在已经不再是纯设计阶段，而是已经进入“基础模型和规则开始落地”的状态。

后续继续推进时，最推荐的顺序是：

1. Alembic
2. 数据库配置与 session 注入
3. JobEvent 持久化接入 application service
4. Outbox 最小事务闭环
5. Shadow write 设计与试点

这样推进，风险最低，也最符合这个项目对一致性和可演进性的要求。
