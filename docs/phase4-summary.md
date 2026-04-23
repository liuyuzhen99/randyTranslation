# Phase 4 当前阶段总结

## 一、这一轮 Phase 4 实际完成了什么

这一轮 Phase 4 是在 Phase 3 已经完成 source ingestion 和 candidate catalog 的基础上，继续把系统推进到“可审核、可追踪、可给前端联调”的状态。

当前代码里，Phase 4 已经完成的核心能力是：

- 建立显式 review checkpoint workflow
- 把 manual review 明确放进业务流程
- 接出 `audit queue / pipeline / library` 三个 `/v1` BFF
- 接出 review approve / reject 动作接口
- 接出 transcript / taste-audit / translation 三类 AI 结果写回接口
- 增加 review stale-version conflict 保护
- 增加 audit log 持久化和查询接口
- 统一 `/v1` 的 response meta、error envelope 和 request id

这里有一个需要特别说明的更新：

- 之前实现过一版 `reviewer / curator / admin` 权限边界
- 现在已经按最新决策移除
- 当前代码保留业务 workflow 和审计能力，但不做角色权限限制

也就是说，Phase 4 当前阶段的重点，已经从“权限分工”收回到了：

- workflow 语义是否正确
- review action 是否稳定
- 前后端是否能顺利联调

---

## 二、改造前的代码和问题

在 Phase 3 结束后，系统已经具备：

- PostgreSQL / SQLAlchemy / Alembic 基础
- `artists` catalog
- `video_candidates`
- 第一批 `/v1/artists` BFF

但是从产品流程角度看，还存在明显缺口。

### 1. candidate 只有 catalog，没有 workflow

原先 `video_candidates` 只能表达：

- 这是哪个候选视频
- 来源是什么
- 当前大致是 `pending_review / accepted / rejected`

但它还不能表达：

- 当前卡在哪个 review checkpoint
- 一共经过了哪些 review 阶段
- 审批通过后要推进到哪一步

这会导致：

- `audit queue` 只能是一个模糊列表
- `pipeline` 无法展示真实业务流程
- `library` 无法判断“是否是完整通过后的结果”

### 2. manual review 还不是正式业务节点

你的原始产品要求里，manual review 要放在 taste audit 之后。  
在 Phase 3 结束时，这个要求还没有真正落成模型，只停留在后续规划。

如果 manual review 不先成为正式 checkpoint，后面前端和后端都会在这些问题上不断返工：

- 什么时候该人工介入
- 审核通过后去哪一关
- 驳回后状态如何变化

### 3. 没有可以直接联调的审核流接口

Phase 3 结束时，前端主要能对接的是：

- `artists`
- `candidates`
- `resync`

但 review workflow 相关页面缺少正式接口：

- `audit queue`
- `pipeline`
- `library`
- 审核动作 approve / reject

所以如果不进入 Phase 4，前端还是只能继续靠 mock。

### 4. 没有显式冲突保护

review 是强业务决策动作。  
如果不同客户端拿着旧数据一起提交 approve / reject，而系统没有版本校验，就会出现：

- 重复审批
- 陈旧页面覆盖新状态
- 审核轨迹混乱

### 5. 审计日志还只是“需要有”，不是“已经有”

在 roadmap 里，Phase 4 就要求开始具备 auditability。  
但在这轮实现前，系统还没有正式的 review 决策日志模型，也没有相应查询接口。

---

## 三、为什么这次这样实现

这一轮实现的原则，是先把 Phase 4 最关键的业务闭环做出来，而不是提前进入 Phase 5/6 的更重能力。

### 1. 先把 workflow 做成显式对象，而不是继续堆状态字段

这次没有把所有 review 信息压进 `VideoCandidate`，而是新增了 `ReviewItem`。

原因是：

- 一个 candidate 会经过多次 review
- 每次 review 都有独立状态
- 每次 review 都有自己的版本号、决策时间、备注

所以 review 必须成为一个独立业务对象。

### 2. 先把手工审核流跑通，再考虑更复杂的账号体系

当前代码已经把 workflow、approve/reject、audit log 和 BFF 接口做出来。  
但因为你准备先做前后端联调，所以当前实现已经把早先那版 `reviewer / curator / admin` 权限边界去掉了。

这样做的目的不是否定权限系统，而是为了：

- 先验证业务流程是否和你的产品想法一致
- 先让前端能直接消费 Phase 4 的业务接口
- 避免联调阶段被权限细节阻塞

### 3. 优先把冲突语义做清楚

这次 approve / reject API 都要求前端传 `expected_version`。

为什么这样做：

- review 决策不能默默覆盖
- stale-version 是审核系统里非常真实的业务风险
- 这个语义越早进入 API contract，前端越不容易以后返工

### 4. 先统一 `/v1` 接口风格

这次我还统一了：

