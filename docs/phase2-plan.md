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

## 二、当前已经完成的 Phase 2 内容

目前已经完成的内容包括：

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
- `job.lifecycle` outbox payload 已收敛为稳定消息契约，便于后续接 RabbitMQ
- reconcile variance threshold 已配置化，可区分“完全一致”和“在允许偏差内”
- outbox dispatcher 默认不会假发布；只有注入真实 publisher 时才会真正 drain pending outbox
- Alembic 现在会优先读取项目 `.env` 中的 `DATABASE_URL`
- 运行时配置现在会自动读取项目 `.env`
- 已修复真实 PostgreSQL 下 `job_events` 状态约束重名问题
- 已在真实 PostgreSQL 上验证：
  - `alembic upgrade head` 成功
  - `jobs / job_events / outbox` 实际写入成功
  - `/internal/phase2/reconcile` 返回一致报告
- Phase 2 foundation 测试

这意味着 Phase 2 的“纯代码部分 + 真实 PostgreSQL 验证”已经基本完成。

---

## 三、下一批推荐实施顺序

### 1. 提交并合并 Phase 2 PR

当前已经具备提 PR 的条件：

- 模型、repository、shadow write、reconcile、outbox dispatcher 原型都已完成
- 真实 PostgreSQL migration 已验证通过
- 真实 PostgreSQL 写入和 reconcile 已验证通过

### 2. 开始 Phase 3 预备设计

下一步最自然的是进入 RabbitMQ 接入前的准备：

- 明确 queue topology
- 明确 publisher 接口与消息发布边界
- 明确 outbox 到真实 broker 的调度路径

### 3. 保持 Phase 2 范围边界

Phase 2 之后不建议继续在没有真实 RabbitMQ 的情况下扩展大量消息系统实现。

后续只应继续做：

- 文档收尾
- PR 合并
- 为 Phase 3 设计消息边界

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

当前可以认为已经完成的标准是：

- 有明确的生命周期规则
- 有 SQLAlchemy 关系模型
- 有 repository 基础实现
- 有 shadow write / reconcile / outbox dispatcher 原型
- Alembic migration 在真实 PostgreSQL 上已跑通
- 真实 PostgreSQL 下 `jobs / job_events / outbox` 写入已验证
- reconcile 报告在真实 PostgreSQL 下已验证
- 测试已覆盖并通过

剩余未完成的部分已经明显属于下一阶段或外部系统接入：

- 真实 RabbitMQ publisher
- outbox 到 broker 的真实发布链路
- Phase 3 的异步 worker / ack-nack / retry / DLQ

---

## 六、总结

Phase 2 现在已经不再只是“基础模型和规则开始落地”的状态，而是已经完成了：

- 代码实现
- 本地测试
- 真实 PostgreSQL migration
- 真实 PostgreSQL 写入与 reconcile 验证

后续继续推进时，最推荐的顺序是：

1. 提交并合并 Phase 2 PR
2. 进入 Phase 3 设计
3. 准备真实 RabbitMQ 接入

这样推进，风险最低，也最符合这个项目对一致性和可演进性的要求。
