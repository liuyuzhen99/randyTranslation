# Phase 2 当前阶段总结

## 一、这一轮 Phase 2 做的事情

这一轮不是直接把 PostgreSQL、Alembic、事务一致性、双写和完整 DAO 一次性全部做完，而是先落下 Phase 2 最关键、最不适合以后返工的“基础层”：

- 明确 Job 生命周期状态机
- 明确阶段状态和 Outbox 状态枚举
- 定义 SQLAlchemy 版核心关系模型
- 建立 SQLAlchemy repository 基础实现
- 建立数据库配置入口与 SQLAlchemy session factory 注入路径
- 初始化 Alembic 并补上首个 migration
- 把 `jobs / job_events / outbox` 的最小 shadow write 路径接进应用层
- 增加对应测试，确保这些基础约束不是“只停留在设计”

这一步的定位是：

**先把 PostgreSQL Foundation 的领域约束和 ORM 模型打牢，为后续 Alembic 和真实 PostgreSQL 接入做准备。**

---

## 二、改造前的代码和状态

在 Phase 1 完成后，项目已经具备了分层架构，也已经有：

- `domain/` 中的实体与 repository 抽象
- `application/` 中的任务服务和 pipeline orchestrator
- `infrastructure/` 中的 in-memory / SQLite 临时适配层

但站在 Phase 2 的目标看，仍然存在几个明显缺口：

### 1. Job 生命周期约束还比较弱

虽然已有 `JobStatus`，但还没有真正的状态机规则，例如：

- 哪些状态跳转允许
- 哪些状态跳转非法
- 失败后如何重试
- 完成后是否还能再跳转

这意味着：

- 生命周期约束目前主要靠调用方自觉
- 后续接数据库时，很容易出现脏状态或非法状态迁移

### 2. 还没有 SQLAlchemy 版“核心关系模型”

当前主要还是 Phase 1 的 SQLite 适配层。

这对于架构过渡是可以的，但还不等于：

- Phase 2 的 PostgreSQL 关系模型
- 具有业务约束的 ORM schema
- 为 Alembic 和真实数据库迁移准备好的元数据

### 3. Job events / outbox 的数据基础还不完整

roadmap 里明确提到：

- `jobs`
- `job_events`
- `outbox`

这些是后续可靠异步、双写、重试、审计的基础。

如果 Phase 2 不先把这些模型和约束立起来，后续 RabbitMQ / outbox / replay 等能力就会缺基础。

---

## 三、为什么要先做这一批基础

这一步最大的原因是：

**状态机和关系模型一旦随便做，后面几乎一定返工。**

相反，如果先把这些抽象打牢，后面再接：

- PostgreSQL
- Alembic
- Outbox
- RabbitMQ
- 重试与幂等

就会顺很多。

这批代码的作用主要是：

### 1. 把“业务状态规则”从隐式变成显式

例如：

- `pending -> completed` 这种跳转是否允许
- `failed -> processing` 是否算 retry
- retry 次数如何累计

这些都应该成为明确代码规则，而不是散落在 service 或 future worker 里。

### 2. 把“数据库结构”从临时表思维变成可演进的关系模型

我们需要的不是“先有表再说”，而是：

- 有主键
- 有业务唯一约束
- 有外键
- 有状态约束
- 有索引

这样后续才有机会真正支持高并发和数据一致性。

### 3. 让后续 Phase 2 的数据库工作有可靠起点

这一轮不是最终完成 Phase 2，而是把最关键的第一批基础真正写成代码并测过。

---

## 四、当前代码变成了什么样

### 1. `domain/enums.py`

新增了：

- `StageType`
- `StageStatus`
- `OutboxStatus`

这让：

- 任务整体状态
- 流水线阶段状态
- outbox 投递状态

都不再只是散落的字符串，而是明确的领域枚举。

### 2. `domain/entities.py`

增强了领域实体，新增或补充了：

- `Job.current_stage`
- `Job.retry_count`
- `Job.created_at`
- `Job.updated_at`
- `JobEvent`
- `OutboxEvent` 的聚合与去重相关字段

这些字段不是为了“让实体变复杂”，而是为了让后续：

- 生命周期跟踪
- 审计
- 重试
- 事件追踪
- 幂等控制

有足够的数据基础。

### 3. `domain/job_lifecycle.py`

新增了专门的生命周期规则模块。

它负责：

- 定义允许的 Job 状态迁移
- 校验状态迁移是否合法
- 处理 retry 场景下 `failed -> processing`
- 统一生成状态迁移后的新 Job

这样做的价值非常高，因为后续不管是：

- API 层
- application service
- worker
- repository

都应该共享同一套生命周期规则，而不是各自写一套。

### 4. `domain/exceptions.py`

新增了：

- `InvalidJobTransitionError`

这让非法状态迁移有了明确的错误类型，而不是只能抛通用异常。

### 5. `infrastructure/persistence/sqlalchemy_models.py`

新增了 SQLAlchemy 版核心关系模型，包含：

- `artists`
- `videos`
- `subtitles`
- `jobs`
- `job_events`
- `outbox`

并加入了：

- 主键
- 外键
- 唯一约束
- check constraint
- status enum constraint
- 查询索引

例如：

- `subtitles(video_id, line_index)` 唯一
- `outbox.dedupe_key` 唯一
- `jobs.status` 建索引
- `videos.spotify_id` 和 `videos.processed_status` 建索引
- `job_events(job_id, created_at)` 建索引

这已经比较接近真正 Phase 2 关系建模需要的样子。

### 6. `infrastructure/persistence/sqlalchemy_repositories.py`

新增了基于 SQLAlchemy 的 repository 基础实现，包括：

