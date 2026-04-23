**Phase 4 详细 Review 报告**

Phase 4 的目标，是把项目从 Phase 3 的“source ingestion + candidate catalog”，推进到“显式 workflow、manual review、可联调的 BFF、审计能力和稳定的接口契约”。这一次我真正完成的事情，核心可以归成 8 大块：领域模型扩展、workflow 持久化、应用服务建模、BFF API、review action 并发保护、审计轨迹、统一错误契约、以及联调前的收口判断。

这里补一条当前最新状态：

- Phase 4 现在已经补齐 transcript / taste-audit / translation 的 AI 结果写回入口
- 所以系统不再只是“手工 approve/reject 的 workflow 骨架”
- 当前已经能把 AI 执行结果在正确 checkpoint 写入并推进 workflow
- 但完整异步 stage queue、自动调度和重试编排，仍然属于后续阶段

**1. 先做了领域层收口：把 review workflow 的业务语义定义清楚**
这一步主要改的是：

- [domain/enums.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/domain/enums.py)
- [domain/entities.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/domain/entities.py)
- [domain/repositories.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/domain/repositories.py)

我先补了：

- `ReviewType`
- `ReviewStatus`

然后新增了：

- `ReviewItem`
- `AuditLogEntry`

同时补了 repository 契约：

- `ReviewRepository`
- `AuditLogRepository`

为什么这么做：

- 如果 Phase 4 还继续只靠 `VideoCandidate.status` 这类粗粒度字段表达审核流，后面的 `audit queue`、`pipeline`、`library` 都只能看到一个模糊结果，看不到真实业务过程
- manual review 是明确业务节点，不是备注字段，所以必须进入领域模型
- audit log 也不能只停留在日志打印，应该成为正式业务记录

这里最关键的收口，不是“多了几个 dataclass”，而是把 Phase 4 的业务问题先定义清楚了：

- 一个 candidate 会经过多个 review checkpoint
- 每个 checkpoint 有独立状态和版本
- 每一次决策和推进都可以形成审计事件

这一步把后面的 API contract 和并发保护都建立在稳定语义上，而不是拍脑袋拼字段。

**2. 然后做了 SQLAlchemy 模型和 repository，让 workflow 和 auditability 真正落库**
这部分主要改的是：

- [infrastructure/persistence/sqlalchemy_models.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/infrastructure/persistence/sqlalchemy_models.py)
- [infrastructure/persistence/sqlalchemy_repositories.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/infrastructure/persistence/sqlalchemy_repositories.py)
- [alembic/versions/20260421_120000_phase4_workflow_schema.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/alembic/versions/20260421_120000_phase4_workflow_schema.py)

我新增了两张核心表：

- `review_items`
- `audit_log_entries`

关键约束包括：

- `UniqueConstraint(subject_kind, subject_id, review_type)`
- `CheckConstraint(version >= 1)`
- `review_items` 上按 `(status, created_at)` 建索引
- `audit_log_entries` 上按 aggregate 和 actor 建索引

为什么这么做：

- 同一个 candidate 的同一类 review checkpoint 不应该重复创建，业务唯一约束要在数据库层就成立
- review 是强业务决策动作，所以 optimistic concurrency 的前提 `version >= 1` 必须落库
- `audit queue` 的核心查询模式就是看当前 pending 的 review，不加索引后面只会越来越慢
- audit log 后面不管是排障还是前端展示轨迹，都会高频按 aggregate 或 actor 查

在 repository 层，我新增了：

- `SQLAlchemyReviewRepository`
- `SQLAlchemyAuditLogRepository`

为什么这么做：

- Phase 4 不应该让 API 直接面向 SQLAlchemy model
- review 和 audit log 都是明确业务对象，应该通过 repository contract 进入 application service

这一步的价值在于：workflow 和 auditability 已经是“系统正式能力”，而不是只存在于某个 API 里的一段逻辑。

**3. 接着做了 application 层建模：把 workflow / audit / pipeline / library 变成真正服务**
这部分主要改的是：

- [application/services/phase4_workflow_service.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/application/services/phase4_workflow_service.py)
- [api/config.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/api/config.py)

我新增了：

- `ArtistService`
- `AuditService`
- `PipelineService`
- `LibraryService`
- `TranslationService`
- `WorkflowSupport`
- `Phase4WorkflowServices`

为什么这样拆：

