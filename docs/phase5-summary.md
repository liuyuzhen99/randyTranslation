# Phase 5 总结：OSS Media Storage and Artifact Delivery

## 一、改造前的状态

Phase 4 结束后，后端已经具备 `audit queue / pipeline / library` 的 workflow BFF，并且候选 MV 可以经过 transcript review、taste audit、manual review、translation review、final asset approval 这些审核节点。

但媒体产物仍停留在 Phase 1 风格：

- `LocalFilesystemMediaStorage` 只负责创建临时目录、拼接临时文件路径和最终输出路径。
- pipeline 的最终结果是一个本地 mp4 路径，例如 `data/media/output/MV_<task_id>.mp4`。
- PostgreSQL 中没有独立 artifact 元数据表，无法记录对象 key、bucket、checksum、content type、生命周期状态和版本。
- library 页面只能表达“审核通过”，还不能表达“可消费资产是否 ready / missing”。

这意味着系统一旦进入更真实的生产环境，就会出现几个问题：

- API 返回的是机器本地路径，前端无法稳定预览或下载。
- 多实例部署时，本地文件不一定存在于处理请求的那台机器。
- 没有 artifact 元数据，后续异步 worker、重试、版本化和生命周期清理都缺少可追踪对象。
- 临时文件和最终文件边界不清晰，容易把 temp workspace 当成生产依赖。

## 二、为什么要这样改

Phase 5 的目标不是一次性完成完整云厂商 OSS/S3 接入，而是先把系统的领域契约改成对象存储语义：

- pipeline 可以继续在本地临时目录里完成下载、转写、字幕生成和渲染。
- 渲染完成后，只有上传后的对象 URI 才会进入 job result 和 artifact metadata。
- 本地 temp 文件在任务结束后仍然可以清理。
- PostgreSQL 成为 artifact 的索引和状态来源。
- 前端通过 artifact metadata 和 preview URL contract 工作，而不是依赖后端机器上的绝对路径。

这样做的好处是：当前开发环境不需要额外启动 MinIO/S3，也能先把对象存储的边界打出来。后面替换成真实 OSS adapter 时，pipeline 和 API 不需要再理解本地路径。

## 三、当前实现内容

### 1. 存储抽象升级

`domain/storage.py` 新增了 `StoredMediaObject`，并扩展了 `MediaStorageService`：

- `upload_artifact`
- `download_artifact`
- `delete_artifact`
- `create_presigned_url`

原有的 workspace 方法仍然保留：

- `prepare_task_workspace`
- `resolve_temp_file`
- `resolve_final_output`
- `cleanup_task_workspace`

这样 pipeline 仍可以用临时路径工作，但完成后的产物必须走 artifact upload。

### 2. 本地 OSS 适配器

`infrastructure/storage/local_media_storage.py` 从单纯路径 resolver 升级为本地 filesystem-backed object storage adapter。

当前对象 key 策略是：

```text
pipeline/<task_id>/<artifact_type>/v1/<filename>
```

对象 URI 形如：

```text
oss://randy-translation/pipeline/<task_id>/final_video/v1/final_video.mp4
```

本地开发时，真实文件会落在：

```text
<MEDIA_OUTPUT_ROOT>/<S3_BUCKET>/pipeline/<task_id>/<artifact_type>/v1/<filename>
```

这保留了本地可测试性，同时让上层只依赖 `oss://...` contract。

### 3. Artifact 领域模型和仓储

新增 `ArtifactRecord`，记录：

- artifact id
- owner type / owner id
- artifact type
- object URI / object key
- bucket / storage provider
- content type
- job id / candidate id
- size bytes
- sha256 checksum
- lifecycle status
- version
- metadata
- created / updated / expires timestamp

新增 `ArtifactRepository` 接口，并在 SQLAlchemy 中实现 `SQLAlchemyArtifactRepository`。

### 4. PostgreSQL / Alembic schema

新增 migration：

```text
alembic/versions/20260426_120000_phase5_artifacts_schema.py
```

新表：

```text
artifacts
```

关键约束和索引：

- `uq_artifacts_owner_type_version`
- `ix_artifacts_job_id`
- `ix_artifacts_owner`
- `ix_artifacts_object_uri`
- `ck_artifacts_version_positive`
- `ck_artifacts_size_non_negative`

这让 artifact 可以按 job 查询，也可以按 candidate/library owner 查询。

### 5. Pipeline 输出更新

`PipelineOrchestrator` 现在仍然先把最终视频渲染到 task temp workspace，但完成后会上传两个 artifact：

- `final_video`
- `subtitle_srt`

job completed result 不再写本地路径，而是写最终视频的对象 URI。

如果运行时配置了 SQLAlchemy artifact repository，还会写入 artifact metadata：

```text
job:<job_id>:final_video:v1
job:<job_id>:subtitle_srt:v1
```

如果 render 任务是从 candidate 发起的，artifact 会以 candidate 作为 owner，并同时保留 `job_id`：

```text
candidate:<candidate_id>:final_video:v1
candidate:<candidate_id>:subtitle_srt:v1
```

