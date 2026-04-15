# Phase 1 实施总结

## 一、这一阶段的目标

Phase 1 的目标不是上线新功能，而是在**不改变现有 API 对外行为**的前提下，把项目从“功能能跑”的状态，推进到“具备明确分层、可以继续工程化扩展”的状态。

这一阶段重点是：

- 建立清晰的分层架构边界
- 让 FastAPI 运行时真正使用这些分层
- 降低并发任务下的状态污染风险
- 为后续数据库、队列、对象存储升级打地基

当前对外仍然保留以下接口：

- `POST /create_task`
- `GET /check_status/{task_id}`
- `GET /list_tasks`

---

## 二、改造前的代码和状态

在进入本次 Phase 1 收尾实现前，仓库里其实已经有一批 Phase 1 的基础文件：

- `api/`
- `application/`
- `domain/`
- `infrastructure/`

也已经存在以下核心模块：

- `application/services/job_service.py`
- `application/services/pipeline_orchestrator.py`
- `domain/entities.py`
- `domain/repositories.py`
- `domain/storage.py`
- `infrastructure/persistence/in_memory_job_repository.py`
- `infrastructure/persistence/sqlite_repositories.py`
- `infrastructure/storage/local_media_storage.py`

但那个阶段的问题是：**代码目录分层已经有了，运行时分层还不够彻底，系统稳定性也还不够扎实。**

主要表现为：

### 1. API 虽然开始接入新结构，但应用组装方式还比较粗糙

`api/service.py` 已经在调用 `JobService` 和 `PipelineOrchestrator`，但整体还是偏向“模块加载时直接创建对象”的写法。

这会导致：

- 依赖如何构建不够清晰
- 不方便切换不同的 repository backend
- 不方便做“应用重建后状态是否保留”的测试
- 后续继续演进到更完整的依赖注入结构会比较别扭

### 2. Job 状态默认只存在内存里

虽然已经有 `SQLiteJobRepository`，但运行中的 API 默认还是依赖内存 repository。

这意味着：

- 服务一重启，任务状态就没了
- 用户再查任务可能直接查不到
- 系统表现会比较像 demo，而不是稳定后端

### 3. Pipeline 编排存在并发污染风险

在 `PipelineOrchestrator` 里，原本是持有一个共享的 producer backend 实例，并且在每次任务执行时修改它的 `temp_dir`。

这个设计在单任务下可能问题不大，但在并发任务下会有明显风险：

- 任务 A 改了 `temp_dir`
- 任务 B 也改了 `temp_dir`
- 两个任务的中间文件可能串目录
- 视频、音频、字幕中间产物可能互相污染

对于长时运行、大文件处理的系统来说，这类问题非常危险。

### 4. 本地媒体目录和日志目录不够可移植

当时默认路径更接近个人机器习惯，比如依赖 `~/Downloads/...` 一类目录。

这类默认值的问题是：

- 在 CI 环境里容易出权限问题
- 在沙箱环境里容易失败
- 在服务器或容器环境里不可控
- 团队协作时路径约定不统一

### 5. 测试还没完全覆盖 Phase 1 真正的风险点

之前虽然已经有一定测试，但还不够覆盖下面这些关键点：

- 并发任务是否真的隔离 backend 状态
- cleanup 失败时任务会不会卡住
- SQLite 模式下应用重建后能不能继续查到任务
- 配置项是否支持安全地切换 backend
- Phase 0 的 config guardrails 是否和 Phase 1 共存

---

## 三、为什么必须改

这个项目不是一个简单脚本，而是一个将来要继续扩展、可能面对很多视频音频任务的后端基础设施。

所以这次 Phase 1 的改造，核心不是“代码好不好看”，而是以下几个工程目标：

### 1. 并发时不能互相污染任务上下文

视频/音频处理会产生很多临时文件。只要不同任务共享同一个可变 backend 实例，就存在：

- 临时目录串用
- 文件覆盖
- 状态错乱
- 排查困难

这必须尽早处理。

### 2. 任务状态不能完全依赖进程内存

一个稍微正式一点的后台系统，如果服务一重启，任务全丢，就很难称得上稳定。

即使 Phase 1 还没上 PostgreSQL，也至少要给出一个可切换的持久化路径。

### 3. 仓储层要有边界感，不能让对象被随便偷偷修改

如果 repository 直接把内部对象引用暴露出去，调用方就可能绕过 repository 语义直接改状态。

这会带来：

- 状态修改路径混乱
- 调试困难
- 后续迁移到数据库时语义不一致

### 4. 运行默认值必须可移植

如果路径依赖某一个开发者本机目录，系统在别的环境很容易出问题。

对于一个要继续走向工程化的项目来说，默认配置必须尽量：

- 可预测
- 可测试
- 可部署

### 5. 架构变化必须经过测试证明

这次重构不是“看起来合理就行”，而是必须用测试证明：

- 行为没变
- 风险点被控制住了
- 配置切换真实可用
- 重启场景下真的能保持状态

