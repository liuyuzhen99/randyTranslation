# Phase 3 当前阶段总结

## 一、这一轮 Phase 3 做了什么

这一轮 Phase 3 没有直接跳去 RabbitMQ 或后续审核流，而是严格按照 `docs/roadmap.md` 里定义的范围，先把上游采集和候选 catalog 打成第一批可交付能力：

- 单独切出 `phase3` 分支开展实现
- 补齐 Phase 3 需要的领域状态：
  - `SyncStatus`
  - `CandidateStatus`
- 补齐 Phase 3 需要的领域实体：
  - `Artist` 扩展同步状态与时间字段
  - `ArtistSyncRun`
  - `VideoCandidate`
- 补齐 repository 契约：
  - `ArtistRepository.list_all()`
  - `ArtistSyncRunRepository`
  - `CandidateRepository`
- 补齐 SQLAlchemy 持久化模型：
  - `artist_sync_runs`
  - `video_candidates`
  - `artists` 上的同步元数据字段
- 实现 Phase 3 catalog 应用服务：
  - `ArtistSyncService`
  - `ChannelDiscoveryService`
  - `VideoDiscoveryService`
  - `CandidateCatalogService`
- 接出第一版 `artists` BFF API：
  - `GET /v1/artists`
  - `GET /v1/artists/{artist_id}/candidates`
  - `POST /v1/artists/{artist_id}/resync`
- 增加默认 provider 装配：
  - Spotify followed artists lookup
  - YouTube channel lookup
  - YouTube RSS candidate lookup
- 增加 Phase 3 内部触发入口：
  - `POST /internal/phase3/spotify/sync-followed-artists`
  - `POST /internal/phase3/catalog/resync-active-artists`
- 补齐 source health / retry metadata / partial failure DTO 字段
- 修正 Alembic 迁移边界：
  - 把 Phase 2 首个 migration 改成真正快照式
  - 新增 Phase 3 catalog migration
- 增加 Phase 3 测试，覆盖：
  - resync 持久化闭环
  - `/v1/artists` / candidates / resync API
  - Alembic 迁移到 Phase 3 的建表结果

---

## 二、改造前的代码和问题

在 Phase 2 完成后，项目已经有了 PostgreSQL / SQLAlchemy / Alembic / Job 生命周期这些基础，但如果从 `artists` 页面和上游内容 catalog 的角度看，仍然有几个明显缺口：

### 1. `artists` 还不是一个可消费的产品化 catalog

虽然已经有 `artists` 和 `videos` 表，但它们还更像“给内部流程留的基础表”，还不是前端能稳定消费的 screen-oriented 数据模型。

缺少的东西包括：

- 艺人同步状态
- 最近同步时间
- 失败原因
- 每次 resync 的运行记录
- 候选视频的 catalog 语义
- 从 artist -> channel -> candidate 的来源追踪

### 2. 旧抓取脚本是脚本，不是服务

现有的：

- `services/getSpotifyFollowingList.py`
- `services/getChannelIDfromFollowingList.py`
- `services/getLatestMVfromRss.py`

它们更多是“能跑起来的抓取逻辑”，但还不是 application service 层可组合、可审计、可测试、可被 BFF 调用的服务。

### 3. Alembic 边界会被新模型污染

原来的首个 migration 直接引用运行时 `Base.metadata`。这在 Phase 2 时问题不大，但一旦进入 Phase 3，新表和新列会反向污染旧 migration，导致：

- Phase 2 migration 不再代表 Phase 2
- 新环境升级路径不可预测
- 后续阶段继续加表时风险越来越高

这类问题如果不现在修，后面只会更难收。

---

## 三、为什么这次这样实现

这一轮实现的原则是：

### 1. 先把 `artists` 页所需的“真实后端数据闭环”做出来

Phase 3 的目标不是单纯“多几个表”，而是让前端 `artists` 页第一次可以不依赖 mock 数据，直接读取真实 catalog。

所以这轮最重要的是：

- 艺人有同步状态
- resync 有运行记录
- candidate 有去重和列表形态
- API 返回的是 screen-oriented DTO，而不是 repository 原始结构

### 2. 先做 catalog，再做更重的 workflow

`docs/roadmap.md` 里明确写了，Phase 3 是 Source Ingestion and Candidate Catalog。  
也就是说，现在应该先把上游来源做稳定，而不是提前进入：

- RabbitMQ fan-out
- manual review
- library
- pipeline timeline

这些属于后面阶段。

### 3. 迁移边界必须立即修正

这一步虽然看起来像“顺手维护”，但实际上非常关键。因为如果 migration 继续绑定运行时模型，后面的每个 phase 都会把历史 revision 搞脏。

所以我这次把它改成：

- Phase 2 migration 只代表 Phase 2 当时的 schema
- Phase 3 migration 单独负责 catalog 增量