这样同一批 artifact 可以：

- 通过 `job_id` 追踪一次 render job 的产物
- 通过 `owner_type=candidate` 和 `owner_id=<candidate_id>` 被 library 页面直接消费

这满足了 Phase 5 的核心方向：最终可消费产物进入 durable object contract，本地 temp 文件仍可清理。

### 6. API / Library contract 更新

新增接口：

```text
GET /v1/artifacts/{artifact_id}/preview-url
```

返回：

- `artifact_id`
- `artifact_type`
- `object_uri`
- `url`
- `expires_in_seconds`

当前本地 adapter 返回的是本地开发用的 signed-style URL contract，后端提供 `/v1/artifacts/download` 作为本地 fallback；腾讯 COS adapter 会返回腾讯 COS 真实 pre-signed URL。

`/v1/library` 现在会带上：

- `artifact_status`
- `artifacts`

当 candidate 还没有关联 artifact 时，`artifact_status` 为 `missing`，前端可以据此展示缺失或处理中状态。

新增 candidate render 入口：

```text
POST /v1/candidates/{candidate_id}/render
```

该接口会校验 candidate 存在，创建 render job，并把 `candidate_id` 传给 pipeline。pipeline 完成后，`final_video` 和 `subtitle_srt` 会同时写入 COS 与 `artifacts` 表，library 可以按 candidate 直接找到最终资产。

## 四、为什么代码这样写

这一轮继续补上了腾讯 COS adapter，但仍然没有让 application 层依赖 COS SDK。原因是 Phase 5 的重点是让系统依赖“对象 URI + metadata + storage adapter contract”，而不是让 pipeline 或 BFF 直接知道具体云厂商。

因此当前实现采用了三层边界：

- Domain：只知道 `MediaStorageService` 和 `ArtifactRepository`。
- Infrastructure：本地 filesystem adapter 负责模拟 object storage 行为，腾讯 COS adapter 负责真实 durable object storage。
- Application/API：只消费 object URI、artifact metadata 和 preview URL。

这个结构让腾讯 COS 只出现在 infrastructure 和 runtime config 中，不需要重写 pipeline orchestrator、library BFF 或 artifact repository。

## 五、腾讯 COS 接入

新增文件：

```text
infrastructure/storage/cos_media_storage.py
```

新增运行时配置：

```text
MEDIA_STORAGE_BACKEND=cos
COS_SECRET_ID=
COS_SECRET_KEY=
COS_BUCKET=randy-translation-1250000000
COS_REGION=ap-shanghai
COS_SCHEME=https
COS_ENDPOINT=
```

当 `MEDIA_STORAGE_BACKEND=cos` 时，`api.config.create_media_storage` 会创建 `TencentCOSMediaStorage`。

腾讯 COS adapter 的行为：

- pipeline 仍然使用本地 temp workspace 做下载、转写、字幕和渲染。
- `upload_artifact` 使用 COS SDK 上传本地完成的 artifact。
- artifact URI 使用：

```text
cos://<bucket>/pipeline/<task_id>/<artifact_type>/v1/<filename>
```

- `create_presigned_url` 返回 COS SDK 生成的真实预签名 URL。
- `download_artifact` 和 `delete_artifact` 分别调用 COS 的 download/delete object 能力。

新增依赖：

```text
cos-python-sdk-v5==1.9.41
```

该版本是 PyPI 当前可见的较新版本，发布时间为 2026-01-06。

## 六、Phase 5 收口补充

在上一轮 COS / artifact metadata 基础上，Phase 5 后续又补齐了四块剩余能力。

### 1. Artifact lifecycle job

新增：

```text
application/services/artifact_lifecycle_service.py
POST /internal/phase5/artifacts/lifecycle
```

配置项：

```text
ARTIFACT_TEMP_RETENTION_DAYS=1
ARTIFACT_FINAL_RETENTION_DAYS=0
```

语义：

- temp workspace 按 temp retention 清理。
- final artifact 按 artifact 自身 `expires_at` 清理。
- `ARTIFACT_FINAL_RETENTION_DAYS=0` 表示最终 artifact 永久保留。
- 删除成功标记 `deleted`。
- 删除失败标记 `delete_failed`。

这让临时文件和最终产物的生命周期不再混用同一个清理策略。

### 2. Artifact detail / refresh / fallback BFF

新增或完善：

```text
GET /v1/artifacts/{artifact_id}
POST /v1/artifacts/{artifact_id}/refresh-url
GET /v1/artifacts/{artifact_id}/preview-url
GET /v1/artifacts/{artifact_id}/download
GET /v1/artifacts/download
```

当前 `/v1/artifacts/{artifact_id}` 会返回：

- artifact metadata
- 归一化后的 lifecycle status
- preview URL
- preview URL TTL
- fallback download URL

这让前端可以处理 ready / missing / expired / deleted / delete_failed，而不是只知道某个路径字符串。

### 3. 前端 Library / media preview 联调

联调范围在：

```text
vibeFrontTranslation/auditflow-app
```

前端补齐：

