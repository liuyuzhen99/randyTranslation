**Phase 3 详细 Review 报告**

Phase 3 的目标，是把项目从 Phase 2 的“PostgreSQL foundation + transaction boundary”推进到“可靠的 source ingestion 和 candidate catalog”。这一阶段我做的事，核心可以归成 6 大块：领域模型扩展、catalog 持久化、应用服务产品化、BFF API、Alembic 边界修正、测试和收口判断。

**1. 先做了领域层收口：把 Phase 3 的状态和对象定义清楚**
这一步主要改的是：

- [domain/enums.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/domain/enums.py)
- [domain/entities.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/domain/entities.py)
- [domain/repositories.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/domain/repositories.py)

我先补了：

- `SyncStatus`
- `CandidateStatus`

然后扩展了：

- `Artist.sync_status`
- `Artist.last_sync_started_at`
- `Artist.last_sync_completed_at`
- `Artist.last_sync_error`
- `Artist.last_channel_resolved_at`
- `Artist.last_discovery_at`

再新增了：

- `ArtistSyncRun`
- `VideoCandidate`

为什么这么做：

- 如果 Phase 3 还继续靠零散字符串和临时字段来表达同步状态，后面 source health、retry metadata、partial failure 都会很难稳定
- `artists` 页真正要消费的不是“artist 这张表长什么样”，而是“这个艺人最近有没有同步成功、失败在哪、最近发现了什么 candidate”
- 这些语义如果不先进入领域模型，后面的 repository 和 BFF 都会变成拼 JSON

同时我还补了 repository 契约：

- `ArtistRepository.list_all()`
- `ArtistSyncRunRepository`
- `CandidateRepository`

这样做的原因是：

- Phase 3 不再只是点查单条记录，而是需要分页列 artist、列 candidate、列某个 artist 的多次运行记录
- 这些能力如果只藏在 SQLAlchemy 层，不进入 repository contract，application 层就会越来越依赖具体存储实现

**2. 然后做了 SQLAlchemy 模型和 repository，让 catalog 真正能落库**
这部分主要改的是：

- [infrastructure/persistence/sqlalchemy_models.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/infrastructure/persistence/sqlalchemy_models.py)
- [infrastructure/persistence/sqlalchemy_repositories.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/infrastructure/persistence/sqlalchemy_repositories.py)
- [infrastructure/persistence/sqlite_repositories.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/infrastructure/persistence/sqlite_repositories.py)

我新增了：

- `artist_sync_runs`
- `video_candidates`

并扩展了 `artists` 表，让它能直接承载：

- 当前同步状态
- 最近同步时间
- 最近失败原因
- 最近 channel resolve 时间
- 最近 discovery 时间

在 `video_candidates` 上，我做了一个关键约束：

- `UniqueConstraint(spotify_id, video_id)`

为什么这么做：

- Phase 3 最容易出问题的地方就是 repeated scan 下不断制造重复 candidate
- 用 artist + video 的业务键做唯一约束，比只靠应用层记忆更可靠
- 这样 RSS repeated discovery 至少在数据库层不会无限插重复行

在 repository 层，我新增了：

- `SQLAlchemyArtistSyncRunRepository`
- `SQLAlchemyCandidateRepository`

这里最关键的一点不是“多了几个 CRUD”，而是：

- `CandidateRepository.upsert()` 会先按 `candidate_id` 查
- 如果没有，再按 `(spotify_id, video_id)` 的业务键查
- 命中后更新，而不是盲目插入

这样做的价值是：

- repeated scan 可以落成幂等 upsert
- candidate catalog 的行为更接近产品语义，而不是一次性采集脚本

另外我也顺手补了 `SQLiteArtistRepository.list_all()`，原因不是要继续依赖 SQLite，而是为了保持 repository contract 的结构一致，不让 Phase 3 的新接口只在一个 backend 上成立。

**3. 接着做了 application 层产品化：把 Spotify / channel / RSS 变成真正服务**
这部分主要改的是：

