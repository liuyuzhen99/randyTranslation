# Phase 9 Review Report

## 结论

Phase 9 已经完成 cutover readiness 的工程骨架：系统现在可以生成 legacy vs target key parity、shadow traffic comparison、dual-write gate、schema freeze gate、rollback gate，并通过内部 API 或 CLI 汇总为 cutover readiness report。

本阶段没有直接切生产读源，也没有移除 legacy paths。原因是 roadmap 的 exit criteria 明确要求 7-day stability window、rollback no longer required、legacy paths removed；这些必须发生在真实运行窗口和前端四屏联测之后，不能在一次代码提交里伪造完成。

## 评审范围

- Phase 9 runtime controls。
- Cutover readiness service。
- Entity parity report。
- Shadow traffic validation。
- Internal readiness API。
- Cutover report CLI。
- Cutover runbook。
- Legacy decommission plan。

## 核心实现

### Cutover Controls

[api/config.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/api/config.py) 新增：

- `PHASE9_CUTOVER_READ_SOURCE`
- `PHASE9_SCHEMA_FREEZE_ENABLED`
- `PHASE9_ROLLBACK_ENABLED`
- `PHASE9_STABILITY_WINDOW_DAYS`
- `PHASE9_SHADOW_TRAFFIC_ENABLED`

[.env.example](/Users/randy/Documents/code/randyTranslation/randyTranslation/.env.example) 已同步补齐，配置契约测试已覆盖。

### Entity Parity

[application/services/phase9_cutover.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/application/services/phase9_cutover.py) 新增 `Phase9ReconciliationService`，用于比较 legacy 和 target snapshots：

- artists
- videos
- subtitles
- jobs
- reviews
- artifacts
- vectors

服务输出 count、missing keys、extra keys 和 consistency flag。它是通用 key-level parity 层，真实数据源只需要生成统一 JSON snapshot 即可接入。

### Shadow Traffic

`Phase9ShadowTrafficValidator` 同时执行 legacy read path 和 target read path，记录：

- latency
- success/failure
- normalized output match
- mismatch reason

这满足 Phase 9 roadmap 对 shadow traffic validation 的要求：切换读源前先比较用户可见输出，而不是只比较数据库计数。

### Cutover Gate

`Phase9CutoverReadinessService` 当前要求五个 gate 全部通过：

- `schema_freeze`
- `rollback_window`
- `dual_write`
- `entity_parity`
- `shadow_traffic`

只有全部通过时 `ready_for_cutover=true`。

### Internal API

[api/service.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/api/service.py) 新增：

```text
GET /internal/phase9/cutover-readiness
```

该 endpoint 会尝试读取 Phase 2 reconcile 结果作为 dual-write gate。若 reconcile 不可用或连接失败，它返回 blocked gate，而不是 500。这一点很重要：cutover readiness endpoint 本身应该稳定可用，依赖失败应体现在报告中。

### CLI 和 Runbook

新增：

- [scripts/phase9_cutover_report.py](/Users/randy/Documents/code/randyTranslation/randyTranslation/scripts/phase9_cutover_report.py)
- [docs/phase9-cutover-runbook.md](/Users/randy/Documents/code/randyTranslation/randyTranslation/docs/phase9-cutover-runbook.md)
- [docs/phase9-legacy-decommission-plan.md](/Users/randy/Documents/code/randyTranslation/randyTranslation/docs/phase9-legacy-decommission-plan.md)

CLI 输入 legacy snapshot、target snapshot、dual-write report、shadow report，输出完整 cutover readiness payload。命令退出码也可作为 release gate：全部通过时返回 0，否则返回 1。

## 验证结果

已完成：

- `test/test_phase9_cutover.py`：7 passed。
- `test/test_phase0_config_validation.py`：29 passed。
- `test/test_phase0_env_template_contract.py`：1 passed。
- `test/test_phase6_async_pipeline.py`：15 passed。
- Phase 9 py_compile：passed。
- Phase 9 cutover report CLI：synthetic parity/shadow evidence 下返回 0。

## 风险与限制

1. 真实 7-day stability window 尚未发生  
   这是运行期事实，不是代码可以直接完成的任务。

2. 真实 frontend four-screen cutover testing 尚未执行  
   Phase 9 roadmap 明确要求切换前后四屏端到端联测。本轮尚未启动前端，因为还没有真实 read-source switch。

3. 当前 snapshot source 是通用 JSON contract  
   这保证服务可测、可接入，但后续仍需要编写真正的 snapshot exporter，从 PostgreSQL、legacy SQLite/Chroma、Qdrant 生成正式 evidence。

4. Legacy paths 未移除  
   当前仍应保留 legacy paths，直到 rollback window 关闭。移除工作应放到 dedicated legacy deprecation PR。

## 后续建议

- 编写真正的 snapshot exporter，覆盖 artists、videos、subtitles、jobs、reviews、artifacts、vectors。
- 为 `/artists`、`/queue`、`/pipeline`、`/library` 建立 shadow traffic case。
- 在 staging 中设置 `PHASE9_SCHEMA_FREEZE_ENABLED=true` 后生成 cutover report。
- 执行读源切换前后四屏联测。
- 稳定窗口结束后执行 legacy decommission PR。

## 总体评价

Phase 9 当前已经具备“判断是否能切”的机制，而不是贸然执行 cutover。这个状态符合低风险迁移原则：先建立 evidence、gate 和 rollback，再做真实 read-source switch。剩余部分是运行期验证和生产切换，而不是基础工程缺口。