- roadmap 已经明确 Phase 4 需要这些 workflow-oriented service
- 但如果 review 初始化、状态推进、审计写入这些基础逻辑不先收进共享 support 层，后面 service 会越来越重复

这次最核心的逻辑集中在 `WorkflowSupport`：

- `bootstrap_reviews()`：为 candidate 自动初始化第一道 `transcript_review`
- `apply_review_decision()`：执行 approve / reject
- 校验 `expected_version`
- 通过时推进到下一个 checkpoint
- 拒绝时把 candidate 置为 `rejected`
- 写审计日志

workflow 顺序目前定义为：

1. `transcript_review`
2. `taste_audit`
3. `manual_review`
4. `translation_review`
5. `final_asset_approval`

为什么按这个顺序做：

- 这和 roadmap 里列出的审核链路一致
- 你一开始就强调 manual review 要放在 taste audit 之后
- final approval 则单独作为最后一关，确保 `library` 的结果是完整流转后的结果

这里最重要的业务意义是：candidate 终于不只是 catalog 记录，而是真正进入了一条显式工作流。

**4. 再往前做了 `/v1` BFF：把 `audit queue / pipeline / library / audit-log / review actions` 真正接出来**
这部分主要改的是：

- [api/service.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/api/service.py)

我接出了这些 Phase 4 关键接口：

- `GET /v1/audit-queue`
- `GET /v1/pipeline`
- `GET /v1/library`
- `GET /v1/audit-log`
- `POST /v1/reviews/{review_id}/approve`
- `POST /v1/reviews/{review_id}/reject`

同时 Phase 3 的接口继续保留可用：

- `GET /v1/artists`
- `GET /v1/artists/{artist_id}/candidates`
- `POST /v1/artists/{artist_id}/resync`

为什么这样做：

- 前端联调不能只靠 `artists`，Phase 4 的主页面本来就应该是 `audit queue / pipeline / library`
- review approve / reject 必须成为正式 API，而不是内存动作或后端私有行为
- audit log 既是审计能力，也是联调和排障时的必要工具

在 DTO 组织上，我尽量坚持 screen-oriented：

- `audit-queue` 返回待审核项、candidate 基础信息、review 类型、版本号、queued 时间
- `pipeline` 返回 workflow 状态、当前 stage、全阶段状态、translation 状态摘要
- `library` 返回最终审批通过后的可入库项
- `audit-log` 返回某个 aggregate 的轨迹

为什么这样做：

- BFF 的目标是服务页面，不是直接吐 repository entity
- 如果现在就把 contract 收口好，后面前端不会被迫理解太多后端内部结构

**5. 然后我补了 approve / reject 的并发保护，让 review action 语义稳定**
这部分还是体现在：

- [application/services/phase4_workflow_service.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/application/services/phase4_workflow_service.py)
- [api/service.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/api/service.py)

现在 review action 都要求客户端传：

- `expected_version`

为什么这么做：

- stale-version conflict handling 是 Phase 4 最关键的业务风险之一
- review 不是可以随便覆盖的轻操作，而是会影响 workflow 推进的强决策动作
- 如果没有 expected version 检查，多个客户端同时审批时一定会出问题

当前语义是：

- 版本一致才允许 approve / reject
- 版本不一致返回 `409`

这个能力的价值非常直接：

- 前端可以明确处理“你看到的是旧状态”
- 后端不会无声吞掉冲突

**6. 我还补了 audit log 和查询接口，让决策轨迹真正可追踪**
这部分主要改的是：

- [application/services/phase4_workflow_service.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/application/services/phase4_workflow_service.py)
- [api/service.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/api/service.py)

当前系统会记录这些关键动作：

- `review_checkpoint_created`
- `review_approved`
- `review_rejected`
- `workflow_promoted`

并且可以通过：

- `GET /v1/audit-log?aggregate_type=...&aggregate_id=...`

来查询。

为什么这样做：

- Phase 4 不只是“状态变化”，而是“谁在什么时间做了什么决策”
- 联调时如果没有轨迹接口，很多问题只能靠猜
- 后面要做审计或排障时，这条链路也必须存在

当前审计记录保留了：

- `actor_id`
- `action`
- `details`
- `created_at`

同时按你的最新要求，早先那版基于 `reviewer / curator / admin` 的权限字段已经移除，不再成为当前联调阻碍。