- [application/services/phase3_catalog_service.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/application/services/phase3_catalog_service.py)
- [services/getSpotifyFollowingList.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/services/getSpotifyFollowingList.py)
- [api/config.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/api/config.py)

我没有把 Phase 3 做成一个大而乱的 service，而是按职责拆成：

- `ArtistSyncService`
- `ChannelDiscoveryService`
- `VideoDiscoveryService`
- `CandidateCatalogService`

这里最重要的决策是：**抓取逻辑通过 provider 注入，而不是在 service 里直接硬编码外部依赖。**

现在默认 provider 包括：

- Spotify followed artists lookup
- YouTube channel lookup
- YouTube RSS candidate lookup

为什么这样做：

- Phase 3 的代码可以把边界做好，但不能假装外部系统一定已经可用
- provider 注入后，测试可以完全用 fake provider 跑通
- 以后如果要把某一段切到 worker、定时任务、RabbitMQ，service 层不需要重写

然后我把旧的 Spotify 同步脚本做了一次重要纠偏：

- `getSpotifyFollowingList.py` 不再在模块导入时就初始化 Spotify OAuth client
- 改成 `create_spotify_client()` 惰性初始化

为什么这样改：

- 导入模块时直接触发外部认证，会让运行时边界非常脆弱
- 这类副作用会污染测试，也会让 app import 本身变得不稳定
- 这不符合你要求的“不要依据猜想出来的外部依赖条件写代码”

在 `CandidateCatalogService` 里，我补了几条关键闭环：

- `sync_followed_artists()`
- `resync_artist()`
- `refresh_active_artists()`

其中 `resync_artist()` 不是只记一个笼统 run，而是拆成两段真实 source run：

- `youtube_channel`
- `youtube_rss`

为什么这么做：

- roadmap 里写的是 Spotify sync、YouTube channel discovery、RSS scanning 三段来源能力
- 如果只记一个总 run，source health 会很模糊
- 拆成 source-kind 粒度后，前端和后端都能知道到底是 channel resolve 失败，还是 RSS discovery 失败

所以现在 artist 的 source health 已经可以表达：

- 哪一段 source 最近一次状态是什么
- retry_count 是多少
- failure_reason 是什么
- started/completed 时间是什么
- 发现了多少结果

**4. 再往前做了 `artists` BFF：把 screen contract 真正接出来**
这部分主要改的是：

- [api/service.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/api/service.py)

我接出了第一版 `/v1` artists contract：

- `GET /v1/artists`
- `GET /v1/artists/{artist_id}/candidates`
- `POST /v1/artists/{artist_id}/resync`

同时补了纯后端内部触发入口：

- `POST /internal/phase3/spotify/sync-followed-artists`
- `POST /internal/phase3/catalog/resync-active-artists`

为什么要补这两个 internal endpoint：

- 让 Phase 3 的 source ingestion 可以通过 app 本身被触发和验证
- 不需要手写临时脚本直接去调用 service
- 但又不会提前把这些运维入口包装成面向前端的正式 BFF contract

在 `/v1/artists` 的 DTO 上，我没有直接返回 entity，而是整理成 screen-oriented 结构，包括：

- `pagination`
- `meta`
- `latest_candidate`
- `latest_run`
- `candidate_count`
- `sync_status`
- `source_health`
- `retry_metadata`
- `partial_failure`
- `empty_state`

为什么这样做：

- `artists` 页面要消费的是“屏幕语义”，不是仓储语义
- 如果现在就把 DTO 收口好，后面前端联调才不会反复逼着后端暴露内部表结构
- `partial_failure` 和 `retry_metadata` 这类字段，越早在 contract 里明确，越不容易以后返工

后面为了配合即将开始的 `artists` 页联调，我又把 API contract 往前收了一步：

- `GET /v1/artists`
  - `pagination.total_pages`
  - `meta.generated_at`
- `GET /v1/artists/{artist_id}/candidates`
  - `pagination.total_pages`
- `POST /v1/artists/{artist_id}/resync`
  - `channel_run_id`
  - `discovery_run_id`