- `/v1` success response 的 `meta`
- `/v1` 的 error envelope
- `X-Request-Id`
- request validation error 也走统一 envelope

原因很简单：

- 只要进入前后端联调，这些东西就必须稳定
- 如果一个接口返回 `detail`，另一个接口返回别的格式，联调体验会很差

### 5. 当前明确采用 polling，而不是抢先做 SSE

Phase 4 现在已经明确在 response meta 中声明：

- `update_mode = polling`
- `refresh_hint_seconds = 15`

这代表当前联调阶段，页面刷新策略已经有稳定约定，但还没有提前进入 SSE / WebSocket。

---

## 四、现在代码已经变成什么样

### 1. 领域层已经能表达 review workflow

当前在 `domain/enums.py` 中新增了：

- `ReviewType`
- `ReviewStatus`

workflow 顺序目前定义为：

1. `transcript_review`
2. `taste_audit`
3. `manual_review`
4. `translation_review`
5. `final_asset_approval`

在 `domain/entities.py` 中新增了：

- `ReviewItem`
- `AuditLogEntry`

`ReviewItem` 当前包含：

- `review_id`
- `subject_kind`
- `subject_id`
- `spotify_id`
- `review_type`
- `status`
- `version`
- `decision_comment`
- `decided_by`
- `decided_at`
- `created_at`
- `updated_at`

`AuditLogEntry` 当前包含：

- `log_id`
- `aggregate_type`
- `aggregate_id`
- `action`
- `actor_id`
- `details`
- `created_at`

### 2. persistence 层已经能落 review 和 audit log

当前数据库里新增了两张表：

- `review_items`
- `audit_log_entries`

其中关键约束包括：

- `review_items(subject_kind, subject_id, review_type)` 唯一
- `version >= 1`
- review 按 `(status, created_at)` 建索引
- audit log 按 aggregate 和 actor 建索引

### 3. application 层已经有真正的 workflow service

当前新增的 service 包括：

- `ArtistService`
- `AuditService`
- `PipelineService`
- `LibraryService`
- `TranslationService`
- `AutomationService`
- `WorkflowSupport`

其中 `WorkflowSupport` 负责：

- review checkpoint 初始化
- review approve / reject
- workflow 推进
- transcript / translation 所需的 candidate、video、subtitle 基础操作

### 4. Phase 4 现在已经补齐 AI 结果进入 workflow 的正式入口

当前 `/v1` 已新增三类同步入口，专门用于把 AI 执行结果接入 Phase 4 workflow：

- `POST /v1/candidates/{candidate_id}/transcript`
- `POST /v1/candidates/{candidate_id}/taste-audit`
- `POST /v1/candidates/{candidate_id}/translation`

它们解决的是同一个问题：

- 之前 Phase 4 只有 review checkpoint，但 AI 结果没有正式入口
- 所以前后端联调时只能靠手工 approve 来推进

现在这三个入口已经把正确的阶段边界固定下来：

- `transcript`：写入英文字幕，并可选择自动通过 `transcript_review`
- `taste-audit`：记录 AI Auditor 结果，并推进 `taste_audit`
- `translation`：写入中文字幕，并可选择自动通过 `translation_review`

这一层仍然是同步、可审计、面向联调的能力，不等于已经做完后续的异步 stage queue 编排。

- 初始化第一道 review checkpoint
- 校验 `expected_version`
- approve / reject
- 推进到下一 checkpoint
- 写入 audit log
- 最终把 candidate 推进到 `accepted`

### 4. Phase 4 BFF 已经接出可联调页面

当前 `/v1` 接口包括：

- `GET /v1/audit-queue`
- `GET /v1/pipeline`
- `GET /v1/library`
- `GET /v1/audit-log`
- `POST /v1/reviews/{review_id}/approve`
- `POST /v1/reviews/{review_id}/reject`

另外 Phase 3 的接口仍然可用：

- `GET /v1/artists`
- `GET /v1/artists/{artist_id}/candidates`
- `POST /v1/artists/{artist_id}/resync`

### 5. 当前已经没有角色权限拦截

当前代码已经去掉了：

- `reviewer / curator / admin` 枚举
- `requested_role`
- `actor_role`
- `X-User-Id`
- `X-User-Role`
- `library` 权限校验
- `audit queue` 按角色过滤

当前 review action 只保留：

- 可选请求头 `X-Actor-Id`

作用是：

- 记录是谁做的审批动作
- 如果不传，默认记成 `manual-review`

所以现在业务上已经没有“谁能看谁不能看”的限制，方便直接联调。

### 6. `/v1` 响应已经统一

当前所有 `/v1` 成功响应都带：

- `meta.generated_at`
- `meta.update_mode`
- `meta.refresh_hint_seconds`

当前所有 `/v1` 错误响应都统一走 error envelope，并返回 request id。

---

## 五、测试和验证结果

当前 Phase 4 相关测试主要覆盖：

