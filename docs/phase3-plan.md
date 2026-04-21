# Phase 3 实施计划

## 一、这一阶段的目标

Phase 3 的目标是把当前项目从 Phase 2 的“事务型基础层 + PostgreSQL foundation”，推进到：

- Spotify followed artists 成为可同步、可持久化的上游来源
- YouTube channel discovery 成为可追踪、可重试的服务能力
- RSS candidate discovery 成为可去重、可审计的 catalog 来源
- `artists` 页面有第一版真实可用的 `/v1` BFF 接口
- artist -> channel -> candidate 的来源链路可以在数据库里被追踪

这一阶段不能只做“把脚本搬进 service”，而要优先保证：

- 上游采集过程有明确运行记录
- 失败原因能被保留下来
- repeated scan 不会制造重复 candidate
- 前端消费的是 screen-oriented DTO，而不是 repository 原始结构
- Phase 3 的 schema 演进和 Phase 2 migration 边界清晰分离

---

## 二、当前已经完成的 Phase 3 内容

目前已经完成的内容包括：

- `SyncStatus` / `CandidateStatus`
- `Artist` 的同步元数据字段扩展
- `ArtistSyncRun`
- `VideoCandidate`
- `ArtistRepository.list_all()`
- `ArtistSyncRunRepository`
- `CandidateRepository`
- `artist_sync_runs` / `video_candidates` SQLAlchemy 模型
- `SQLAlchemyArtistSyncRunRepository`
- `SQLAlchemyCandidateRepository`
- `ArtistSyncService`
- `ChannelDiscoveryService`
- `VideoDiscoveryService`
- `CandidateCatalogService`
- provider 注入式 Phase 3 装配：
  - Spotify followed artists lookup
  - YouTube channel lookup
  - YouTube RSS candidate lookup
- `GET /v1/artists`
- `GET /v1/artists/{artist_id}/candidates`
- `POST /v1/artists/{artist_id}/resync`
- `/v1/artists` / `/v1/artists/{artist_id}/candidates` 的分页 contract 已补齐 `total_pages`
- `/v1/artists` 已补 `meta.generated_at`
- `/v1/artists/{artist_id}/resync` response model 已显式声明
  - `channel_run_id`
  - `discovery_run_id`
- 内部入口：
  - `POST /internal/phase3/spotify/sync-followed-artists`
  - `POST /internal/phase3/catalog/resync-active-artists`
- `artists` BFF DTO 中的：
  - `source_health`
  - `retry_metadata`
  - `partial_failure`
  - `latest_run`
  - `latest_candidate`
- Spotify client 改成惰性初始化，避免模块导入时就触发外部认证/浏览器依赖
- Alembic Phase 2 migration 改成快照式
- 新增 Phase 3 catalog migration
- Phase 3 catalog 测试

这意味着 Phase 3 里“纯代码可以完成的上游 catalog 部分”已经基本落地。

---

## 三、下一批推荐实施顺序

### 1. 先做 `artists` 页联调，而不是继续空推代码

现在最自然的下一步，不是再继续凭想象扩很多 API，而是和前端一起验证 `artists` 页面真实 contract：

- list shape 是否够用
- empty state 是否正确
- partial failure 表达是否足够
- retry / resync 按钮语义是否匹配
- sort / filter / pagination 细节是否需要调整
- 前端是否要直接消费 `meta.generated_at` / `pagination.total_pages`
- 是否继续保持 candidates 列表和 artists 列表共用同一分页 contract

### 2. 再做真实环境联通验证

Phase 3 虽然代码闭环已经完成，但要真正收口，还需要确认：

- Spotify followed artists lookup 在真实配置下能取到数据
- YouTube channel lookup 在真实网络环境下能跑通
- RSS discovery 在真实频道数据下能稳定去重

这部分已经触到真实外部依赖，应该通过验证来推进，而不是继续靠猜想写代码。

### 3. 保持 Phase 3 范围边界

Phase 3 之后不建议继续在没有前端验证和真实外部联通确认的前提下，提前进入：

- audit queue
- pipeline
- library
- manual review workflow
- RabbitMQ 真发布

这些已经属于 Phase 4 或更后面的范围。

后续只应继续做：

- 文档收尾
- `artists` 页联调
- 真实 Spotify / YouTube 联通验证
- 根据联调结论微调 DTO / filter / sort / retry contract

---

## 四、这一阶段需要始终坚持的原则

### 1. 先把来源链路产品化，再谈后续 workflow