这一步看起来小，但很关键：

- 前端分页组件本来就依赖 `totalPages`
- server-render 和 BFF 适配层需要明确 `generatedAt`
- resync 返回 source-level run id 后，前端后面要做 toast、debug、候选刷新追踪都更顺
- artists 和 candidates 共用同一分页 response model 时，contract 必须一起补齐，不能只修一个 endpoint

**5. 然后我修了 Alembic 边界：避免 Phase 3 反向污染 Phase 2**
这部分主要改的是：

- [alembic/versions/20260415_220500_phase2_initial_schema.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/alembic/versions/20260415_220500_phase2_initial_schema.py)
- [alembic/versions/20260419_120000_phase3_catalog_schema.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/alembic/versions/20260419_120000_phase3_catalog_schema.py)

原来的 Phase 2 首个 migration 直接引用运行时 `Base.metadata`。这在只有一个 revision 的时候看起来没问题，但一旦进入 Phase 3，新模型会把旧 migration 一起污染。

所以我做了两件事：

1. 把 Phase 2 migration 改成真正快照式定义
2. 单独新增 Phase 3 migration

为什么这样改：

- 历史 migration 必须只代表历史那个阶段
- 如果 Phase 2 migration 在 Phase 3 还能偷偷创建新表，那 revision 边界就失效了
- 后面不管是回滚、排查、还是对比环境差异，都会越来越难

这是这次 Phase 3 里一个很关键但也很容易被忽略的工程修正。

**6. 测试是怎么补的，为什么这么补**
这部分主要改的是：

- [test/test_phase3_catalog.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/test/test_phase3_catalog.py)

我补的测试，不是只测一个 happy path，而是围绕 Phase 3 最容易出错的点来测：

- Spotify followed artists 同步后 artist 和 run 是否被写入
- 单个 artist resync 后：
  - `youtube_channel` run 是否落库
  - `youtube_rss` run 是否落库
  - candidate 是否落库
  - artist 状态是否更新
- batch refresh active artists 的汇总结果是否正确
- `/v1/artists` 是否返回分页 contract
- `/v1/artists/{artist_id}/candidates` 是否返回列表 contract
- `/v1/artists/{artist_id}/resync` 是否返回运行结果
- unknown artist 是否正确 404
- Alembic upgrade 到 head 后，Phase 3 的表和字段是否存在

这轮补 contract 之后，我还专门在 [randyTranslation/.venv](/Users/randy/Documents/code/randyTranslation/randyTranslation/.venv) 里重新跑了：

- `PYTHONPATH=/Users/randy/Documents/code/randyTranslation/randyTranslation python -m unittest discover -s test -p 'test_phase3_catalog.py'`

这里有一个真实回归也被测出来了：

- `PaginationResponse` 扩成必填 `total_pages` 后
- `/v1/artists/{artist_id}/candidates` 一开始没同步返回这个字段
- FastAPI response validation 直接报错

我随后把 candidates endpoint 一并补齐，最后这一组测试通过。

同时我还回归跑了已有测试：

- `test_phase0_api_baseline.py`
- `test_phase2_postgres_foundation.py`

为什么这样测：

- Phase 3 最大风险不是“代码写不出来”，而是“看起来闭环了，实际上 source run、dedupe、DTO contract 其中一段没站稳”
- 如果不把 source-kind run、BFF contract、migration head 都覆盖掉，后面联调时会集中暴雷

**7. 我为什么说 Phase 3 纯代码部分到这里基本完成**
因为 roadmap 里属于 Phase 3 的核心内容，现在已经具备：

- Spotify sync
- YouTube channel discovery
- RSS candidate discovery
- sync run 持久化
- candidate dedupe
- source traceability
- `/v1/artists` BFF 首版
- BFF contract 已从“能返回数据”收口到“分页 / meta / run id 都明确”
- DTO / pagination contract 首版
- migration 和测试

剩下没继续做的，已经明显不适合继续靠纯代码想象推进，而是需要真实验证：