- review checkpoint 自动初始化
- approve 连续推进 workflow
- stale-version 返回 409
- `pipeline` 在完整推进后的状态
- `library` 在最终审批完成后的可见结果
- `audit-log` 查询
- Alembic 升级到 head 后 Phase 4 表存在
- `/v1` validation error 的统一 envelope

实际回归运行命令：

```bash
PYTHONPATH=. .venv/bin/python -m unittest discover -s test -p 'test_phase[34]*.py'
```

结果：

- 共运行 8 个测试
- 全部通过

---

## 六、现在可以做哪些前后端联调

这是当前最重要的部分。

### 1. `artists` 页面

可联调接口：

- `GET /v1/artists`

可联调内容：

- artist 列表
- 分页
- 搜索 `q`
- `sync_status` 过滤
- `sort`
- `latest_candidate`
- `latest_run`
- `source_health`
- `retry_metadata`

### 2. artist 下的 candidate 列表

可联调接口：

- `GET /v1/artists/{artist_id}/candidates`

可联调内容：

- candidate 列表
- candidate 状态过滤
- 分页
- 候选视频基础展示信息

### 3. artist 手动 resync

可联调接口：

- `POST /v1/artists/{artist_id}/resync`

可联调内容：

- 手动刷新 artist
- 查看返回的 `run_id`
- 查看 `channel_run_id` / `discovery_run_id`
- 成功刷新后的候选变化

### 4. `audit queue` 页面

可联调接口：

- `GET /v1/audit-queue`

可联调内容：

- 当前所有待审核 review 节点列表
- 每条 review 的类型、版本号、排队时间
- candidate 基础信息展示

### 5. review approve / reject

可联调接口：

- `POST /v1/reviews/{review_id}/approve`
- `POST /v1/reviews/{review_id}/reject`

请求体：

```json
{
  "expected_version": 1,
  "comment": "optional"
}
```

可选请求头：

```http
X-Actor-Id: frontend-user-1
```

可联调内容：

- 审核通过
- 审核拒绝
- 通过后推进到下一阶段
- 驳回后 candidate 进入拒绝态
- comment 是否正确记录

### 6. stale-version 冲突处理

可联调内容：

- 故意提交旧版本 `expected_version`
- 后端返回 409
- 前端处理冲突提示和刷新逻辑

### 7. `pipeline` 页面

可联调接口：

- `GET /v1/pipeline`

可联调内容：

- 当前 candidate 的 workflow 状态
- 当前 stage
- 各阶段 `stages`
- translation 状态摘要
- `last_updated_at`

### 8. `library` 页面

可联调接口：

- `GET /v1/library`

可联调内容：

- 只有最终审批通过后的内容才会出现
- `approved_at`
- `approved_by`
- `curation_status`

### 9. 审计轨迹查看

可联调接口：

- `GET /v1/audit-log?aggregate_type=...&aggregate_id=...`

可联调内容：

- 某个 candidate 的 workflow 轨迹
- 某个 review 的决策轨迹
- actor id、action、details、created_at 展示

### 10. `/v1` 通用协议

可联调内容：

- success response 中统一的 `meta`
- 当前采用 `polling`
- error envelope
- request validation error
- `X-Request-Id`

---

## 七、下一步最应该做什么

当前最合适的下一步，不是继续在后端里盲目扩功能，而是按下面顺序推进。

### 第一步：做前后端联调确认

优先确认这些问题：

- `audit queue` 的数据形态是不是符合页面预期
- `pipeline` 的阶段展示是不是符合你的产品理解
- `library` 当前是否满足 Phase 4 页面需要
- approve / reject 后页面要如何刷新
- stale-version 冲突前端要怎么提示

### 第二步：补齐联调过程中暴露出的 DTO 细节

这一轮最可能暴露的问题通常是：

- 字段命名是否要调整
- 某个页面是不是还缺 1 到 2 个展示字段
- 错误文案是不是需要更前端友好
- 列表排序和分页语义是否需要微调

这些都属于联调期正常收口。

### 第三步：如果联调语义稳定，再进入 Phase 5

如果前端联调后确认 Phase 4 workflow 和页面语义已经对齐，那么下一阶段最自然的方向就是：

- Phase 5 的 artifact / OSS / library deep detail

也就是把 library 从“审核通过列表”推进到“真正可消费的资产结果”。

---

## 八、当前结论

到目前为止，Phase 4 在当前代码库里已经完成了最有价值的后端部分：

- candidate 已进入显式 workflow
- manual review 已经成为真实业务节点
- `audit queue / pipeline / library` 已经有可联调 BFF
- approve / reject 已具备冲突保护
- 审计轨迹已经存在并可查询
- `/v1` 接口风格已经统一
- 角色权限限制已经移除，方便直接联调

所以当前最优先的工作，已经不是继续闭门造车，而是开始前后端联调，把页面语义和后端 contract 最终对齐。
