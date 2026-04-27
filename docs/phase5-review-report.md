**Phase 5 详细 Review 报告**

Phase 5 的目标，是把媒体产物从“后端机器上的临时/本地路径”，推进到“可被 API、Library、前端 preview 稳定消费的 durable artifact contract”。这一阶段最终完成的范围包括：对象存储抽象、COS adapter、artifact metadata、生命周期清理、artifact BFF、Library artifact 状态联调、以及真实 producer backend 恢复。

这次 review 的结论是：Phase 5 已经完成主目标，并且真实跑通了一次 YouTube 下载、Whisper 转写、SRT 生成、ffmpeg 输出、COS 上传、artifact metadata 登记的端到端生产链路。

**1. Artifact 和 storage contract 已经从本地路径升级为对象存储语义**

核心文件：

- [domain/storage.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/domain/storage.py)
- [infrastructure/storage/local_media_storage.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/infrastructure/storage/local_media_storage.py)
- [infrastructure/storage/cos_media_storage.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/infrastructure/storage/cos_media_storage.py)

当前 `MediaStorageService` 不再只负责拼临时路径，而是明确提供：

- `upload_artifact`
- `download_artifact`
- `delete_artifact`
- `create_presigned_url`
- `cleanup_task_workspace`
- `cleanup_stale_task_workspaces`

这个边界是正确的：pipeline 仍然可以在本地 workspace 里完成下载、转写和渲染，但完成后的产物必须上传为 object URI。上层 API 和前端不再需要知道文件落在哪台机器上。

本地 adapter 使用 `oss://...` 语义模拟对象存储，COS adapter 使用 `cos://...` 指向真实腾讯 COS 对象。两者对 application 层保持同一 contract。

**2. Artifact metadata 已经落库，并支持 job owner 和 candidate owner 两种查询模式**

核心文件：

- [domain/entities.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/domain/entities.py)
- [domain/repositories.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/domain/repositories.py)
- [infrastructure/persistence/sqlalchemy_models.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/infrastructure/persistence/sqlalchemy_models.py)
- [infrastructure/persistence/sqlalchemy_repositories.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/infrastructure/persistence/sqlalchemy_repositories.py)
- [alembic/versions/20260426_120000_phase5_artifacts_schema.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/alembic/versions/20260426_120000_phase5_artifacts_schema.py)

`ArtifactRecord` 记录了 artifact 的完整可追踪信息：

- owner type / owner id
- artifact type
- object URI / object key
- bucket / storage provider
- content type
- job id / candidate id
- size bytes
- checksum sha256
- lifecycle status
- version
- metadata
- created / updated / expires timestamp

这里最重要的设计点是 owner 语义：

- 普通 task render 会生成 `job:<job_id>:final_video:v1`
- candidate render 会生成 `candidate:<candidate_id>:final_video:v1`

这让 pipeline 可以按 job 追踪一次执行结果，也让 Library 可以按 candidate 直接消费最终资产。

**3. Pipeline 已经上传 final_video 和 subtitle_srt，而不是返回本地路径**

核心文件：

- [application/services/pipeline_orchestrator.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/application/services/pipeline_orchestrator.py)

`PipelineOrchestrator` 的成功路径现在是：

1. prepare task workspace
2. producer 下载视频
3. producer 抽音并转写
4. producer 生成 bilingual SRT
5. producer 生成 final video
6. upload `final_video`
7. upload `subtitle_srt`
8. 写 artifact metadata
9. job result 写最终视频 object URI
10. 清理 task workspace

这是 Phase 5 最关键的行为变化：`Job.result` 不再是本地 mp4 path，而是 durable object URI。

**4. Lifecycle job 已经补齐，并区分 temp retention 和 final artifact retention**

核心文件：

- [application/services/artifact_lifecycle_service.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/application/services/artifact_lifecycle_service.py)
- [api/service.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/api/service.py)

新增内部入口：

```text
POST /internal/phase5/artifacts/lifecycle
```

配置项：

```text
ARTIFACT_TEMP_RETENTION_DAYS=1
ARTIFACT_FINAL_RETENTION_DAYS=0
```

当前语义：

- temp workspace 使用 `ARTIFACT_TEMP_RETENTION_DAYS` 清理 stale task directories
- final artifact 使用 `expires_at` 判断是否进入删除流程
- `ARTIFACT_FINAL_RETENTION_DAYS=0` 表示最终 artifact 永久保留
- 删除成功后 artifact 标记为 `deleted`
- 删除失败后 artifact 标记为 `delete_failed`

这个设计避免了 Phase 5 最容易混淆的问题：临时工作目录可以积极清理，但最终产物不能被同一套 retention 误删。

**5. Artifact BFF 已经补齐详情、URL refresh 和下载 fallback**

核心文件：

- [api/service.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/api/service.py)

新增或完善的接口：

```text
GET /v1/artifacts/{artifact_id}
POST /v1/artifacts/{artifact_id}/refresh-url
GET /v1/artifacts/{artifact_id}/preview-url
GET /v1/artifacts/{artifact_id}/download
GET /v1/artifacts/download
```

当前行为：

- detail 返回 artifact metadata、lifecycle status、preview URL、fallback download URL
- refresh-url 用于 URL 过期后的重新签发
- 本地 adapter 返回 `/v1/artifacts/download?...` 形式的本地 fallback URL
- COS adapter 返回真实 COS pre-signed URL
- 下载失败时可以走后端 artifact id download fallback

这让前端不需要自己理解 `oss://` 或 `cos://`，只消费 BFF 给出的 preview/download contract。