如果 artist -> channel -> candidate 的来源链不稳定，后面 audit / pipeline / library 只会建立在不可靠输入上。

### 2. 先把 screen contract 做稳定，再谈内部扩展

前端不应该直接理解 repository shape。Phase 3 要优先稳定的是 `artists` 页 contract，而不是继续暴露内部表结构。

### 3. 保持 migration phase 边界清晰

Phase 2 migration 就应该只代表 Phase 2。Phase 3 的 schema 增量必须由独立 revision 承载，不能再回头污染历史 migration。

### 4. 不假设外部依赖已经成功

没有真实 Spotify / YouTube / 前端联调结果之前，不应该把“理论上能跑”当成“已经完成”。代码可以先为真实验证做好边界，但不能靠想象把验证结果写进系统。

---

## 五、当前定义的 Phase 3 完成标准

当前可以认为已经完成的标准是：

- 有明确的 Sync / Candidate 领域状态
- 有 artist sync run 和 candidate catalog 数据模型
- 有来源链路追踪能力
- 有 `artists` 页第一版 `/v1` BFF API
- artists / candidates API 的分页与 meta contract 已统一并通过测试锁定
- 有 Spotify sync / artist resync / batch refresh 的纯代码闭环
- repeated scan 下 candidate 可以按业务键去重
- Alembic 能把 Phase 3 schema 正确迁移出来
- 测试已覆盖并通过
- `randyTranslation/.venv` 下已重新跑通 `test_phase3_catalog.py`

剩余未完成的部分已经明显属于联调验证或下一阶段：

- 和前端一起确认 `artists` screen contract
- 真实 Spotify / YouTube 联通验证
- 更丰富的筛选、排序、分页细节打磨
- audit queue / pipeline / library 的 BFF
- manual review 工作流
- RabbitMQ 和异步 worker

---

## 六、总结

Phase 3 到这里已经不再只是“source ingestion 的设计草图”，而是已经完成了：

- 代码实现
- 数据模型演进
- BFF 首版接口
- 本地测试验证
- migration 边界修正

后续继续推进时，最推荐的顺序是：

1. 完成 `artists` 页联调
2. 完成真实 Spotify / YouTube 联通验证
3. 再进入 Phase 4 的 workflow / manual review / secure BFF

这样推进，最符合当前项目“先把上游 catalog 做稳，再进入产品工作流”的节奏。

---

## 七、基于 artistPageSmokeTest 的状态更新

`artistPageSmokeTest` 这一轮真实联调之后，Phase 3 原来标记为“待验证”的两项关键工作，已经不再停留在计划层：

- `artists` 页联调已完成
- 真实 Spotify / YouTube / RSS 数据面已完成一轮本地 smoke test

这次补充确认的事实包括：

- 本地 Postgres schema 已从 `20260415_220500` 升级到 `20260419_120000`
- Python API 已在本地真实启动并监听 `127.0.0.1:8000`
- `create_phase3_catalog_service()` 已确认不是 `None`
- `POST /internal/phase3/spotify/sync-followed-artists`
  - 返回 `200`
  - 实际结果：`synced_count=625`、`created_count=625`、`updated_count=0`
- `POST /internal/phase3/catalog/resync-active-artists?days=14&limit=20`
  - 返回 `200`
  - 实际结果：`requested=20`、`refreshed=20`、`failed=0`
- `GET /v1/artists?page=1&page_size=5&sort=last_synced_desc`
  - 返回 `200`
  - 实际结果：`total=625`
  - 至少一个 artist 已存在真实 candidates
- Next BFF 已通过真实联调验证：
  - `GET /api/artists`
  - `GET /api/artists/[artistId]/candidates`
  - `POST /api/artists/[artistId]/resync?days=7`
- SSR 页面已通过真实联调验证：
  - `GET /artists`
  - `GET /artists/[artistId]`

这一步也顺带暴露并修复了两个真正的页面级阻塞：

1. SSR 环境请求相对 `/api/...` URL 会失败
2. 后端 datetime 形式不统一，导致 artists BFF 时间字段在前端 schema 校验阶段失败

修复后，`artists` 这一条链已经从“Phase 3 代码闭环已完成”推进到了“真实后端数据面、Next BFF、SSR 页面都已完成 smoke test”的状态。

因此，Phase 3 剩余重点也应该同步调整，不再把“联调是否能跑通”当作主问题，而是转向：

- channel lookup 命中精度
- candidate relevance 筛选质量
- 特殊 artist 名称歧义
- 后续更细的前端体验增强