这样以后每一阶段的数据库演进才是真正可追踪、可回滚、可验证的。

---

## 四、现在代码变成了什么样

### 1. 领域层已经具备 Phase 3 的状态表达能力

在 `domain/enums.py` 中新增了：

- `SyncStatus`
- `CandidateStatus`

在 `domain/entities.py` 中新增或扩展了：

- `Artist.sync_status`
- `Artist.last_sync_started_at`
- `Artist.last_sync_completed_at`
- `Artist.last_sync_error`
- `Artist.last_channel_resolved_at`
- `Artist.last_discovery_at`
- `ArtistSyncRun`
- `VideoCandidate`

这样做之后，Phase 3 的核心业务语义已经从“零散字段”变成了明确领域对象。

### 2. persistence 层已经能承载 artist catalog

在 `infrastructure/persistence/sqlalchemy_models.py` 里，新增了：

- `artist_sync_runs`
- `video_candidates`

并且扩展了 `artists` 表，让它能直接表达同步状态和最近一次运行结果。

在 `infrastructure/persistence/sqlalchemy_repositories.py` 里，新增了：

- `SQLAlchemyArtistSyncRunRepository`
- `SQLAlchemyCandidateRepository`

同时给 `SQLAlchemyArtistRepository` 增加了 `list_all()`，这样 BFF 层可以做分页、筛选和 screen DTO 组装。

### 3. application 层已经有了真正的 Phase 3 catalog 服务

新增文件：

- `application/services/phase3_catalog_service.py`

这里面我没有把所有逻辑都塞到一个 service 里，而是按职责拆成：

- `ArtistSyncService`
- `ChannelDiscoveryService`
- `VideoDiscoveryService`
- `CandidateCatalogService`

其中真正对 BFF 暴露能力的是 `CandidateCatalogService`，它负责：

- 同步 Spotify followed artists
- 列艺人
- 列候选
- 手动 resync 单个艺人
- 批量刷新 active artists catalog

而 channel lookup / candidate lookup 则通过 provider 注入，这样做的价值是：

- 真实抓取逻辑和应用层解耦
- 测试可以用 fake provider
- 后续换成 RabbitMQ / worker / 更稳定的 provider 时不需要改 BFF 合同

### 4. API 层已经有了第一版 `/v1/artists`

在 `api/service.py` 中接出了：

- `GET /v1/artists`
- `GET /v1/artists/{artist_id}/candidates`
- `POST /v1/artists/{artist_id}/resync`

同时也补了纯后端触发入口：

- `POST /internal/phase3/spotify/sync-followed-artists`
- `POST /internal/phase3/catalog/resync-active-artists`

返回形态也不是直接吐 repository entity，而是整理成 BFF-oriented 结构，带上：

- `pagination`
- `meta`
- `latest_candidate`
- `latest_run`
- `candidate_count`
- `sync_status`
- `source_health`
- `retry_metadata`
- `partial_failure`

这就是 Phase 3 要的“artists screen contract”。

后面为了进入真正的前端联调，我又补了一轮 contract 收口：

- `/v1/artists`
  - `pagination.total_pages`
  - `meta.generated_at`
- `/v1/artists/{artist_id}/candidates`
  - `pagination.total_pages`
- `/v1/artists/{artist_id}/resync`
  - `channel_run_id`
  - `discovery_run_id`

这让前端后面做 Next BFF 时，不需要再猜分页总页数，也能拿到统一的生成时间和 resync run trace。

### 5. Alembic 现在有了清晰的 phase 边界

这次我做了两件重要事情：

1. 把 `20260415_220500_phase2_initial_schema.py` 改成快照式 migration
2. 新增 `20260419_120000_phase3_catalog_schema.py`

这样以后不管继续做 Phase 4 还是更后面的 schema 迭代，都不会反向污染旧版本。

---

## 五、测试和验证结果

这一轮我补了 `test/test_phase3_catalog.py`，主要验证：

- Spotify followed artists 同步后 artist 和 run 会被写入
- 单个 artist resync 后：
  - `youtube_channel` run 被写入
  - `youtube_rss` run 被写入
  - `artist_sync_runs` 被写入
  - `video_candidates` 被写入
  - artist 的 `sync_status` 和 `yt_channel_id` 被更新
- 批量 refresh active artists 后结果汇总正确
- `/v1/artists` 能返回分页后的 screen DTO
- `/v1/artists/{artist_id}/candidates` 能返回 candidate 列表
- `/v1/artists/{artist_id}/resync` 能正常返回运行结果
- Alembic 升级到 head 后，Phase 3 的表和列确实存在

另外这轮我不是只在系统 Python 下试，而是切到 [randyTranslation/.venv](/Users/randy/Documents/code/randyTranslation/randyTranslation/.venv) 重新执行了：

- `PYTHONPATH=/Users/randy/Documents/code/randyTranslation/randyTranslation python -m unittest discover -s test -p 'test_phase3_catalog.py'`