---

## 四、当前代码变成了什么样

经过这一轮 Phase 1 实施和收尾，现在系统已经形成了更清晰的结构。

### 1. 分层职责更明确

#### `api/`

职责：

- 提供 FastAPI 接口
- 定义请求/响应模型
- 组装应用运行时依赖

关键文件：

- `api/service.py`
- `api/config.py`

#### `application/`

职责：

- 封装业务编排逻辑
- 管理任务生命周期

关键文件：

- `application/services/job_service.py`
- `application/services/pipeline_orchestrator.py`

#### `domain/`

职责：

- 定义核心实体
- 定义枚举
- 定义 repository/storage 抽象接口

关键文件：

- `domain/entities.py`
- `domain/enums.py`
- `domain/repositories.py`
- `domain/storage.py`

#### `infrastructure/`

职责：

- 提供具体实现
- 提供临时适配器
- 提供本地存储和 SQLite 持久化能力

关键文件：

- `infrastructure/persistence/in_memory_job_repository.py`
- `infrastructure/persistence/sqlite_repositories.py`
- `infrastructure/storage/local_media_storage.py`
- `infrastructure/pipeline/legacy_producer_adapter.py`

---

## 五、具体做了哪些改动，以及为什么这样写

## 1. 把 API 运行时依赖组装改成 app factory 模式

在 `api/service.py` 中，引入了：

- `build_runtime_services()`
- `create_app()`

并将应用实例的依赖统一挂在 `app.state` 上。

### 为什么要这样做

这样做有几个明显好处：

- 运行时依赖如何创建，一眼就能看清楚
- 可以很方便地根据配置切换 repository backend
- 测试中可以重新构建 app，验证重启场景
- 为后续引入更完整的生命周期管理和依赖注入做准备

以前更像是：

- 模块导入时顺手把对象都 new 出来

现在更像是：

- 明确存在一个“应用装配层”

这对后端工程化非常重要。

## 2. 在 `api/config.py` 中增加运行时配置能力

新增了：

- `AppRuntimeSettings`
- `load_runtime_settings()`
- `create_job_repository()`

同时支持以下配置：

- `JOB_REPOSITORY_BACKEND`
- `JOB_REPOSITORY_SQLITE_PATH`

### 为什么要这样做

因为 Phase 1 不能直接跳到 PostgreSQL，但又不能一直只靠内存。

所以最合理的做法就是：

- 默认仍支持轻量的 in-memory 模式
- 需要时切到 SQLite 模式
- 且这个切换通过统一配置入口完成

这样做的价值是：

- 当前阶段就能拥有简单持久化能力
- 不会破坏旧 API 行为
- 为 Phase 2 的数据库升级留出了平滑过渡路径

## 3. `PipelineOrchestrator` 改成每个任务使用独立 producer backend

原本 orchestrator 接收的是共享 backend 实例。

现在改成接收 backend factory，并在每次任务执行时创建新的 backend。

### 为什么要这样做

因为 producer 内部存在运行态属性，例如 `temp_dir`。

如果多个任务共享一个 backend 实例，就会出现：

- A 任务刚改完 `temp_dir`
- B 任务又把它覆盖
- 最终两个任务写进错误目录

现在每个任务都拥有独立 backend，就能把任务级运行状态隔离开。

这对于处理视频、音频、字幕等多阶段流水线非常关键。

## 4. `InMemoryJobRepository` 增加深拷贝边界

现在在：

- `create()`
- `get()`
- `update()`
- `list_all()`

中都通过深拷贝保护内部状态。

### 为什么要这样做

repository 应该是状态边界，而不是一个把内部对象引用随手交出去的容器。

这样做后：

- 调用方不会意外污染 repository 内部状态
- 所有状态变化更接近“必须通过 repository 持久化”这一语义
- 行为更像未来真正数据库 repository 的模式

## 5. `SQLiteJobRepository` 做了并发友好增强

在 SQLite 连接上增加了：

- `timeout=30.0`
- `PRAGMA journal_mode=WAL`
- `PRAGMA busy_timeout = 30000`

### 为什么要这样做

虽然 SQLite 不是最终企业级数据库，但在 Phase 1 它是一个非常合适的过渡方案。

这些设置可以让它在当前阶段更稳一些：

- `WAL` 更适合并发读写
- `busy_timeout` 减少短暂锁冲突直接失败
- `timeout` 给数据库操作更合理的等待时间

## 6. 本地媒体存储默认路径改为项目内部目录

在 `infrastructure/storage/local_media_storage.py` 中，默认目录改为：

- `./data/media/temp`
- `./data/media/output`

### 为什么要这样做

这样比使用个人机器 `Downloads` 目录更稳定，原因包括：

- 更适合测试
- 更适合 CI
- 更适合容器化部署
- 更利于团队统一路径约定

这也是将来切换到 OSS/S3 前，一个更合理的本地适配方式。

## 7. 日志路径改成更可移植的默认值