**7. 我还统一了 `/v1` 的错误 envelope 和 request id，把联调层契约做稳定**
这部分主要改的是：

- [api/service.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/api/service.py)

我做了几件关键事情：

- 给 `/v1` 接口统一加了 success `meta`
- 给 `/v1` 错误统一加了 error envelope
- middleware 注入 `X-Request-Id`
- request validation error 也统一包进 `/v1` error envelope
- 当前明确采用 `polling`

为什么这么做：

- 一旦进入前后端联调，接口“长得一致”本身就是非常重要的能力
- 如果一个接口 validation error 返回一套结构，另一个业务错误又是另一套结构，前端成本会很高
- `request_id` 能帮助双方快速定位请求

现在 `/v1` 的稳定约定包括：

- `meta.generated_at`
- `meta.update_mode`
- `meta.refresh_hint_seconds`
- error envelope
- `X-Request-Id`

这一步其实是把 BFF 从“能返回数据”推进到“可以稳定联调”的关键。

**8. 测试和迁移验证是怎么补的，为什么这么补**
这部分主要改的是：

- [test/test_phase4_workflow.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/test/test_phase4_workflow.py)
- [test/test_phase3_catalog.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/test/test_phase3_catalog.py)

我补的测试，重点覆盖的是最容易出问题的点：

- review checkpoint 自动初始化
- approve 连续推进 workflow
- stale-version 返回 409
- 最终推进后 `pipeline` 状态正确
- 最终推进后 `library` 可见
- `audit-log` 能查到 workflow promotion
- validation error 统一走 envelope
- Alembic head 包含 Phase 4 表

为什么这样测：

- Phase 4 的风险核心不在“接口能不能 200”，而在 workflow 语义是不是稳定
- 只测 happy path 不够，冲突处理和轨迹查询同样重要

我回归跑了：

```bash
PYTHONPATH=. .venv/bin/python -m unittest discover -s test -p 'test_phase[34]*.py'
```

结果是：

- 共运行 8 个测试
- 全部通过

这说明当前 Phase 3 和 Phase 4 的主路径 contract 是兼容的，没有互相打架。

**9. 这轮实现后，当前已经可以联调哪些内容**
从业务角度看，现在已经可以和前端联调这些内容：

- `artists` 列表
- artist 下 candidate 列表
- artist 手动 resync
- `audit queue` 页面
- review approve / reject
- stale-version 冲突提示
- `pipeline` 页面
- `library` 页面
- `audit-log` 轨迹查看
- `/v1` 通用 success/error 契约

其中我认为优先级最高的联调顺序是：

1. `GET /v1/audit-queue`
2. `POST /v1/reviews/{review_id}/approve|reject`
3. `GET /v1/pipeline`
4. `GET /v1/library`
5. `GET /v1/audit-log`
6. 再把 `artists` 页一起串起来

为什么这个顺序更好：

- Phase 4 当前最需要验证的是 workflow 语义，而不是 catalog 本身
- 先把审核流跑通，后面 `artists -> candidates -> review -> pipeline -> library` 才能连成完整闭环

**10. 下一步最应该做什么**
如果从当前阶段判断，下一步最应该做的不是继续在后端里盲目扩功能，而是：

1. 做一次真正的前后端联调确认
2. 根据联调结果微调 DTO、字段命名、错误文案和刷新语义
3. 如果联调确认 workflow 语义稳定，再进入 Phase 5

Phase 5 更自然的方向会是：

- artifact / OSS / library deep detail
- 让 library 从“审核通过列表”变成“真正资产结果页”

当前还不建议继续在 Phase 4 内部盲目往下做的内容包括：

- 重新加复杂权限系统
- 提前做 SSE / WebSocket
- 提前做 RabbitMQ async pipeline
- 提前做 media/OSS 深度联动

因为这些都会把当前联调焦点打散。

**11. 我为什么说 Phase 4 到这里在当前代码范围内已经基本完成**
因为属于 Phase 4 的核心事情，现在已经具备：

- explicit review checkpoints
- manual review 正式进入 workflow
- `audit queue / pipeline / library` 的 `/v1` BFF
- approve / reject action
- stale-version conflict handling
- audit log and audit-log API
- polling 策略与统一 response envelope
- 足够支撑前后端开始联调

也就是说，当前阶段的关键不再是“继续写多少后端代码”，而是“把已经实现的业务语义和前端页面真正对齐”。