- `/api/library/[assetId]` detail BFF
- `/api/artifacts/[artifactId]/download` download fallback proxy
- Library list artifact status badge
- Library detail media preview
- Missing artifact fallback
- Artifact records list

浏览器验证：

```text
http://localhost:3000/library
http://localhost:3000/library/phase5-smoke-artist:cos-render-smoke
http://localhost:3000/library/4IVAbR2w4JJNJDDRFP3E83:eu7rZDy2M3g
```

验证结果：

- Library 列表显示 3 条 accepted asset。
- `Phase 5 COS Render Smoke` 显示 `Ready`。
- 6LACK 和 21 Savage 两条 accepted asset 显示 `Missing`。
- Ready 详情页可以打开，显示 video surface、artifact list、Download、Refresh、Source。
- Missing 详情页可以打开，显示缺失说明和空 artifact records。

联调中修复：

- Server Component 调 BFF 不能使用相对 URL，详情页改为使用 request origin。
- asset id 含冒号时发生二次 URL encode，详情页和 BFF 增加 `decodeURIComponent` 归一化。

### 4. 真实 producer backend 恢复

新增：

```text
core/hipHopProducer.py
```

`HipHopAutoProject` 现在提供真实 producer backend：

- yt-dlp 下载 YouTube 视频
- ffmpeg 抽取音频
- faster-whisper 转写
- DeepSeek/OpenAI-compatible API 翻译
- 生成 bilingual SRT
- ffmpeg 输出 final MP4
- 上传 final video 和 subtitle SRT artifact

真实 E2E 已跑通：

```text
job_id: e2e-7b8afd5c70
song: J. Cole MIDDLE CHILD official music video
storage: tencent-cos
status: completed
progress: ✨ 制作完成！
```

产物：

```text
cos://randytranslation-1426182286/pipeline/e2e-7b8afd5c70/final_video/v1/final_video.mp4
cos://randytranslation-1426182286/pipeline/e2e-7b8afd5c70/subtitle_srt/v1/bilingual.srt
```

artifact metadata：

```text
job:e2e-7b8afd5c70:final_video:v1
size: 28516231
status: ready

job:e2e-7b8afd5c70:subtitle_srt:v1
size: 6529
status: ready
```

真实 E2E 中发现并修复：

- ffmpeg `subtitles` filter 的 `force_style` 参数需要正确转义。
- 当前本机 ffmpeg 没有 `subtitles` filter/libass，因此 producer 增加 fallback：优先硬烧字幕，若不支持则 mux 成 MP4 内嵌字幕轨。

## 七、测试结果

后端已跑通过：

```text
.venv/bin/python -m unittest discover -s test -p 'test_phase5_cos_storage.py'
.venv/bin/python -m unittest discover -s test -p 'test_phase1_layered_architecture.py'
.venv/bin/python -m unittest discover -s test -p 'test_phase4_workflow.py'
.venv/bin/python -m py_compile domain/storage.py domain/entities.py domain/repositories.py infrastructure/storage/local_media_storage.py infrastructure/storage/cos_media_storage.py infrastructure/persistence/sqlalchemy_repositories.py application/services/artifact_lifecycle_service.py application/services/pipeline_orchestrator.py api/config.py api/service.py core/hipHopProducer.py
```

前端已跑通过：

```text
npm run typecheck
npm test -- --run src/components/features/library/library-asset-detail-client.test.tsx src/components/features/library/library-dashboard-client.test.tsx
```

测试过程中有一个已知噪声：

- cleanup failure 用例会故意打印一次 cleanup 异常日志，用于验证任务不会卡死。

## 八、当前限制和后续建议

Phase 5 主链路已经完成，但仍有几个明确限制：

- 真实 E2E 这次创建的是 `job` owner artifact，不是 `candidate` owner artifact，因此不会自动出现在 Library accepted asset 列表中。
- Library 要展示真实 E2E 产物，需要从 accepted candidate 发起 render，或补一个把 job artifact 关联到 candidate 的运维动作。
- 当前翻译接口要求返回 JSON array；真实运行中出现过非 JSON 响应，producer 会 fallback 成英文字幕。后续建议增强 JSON repair 或 structured output。
- 当前本机 ffmpeg 不支持硬烧字幕，只能 fallback 成 MP4 内嵌字幕轨。若要硬烧字幕，需要安装带 libass/subtitles filter 的 ffmpeg。
- yt-dlp 提示缺少 JS runtime，当前下载成功，但建议后续补 deno/node runtime，降低 YouTube extractor 变化带来的风险。

## 九、最终结论

Phase 5 已完成：

- 本地 object-storage style adapter
- 腾讯 COS adapter
- artifact metadata schema
- pipeline artifact upload
- artifact lifecycle cleanup
- artifact detail / refresh / download BFF
- Library artifact ready / missing / expired 状态 contract
- 前端 Library/media preview 联调
- 真实 producer backend
- 真实 YouTube / Whisper / ffmpeg / COS 端到端生产验证

可以进入 Phase 6：异步 worker、stage queue、可靠重试和 producer/consumer 化。