在 `utils/logger_manager.py` 中，日志默认改为项目内：

- `logs/hiphop_app.log`

并支持通过 `LOG_FILE_PATH` 自定义。

### 为什么要这样做

原因和媒体目录一致：

- 避免依赖个人机器目录
- 避免权限问题
- 避免测试环境启动失败
- 提高部署可控性

## 8. cleanup 失败不会把任务搞卡死

在 `PipelineOrchestrator.run()` 中，cleanup 逻辑现在单独做了保护。

### 为什么要这样做

如果主任务已经成功生成结果，只是清理临时目录失败，那么：

- 不应该把整个任务改判为失败
- 更不应该让任务状态悬空

现在的处理方式是：

- 结果仍保留
- 成功状态仍保留
- cleanup 失败会记录到日志和进度信息里

这种设计更符合真实生产系统的优先级判断：

- 主业务结果优先
- 清理失败可见，但不能吞掉主结果

## 9. 用 lifespan 替代了废弃的 startup 事件写法

在 `api/service.py` 中，原先使用的是：

- `@app.on_event("startup")`

现在改成了 FastAPI 的 `lifespan`。

### 为什么要这样做

因为：

- 原来的写法已经出现 deprecation warning
- 继续沿用会留下技术债
- 现在就改掉成本最低

这样做后，Phase 1 的运行时初始化方式更符合当前 FastAPI 推荐模式。

---

## 六、测试补强了什么

Phase 1 不是只改代码，也补了直接对应风险点的测试。

### 1. `test_phase1_layered_architecture.py`

覆盖了：

- `JobService` 创建与列出任务
- `InMemoryJobRepository` 返回副本行为
- `SQLiteJobRepository` 的写入、读取、列出
- `LocalFilesystemMediaStorage` 的路径和清理行为
- `MissingProducerBackend` 的失败处理
- pipeline 成功路径
- pipeline 失败路径
- cleanup 失败后的稳定性
- 并发任务下 backend 是否隔离

### 2. `test_phase0_api_baseline.py`

覆盖了：

- `POST /create_task` 合同
- `GET /check_status/{task_id}` 合同
- 404 not found 行为
- `GET /list_tasks` 合同
- SQLite backend 下 app 重建后任务状态是否仍可查询

### 3. `test_phase0_config_validation.py`

覆盖了：

- 启动必须环境变量校验
- runtime settings 默认值
- 非法 backend 校验
- repository factory 创建逻辑

---

## 七、CI 收尾做了什么

在 `.github/workflows/ci.yml` 中，之前 CI 只跑了部分测试，不足以完整体现当前 Phase 1 的真实测试面。

现在调整为明确覆盖：

- config validation tests
- phase 1 architecture tests
- baseline API compatibility tests

### 为什么这一步重要

因为重构阶段最怕的不是“本地能跑”，而是：

- CI 没有真实反映风险点
- 合并后别人改坏了也发现不了

把 CI 对齐真实测试面，才算把这一阶段的质量闭环补上。

---

## 八、当前 Phase 1 已完成到什么程度

如果从“核心目标是否完成”来看，Phase 1 现在已经完成。

已经实现的关键结果包括：

- FastAPI 运行时真正使用分层架构
- API 旧接口行为保持兼容
- job repository 支持 memory / sqlite 可切换
- SQLite 模式支持应用重建后的状态保留
- producer backend 实现任务级隔离，降低并发污染风险
- 本地媒体目录和日志目录默认值更适合工程化运行
- cleanup 失败不会轻易导致任务卡住或结果丢失
- 测试和 CI 已覆盖这一阶段的关键风险点
- startup 初始化方式已清理为 lifespan 形式

---

## 九、Phase 1 没做什么

为了避免理解偏差，也需要明确说明：Phase 1 仍然不是最终企业版架构。

当前还没有进入的内容包括：

- PostgreSQL
- Alembic
- RabbitMQ
- Outbox
- Qdrant
- OSS/S3
- 更严格的数据库事务一致性方案
- 分布式 worker

这些都属于后续阶段的工作。

所以当前 Phase 1 的定位应该理解为：

**先把架构边界、运行时稳定性和后续升级接口打牢。**

---

## 十、最终总结

这一轮 Phase 1 的本质，不是简单“整理了目录”，而是完成了以下几件关键事情：

1. 把分层架构从静态文件结构，推进成了真实运行时结构。
2. 把最危险的并发污染点从共享 backend 改成了任务级隔离。
3. 给 job 状态加入了可切换的持久化能力。
4. 把运行默认值改成更可移植、更适合团队协作和 CI 的形式。
5. 用测试和 CI 把这次改造真正闭环。

从后续发展角度看，这一阶段最大的价值是：

- 为 PostgreSQL 接入打地基
- 为消息队列和异步任务系统打地基
- 为更严格的任务状态机打地基
- 为对象存储替换本地文件系统打地基

也就是说，Phase 1 现在已经不只是“重构过一遍”，而是已经成为后续企业化演进的真正基础层。