**6. Library BFF 和前端 preview 已经联调 ready / missing 状态**

后端核心文件：

- [application/services/phase4_workflow_service.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/application/services/phase4_workflow_service.py)
- [api/service.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/api/service.py)

前端核心文件：

- [src/app/api/library/route.ts](/Users/randy/Documents/code/randyTranslation/vibeFrontTranslation/auditflow-app/src/app/api/library/route.ts)
- [src/app/api/library/[assetId]/route.ts](/Users/randy/Documents/code/randyTranslation/vibeFrontTranslation/auditflow-app/src/app/api/library/[assetId]/route.ts)
- [src/app/api/artifacts/[artifactId]/download/route.ts](/Users/randy/Documents/code/randyTranslation/vibeFrontTranslation/auditflow-app/src/app/api/artifacts/[artifactId]/download/route.ts)
- [src/components/features/library/library-dashboard-client.tsx](/Users/randy/Documents/code/randyTranslation/vibeFrontTranslation/auditflow-app/src/components/features/library/library-dashboard-client.tsx)
- [src/components/features/library/library-asset-detail-client.tsx](/Users/randy/Documents/code/randyTranslation/vibeFrontTranslation/auditflow-app/src/components/features/library/library-asset-detail-client.tsx)

联调结果：

- `/v1/library` 返回 3 条 accepted asset
- 第一条 `Phase 5 COS Render Smoke` 为 `artifact_status=ready`
- 另外两条 accepted asset 为 `artifact_status=missing`
- 前端 Library 列表正确显示 `Ready / Missing`
- Ready 详情页可以打开，显示 artifact 列表、Download、Refresh、Source
- Missing 详情页可以打开，显示缺失 fallback 和 “No artifact records”

联调时修复了两个前端问题：

- Server Component 调 BFF 时不能用相对 URL，详情页已改成使用 request origin
- asset id 中包含冒号时被二次 encode，详情页和 BFF 已做 `decodeURIComponent` 归一化

**7. 真实 producer backend 已经恢复，并跑通真实 E2E**

核心文件：

- [core/hipHopProducer.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/core/hipHopProducer.py)
- [infrastructure/pipeline/legacy_producer_adapter.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/infrastructure/pipeline/legacy_producer_adapter.py)

`core.hipHopProducer.HipHopAutoProject` 已经恢复，当前真实 producer 路径为：

- yt-dlp 搜索并下载 YouTube 视频
- ffmpeg 抽取 16k mono 音频
- faster-whisper 转写歌词片段
- DeepSeek/OpenAI-compatible API 翻译生成 bilingual SRT
- ffmpeg 输出 final mp4
- 上传 final video 和 SRT 到 COS

真实 E2E 运行记录：

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

E2E 中发现并修复了两个 producer 问题：

- ffmpeg `subtitles` filter 的 `force_style` 参数需要转义逗号，否则 filter 解析失败
- 当前本机 ffmpeg 没有编译 `subtitles` filter/libass，因此 producer 增加 fallback：优先硬烧字幕，若不支持则把 SRT mux 成 MP4 字幕轨

当前真实产出的 final video 是内嵌字幕轨 MP4；如果要得到硬烧字幕版，需要安装带 libass/subtitles filter 的 ffmpeg。

**8. 当前仍需注意的限制**

Phase 5 主链路已经完成，但仍有几个产品/工程限制需要明确：

- 真实 E2E 这次创建的是 `job` owner artifact，不是 `candidate` owner artifact，所以不会自动出现在 Library accepted asset 列表中
- Library 要展示真实 E2E 产物，需要从某个 accepted candidate 发起 render，或补一个把 job artifact 关联到 candidate 的运维动作
- 当前翻译接口要求模型返回 JSON array；真实运行中出现过非 JSON 响应，producer 会 fallback 成英文字幕，后续可以增强 JSON repair / structured output
- 本机 ffmpeg 不支持硬烧字幕时，只能产出内嵌字幕轨；这对部分播放器可用，但视觉上不是 burned-in subtitles
- yt-dlp 提示缺少 JS runtime，当前下载仍成功，但后续 YouTube extractor 可能更依赖 JS runtime，建议补 deno/node 运行时配置

**9. 验证结果**

后端已验证：

```text
.venv/bin/python -m unittest discover -s test -p 'test_phase5_cos_storage.py'
.venv/bin/python -m unittest discover -s test -p 'test_phase1_layered_architecture.py'
.venv/bin/python -m unittest discover -s test -p 'test_phase4_workflow.py'
.venv/bin/python -m py_compile domain/storage.py domain/entities.py domain/repositories.py infrastructure/storage/local_media_storage.py infrastructure/storage/cos_media_storage.py infrastructure/persistence/sqlalchemy_repositories.py application/services/artifact_lifecycle_service.py application/services/pipeline_orchestrator.py api/config.py api/service.py core/hipHopProducer.py
```

前端已验证：

```text
npm run typecheck
npm test -- --run src/components/features/library/library-asset-detail-client.test.tsx src/components/features/library/library-dashboard-client.test.tsx
```

浏览器联调已验证：

```text
http://localhost:3000/library
http://localhost:3000/library/phase5-smoke-artist:cos-render-smoke
http://localhost:3000/library/4IVAbR2w4JJNJDDRFP3E83:eu7rZDy2M3g
```

结论：Phase 5 的 artifact delivery、lifecycle、BFF、frontend preview state 和真实 producer backend 均已完成到可继续推进 Phase 6 的状态。