第一次重跑时，测试真实暴露出一个 contract 回归：

- `PaginationResponse` 已要求 `total_pages`
- 但 candidates endpoint 还没同步返回

补完后重新跑，结果是：

- `Ran 5 tests`
- `OK`

同时也回归跑过已有测试：

- `test_phase0_api_baseline.py`
- `test_phase2_postgres_foundation.py`

当前结果是：

- Phase 3 新测试通过
- 旧 Phase 0 / Phase 2 测试继续通过

说明这次不是“只把新功能做出来”，而是保持了已有兼容性。

---

## 六、这一轮完成后的定位

到这里，Phase 3 纯代码能完成的部分已经基本打齐了：

- Spotify followed artists 可以同步入 catalog
- 上游采集结果开始成为可持久化、可追踪的 catalog
- `artists` 页已经有第一版真实 BFF API
- 单个 artist resync 和批量 refresh 都已经形成可测试闭环
- 数据库 migration 边界被修正到可持续演进的状态

但这并不代表整个项目已经进入后续阶段。现在剩下没继续做的，不是因为代码上还能无限扩，而是因为已经触到需要真实外部依赖或前端联调验证的边界。

当前还没有做的，仍然属于下一批或下一阶段：

- 更丰富的 artist/candidate filter contract
- 更复杂的后台调度策略
- audit queue / pipeline / library 的 BFF
- manual review 工作流
- RabbitMQ 异步化和 outbox 真发布

另外真正进入 Phase 3 关闭前，还需要：

- 和前端一起验证 `artists` 页 DTO、empty state、partial failure、retry/resync 语义
- 在真实 Spotify / YouTube 环境下做一轮联通验证

---

## 七、总结

这次 Phase 3 的实现重点不是“多写几个接口”，而是把项目从 Phase 2 的事务基础层，真正推进到第一个面向前端 screen 的真实 product catalog。

现在项目已经从：

- “有基础表和 repository”

推进到了：

- “有 artist sync run”
- “有 candidate catalog”
- “有 `/v1/artists` BFF”
- “artists / candidates / resync contract 已为联调收口”
- “有清晰 phase migration”
- “有可复验测试闭环”

这一步做完之后，后面再继续推进：

- artists 页联调
- 更强的 source ingestion
- Phase 4 的 review / workflow / secure BFF

就会顺得多，也不会再被 Phase 2/3 的数据边界反复拖住。

---

## 八、基于 artistPageSmokeTest 的最新结论

在 `artistPageSmokeTest` 这一轮真实联调之后，Phase 3 的阶段判断需要更新。

原先这里把：

- `artists` 页联调
- 真实 Spotify / YouTube 环境验证

都视为下一步待完成项。现在这两件事已经各自完成了一轮本地 smoke test，而且结果不是停留在“接口能返回”，而是已经打通到了真实页面层。

这次新增确认的结果包括：

- 本地 Postgres schema 已升级到 `20260419_120000`
- Python API 已在 `127.0.0.1:8000` 真实运行
- `create_phase3_catalog_service()` 已确认可用
- `POST /internal/phase3/spotify/sync-followed-artists`
  - `200`
  - `synced_count=625`
  - `created_count=625`
  - `updated_count=0`
- `POST /internal/phase3/catalog/resync-active-artists?days=14&limit=20`
  - `200`
  - `requested=20`
  - `refreshed=20`
  - `failed=0`
- `GET /v1/artists?page=1&page_size=5&sort=last_synced_desc`
  - `200`
  - `total=625`
- Next BFF 真实链路已验证：
  - `GET /api/artists`
  - `GET /api/artists/[artistId]/candidates`
  - `POST /api/artists/[artistId]/resync?days=7`
- SSR 页面真实链路已验证：
  - `GET /artists`
  - `GET /artists/[artistId]`

这一轮真实验证里，还额外暴露并修复了两个关键页面层问题：

- SSR 下相对 `/api/...` URL 无法直接请求
- 后端 datetime 形式不统一，导致 artists BFF 时间字段在前端 schema 校验时失败

修完之后，Phase 3 已经不只是“有可联调的 API”，而是已经具备：

- 真实后端数据面
- 真实 Next BFF
- 真实 SSR 页面

三层联调验证闭环。

这也意味着当前真正剩下的核心问题已经转移，不再是“链路能不能通”，而是“结果质量够不够可信”。当前最明显的例子是：

- `artistId=6zfh3gWQe8WsPAA2XrUh2g`
- `name=A.M.`
- 当前 candidates 命中了 `Coast to Coast AM` 相关内容

因此，Phase 3 在当前阶段的更准确总结应该是：

上游 catalog、BFF、SSR 页面联调已经通过，接下来最该投入的是 channel lookup 精度、candidate relevance 以及 artist 名称歧义处理，而不是继续补链路接线。