- `SQLAlchemySessionFactory`
- `SQLAlchemyArtistRepository`
- `SQLAlchemyVideoRepository`
- `SQLAlchemySubtitleRepository`
- `SQLAlchemyJobRepository`
- `SQLAlchemyJobEventRepository`
- `SQLAlchemyOutboxRepository`

这里最关键的是：

`SQLAlchemyJobRepository.update()` 已经开始基于生命周期规则校验状态迁移。

也就是说：

数据库 repository 不再只是“盲写”，而是开始承担 Phase 2 所要求的状态一致性责任。

### 7. `application/services/phase2_shadow_write_service.py`

新增了最小的 Phase 2 shadow write 服务。

它的职责是：

- 在不改变 Phase 1 主存储路径的前提下
- 把 job 的创建和状态变化同步写入 SQLAlchemy 模型
- 同时生成 `job_events` 和 `outbox` 记录
- 并把这几类写操作放进同一个 session/transaction 中完成

这一步很关键，因为它让 Phase 2 不再只是“表结构设计好了”，而是已经开始出现：

- 审计链路
- outbox 原型
- 双写切入点

### 8. `api/config.py`

新增了 Phase 2 相关运行时配置入口，例如：

- `PHASE2_SHADOW_WRITE_ENABLED`
- `PHASE2_AUTO_CREATE_SCHEMA`
- `DATABASE_URL`

并且可以根据配置创建：

- `SQLAlchemySessionFactory`
- `Phase2ShadowWriteService`

这让 Phase 2 数据层不再只是测试里能用，而是运行时也有清晰注入路径。

### 9. Alembic 基础设施

当前已经新增：

- `alembic.ini`
- `alembic/env.py`
- `alembic/script.py.mako`
- 初始 migration 文件

并且测试已经验证：

- `upgrade head` 可执行
- `downgrade base` 可执行

这说明 Phase 2 已经不只是有 ORM metadata，而是有真正可管理的迁移入口。

---

## 五、为什么代码要这样写

### 1. 先把状态机单独抽出来

如果状态机规则直接写在：

- route handler
- orchestrator
- repository

以后几乎一定会重复、分叉、失控。

所以把它单独放进 `domain/job_lifecycle.py`，是因为：

- 它本质上是领域规则
- 不属于基础设施细节
- 应该被 application 和 persistence 共用

### 2. ORM 模型先做成“关系约束优先”

这一轮写 SQLAlchemy 模型时，我不是按“先能存再说”做的，而是优先补：

- 外键
- 唯一约束
- check constraint
- status enum
- index

这是因为 Phase 2 的目标不是“换个 ORM”，而是：

**把 PostgreSQL 变成可信 source of truth。**

如果关系和约束一开始不立住，后面越做越难修。

### 3. Repository 先做核心实体，不急着一次补全所有边角

这一轮 repository 重点放在：

- artist
- video
- subtitle
- job
- job_event
- outbox

这些是 roadmap 里最核心的 Phase 2 数据结构。

这是比较合理的切法，因为它们直接决定后续：

- pipeline 状态记录
- 幂等
- outbox
- retry
- 审计

### 4. 测试优先验证“规则”，而不是只测 happy path

这一轮测试重点不是“表能创建”这么简单，而是验证：

- 非法状态迁移会失败
- retry 语义正确
- 关键表和索引存在
- outbox dedupe key 真正唯一
- repository round-trip 是否成立

这样才能说明这些代码是“真的有约束意义”，而不是只是多写了几层抽象。

---

## 六、测试做了什么

新增测试文件：

- `test/test_phase2_postgres_foundation.py`

覆盖内容包括：

- Job 生命周期非法跳转校验
- 失败后 retry 的合法迁移
- SQLAlchemy metadata 是否包含核心表和索引
- `SQLAlchemyJobRepository` 是否拒绝非法状态迁移
- artist / video / subtitle / job / job_event / outbox 的基础 round-trip
- outbox `dedupe_key` 唯一约束是否生效
- shadow write 是否会生成 `jobs / job_events / outbox` 记录
- Alembic upgrade / downgrade 是否可执行

这一轮测试重点是证明：

**Phase 2 的“规则基础”和“关系模型基础”已经开始落地。**

---

## 七、这一轮做到哪里，还没做到哪里

### 已经做到的

- 初步建立 Job 生命周期状态机
- 初步建立 SQLAlchemy 关系模型
- 初步建立 SQLAlchemy repository
- 建立数据库配置入口和 session factory 注入路径
- 把 job 创建与状态变化接入最小 shadow write
- 初始化 Alembic 并验证 migration upgrade/downgrade
- 为 job_events / outbox / 幂等约束打基础
- 用测试验证这些规则和模型

### 还没做到的

- 还没有真正切到 PostgreSQL 实例运行
- 还没有实现 shadow write / reconciliation
- 还没有把 Phase 1 的运行时切换到 SQLAlchemy/PostgreSQL
- 还没有建立完整 transaction + outbox 工作流

这些仍然属于 Phase 2 后续工作。

---

## 八、最终总结

这一轮 Phase 2 的意义，不是“已经完成 PostgreSQL 迁移”，而是：

**把最容易影响后续架构稳定性的那一层提前做好。**

具体来说，这一轮已经把：

- 生命周期规则
- 关系约束
- ORM 基础模型
- 核心 SQLAlchemy repository
- 配置化数据库入口
- Alembic migration 基础设施
- shadow write 初步接入
- 对应测试

正式建立起来了。

这会直接帮助后续继续做：

- Alembic 初始化与迁移
- PostgreSQL DAO 落地
- Job transition DB 约束
- Outbox + RabbitMQ
- Retry / idempotency / replay

换句话说，这一轮不是 Phase 2 的终点，而是一个正确、稳固、可继续扩展的起点。