- 和前端一起确认 `artists` 页 DTO / empty state / partial failure / retry 语义
- 在真实 Spotify / YouTube 环境下验证真实联通

再往后的：

- audit queue
- pipeline
- library
- manual review
- RabbitMQ / worker

这些都已经是 Phase 4 或更后面的内容，不应该继续混在 Phase 3 里做。

**8. 这一阶段最关键的几次纠偏**
如果从 review 角度看，这一阶段最重要的不是“多写了几个 endpoint”，而是几次关键纠偏：

- 我把 Spotify client 改成惰性初始化
  原因：不能在模块导入阶段就假设外部认证和浏览器环境已经可用
- 我把 resync run 拆成 `youtube_channel` 和 `youtube_rss`
  原因：source health 需要真实来源粒度，不能只有一个模糊总 run
- 我把 Alembic Phase 2 migration 改成快照式
  原因：不能让 Phase 3 的 schema 反向污染历史 revision
- 我没有继续凭空扩 `audit queue` / `pipeline` / `library`
  原因：那已经超出 Phase 3 范围，而且会在没有前端和真实外部验证前越写越虚

这些调整，本质上都是为了让 Phase 3 更真实、更稳、更符合你要求的边界。

**9. 当前最终结论**
如果用一句话概括 Phase 3：

Phase 3 已经从“source ingestion 的脚本集合”推进到“有持久化 run、有 candidate catalog、有 `artists` BFF contract 的真实上游产品化能力”。

包括：

- 代码完成
- BFF 首版完成
- migration 完成
- 测试完成
- 文档完成

接下来最合理的下一步，不是继续盲写更多阶段，而是：

1. 跟前端联调 `artists` 页面
2. 跑一轮真实 Spotify / YouTube 联通验证
3. 再进入 Phase 4 的 workflow / review / secure BFF

**10. 基于 artistPageSmokeTest 的后续验证结果**
上面这个结论，在 `artistPageSmokeTest` 这一轮真实 smoke test 之后，需要再往前推进一步。

因为原来列为“下一步”的两件事，现在都已经完成了第一轮真实验证：

- `artists` 页面联调已完成
- 真实 Spotify / YouTube / RSS 数据面验证已完成

这次真实联调里，已经实际确认：

- 本地 Postgres schema 已升级到 `20260419_120000`
- Python API 已在 `127.0.0.1:8000` 启动
- `create_phase3_catalog_service()` 不是 `None`
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
  - 至少一个 artist 已存在真实 candidates
- `GET /api/artists`：通过
- `GET /api/artists/[artistId]/candidates`：通过
- `POST /api/artists/[artistId]/resync?days=7`：通过
- `GET /artists`：通过，并已 SSR 渲染真实 artists 列表
- `GET /artists/[artistId]`：通过，并已 SSR 渲染真实 candidates 详情

这次 smoke test 也不是完全没有暴露问题。它实际找出了两个页面层阻塞，并且都已经修复：

- SSR 请求相对 `/api/...` URL 失败
- artists BFF 未统一规范后端 datetime，导致前端 schema parse 失败

修完后再看 Phase 3 的阶段判断，就不应该再停留在“API 已准备好，等待联调”。

更准确的说法应该是：

Phase 3 已经完成了真实后端数据面、Next BFF、SSR 页面三层联调验证。

**11. 当前剩余风险已经从链路问题转向结果质量问题**
到这个阶段，最值得关注的已经不是“artists 页面能不能连上后端”，而是“连上后端之后，结果是不是可信”。

这次真实联调里最典型的例子是：

- `artistId=6zfh3gWQe8WsPAA2XrUh2g`
- `name=A.M.`
- 当前返回的 candidates 命中了 `Coast to Coast AM`

这说明当前剩余重点是：

- channel lookup 精度
- candidate relevance 规则
- artist 名称歧义处理

所以从 review 角度看，Phase 3 到这里的结论应该更新为：

- 链路接通工作已基本完成
- 真实 smoke test 已完成
- 后续优先级应转向 discovery 质量校准，而不是继续补 artists 基础接线
