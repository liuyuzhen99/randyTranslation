from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from api.dependencies import (
    get_artifact_lifecycle_service,
    get_outbox_dispatcher,
    get_phase3_catalog_service,
    get_phase6_async_pipeline_services,
    get_reconcile_service,
    get_runtime_settings,
    get_session_factory,
)

router = APIRouter(tags=["internal"])


@router.get("/healthz")
async def healthz(request: Request):
    from application.services.phase7_health import Phase7HealthService

    result = Phase7HealthService(
        session_factory=request.app.state.session_factory,
        media_storage=request.app.state.media_storage,
    ).liveness()
    return {"status": result.status, "checks": result.checks}


@router.get("/readyz")
async def readyz(request: Request):
    from application.services.phase7_health import Phase7HealthService
    from domain.queue_topology import PipelineQueueTopology
    from infrastructure.messaging.rabbitmq_observability import (
        RabbitMQQueueMetricsCollector,
        RabbitMQQueueMetricsConfig,
    )

    topology = PipelineQueueTopology()
    rabbitmq_url = os.environ.get("RABBITMQ_URL", "").strip()
    queue_probe = (
        RabbitMQQueueMetricsCollector(RabbitMQQueueMetricsConfig(url=rabbitmq_url, topology=topology))
        if rabbitmq_url
        else None
    )
    result = Phase7HealthService(
        session_factory=request.app.state.session_factory,
        media_storage=request.app.state.media_storage,
        queue_probe=queue_probe,
        qdrant_url=os.environ.get("QDRANT_URL", "").strip(),
        qdrant_api_key=os.environ.get("QDRANT_API_KEY", "").strip(),
    ).readiness()
    return Response(
        status_code=200 if result.status == "ok" else 503,
        content=__import__("json").dumps({"status": result.status, "checks": result.checks}),
        media_type="application/json",
    )


@router.get("/internal/phase2/reconcile")
async def phase2_reconcile(
    reconcile_service=Depends(get_reconcile_service),
    runtime_settings=Depends(get_runtime_settings),
):
    if reconcile_service is None:
        raise HTTPException(status_code=503, detail="Phase 2 reconcile service is not enabled")
    report_path = runtime_settings.phase2_reconcile_report_path
    if report_path:
        report = reconcile_service.write_report(report_path)
    else:
        report = reconcile_service.generate_report()
    return {"report": report.to_dict(), "report_path": report_path}


@router.get("/internal/phase9/cutover-readiness")
async def phase9_cutover_readiness(
    reconcile_service=Depends(get_reconcile_service),
    runtime_settings=Depends(get_runtime_settings),
):
    from application.services.phase9_cutover import Phase9CutoverReadinessService

    dual_write_report = None
    if reconcile_service is not None:
        try:
            dual_write_report = reconcile_service.generate_report().to_dict()
        except Exception as exc:
            dual_write_report = {
                "is_within_threshold": False,
                "is_consistent": False,
                "error": str(exc),
            }

    report = Phase9CutoverReadinessService(
        read_source=runtime_settings.phase9_cutover_read_source,
        schema_freeze_enabled=runtime_settings.phase9_schema_freeze_enabled,
        rollback_enabled=runtime_settings.phase9_rollback_enabled,
        stability_window_days=runtime_settings.phase9_stability_window_days,
    ).evaluate(dual_write_report=dual_write_report)
    return {"report": report.to_dict()}


@router.post("/internal/phase2/outbox/dispatch")
async def phase2_outbox_dispatch(outbox_dispatcher=Depends(get_outbox_dispatcher)):
    if outbox_dispatcher is None:
        raise HTTPException(status_code=503, detail="Phase 2 outbox dispatcher is not enabled")
    return outbox_dispatcher.dispatch_pending()


@router.get("/internal/phase6/queue-topology")
async def phase6_queue_topology():
    from domain.queue_topology import PipelineQueueTopology

    topology = PipelineQueueTopology()
    return {
        "exchange": topology.exchange,
        "bindings": [
            {
                "queue_name": binding.queue_name,
                "routing_key": binding.routing_key,
                "stage": binding.stage.value if binding.stage else None,
            }
            for binding in topology.bindings()
        ],
    }


@router.post("/internal/phase6/worker/handle")
async def phase6_worker_handle(
    payload: dict,
    services=Depends(get_phase6_async_pipeline_services),
):
    if services is None:
        raise HTTPException(status_code=503, detail="Phase 6 async pipeline is not enabled")
    _command_service, worker = services
    raw_payload = payload.get("payload")
    if not isinstance(raw_payload, str):
        raise HTTPException(status_code=422, detail="payload must be a serialized message string")
    result = worker.handle_payload(raw_payload)
    return {
        "action": result.action,
        "job_id": result.job_id,
        "stage": result.stage,
        "dedupe_key": result.dedupe_key,
        "attempt": result.attempt,
        "next_retry_seconds": result.next_retry_seconds,
        "error": result.error,
    }


@router.post("/internal/phase6/retry-scheduler/run")
async def phase6_retry_scheduler_run(
    limit: int = 100,
    services=Depends(get_phase6_async_pipeline_services),
    session_factory=Depends(get_session_factory),
):
    from application.services.retry_scheduler import Phase6RetryScheduler
    from infrastructure.persistence.sqlalchemy_repositories import SQLAlchemyPipelineStageExecutionRepository

    if services is None or session_factory is None:
        raise HTTPException(status_code=503, detail="Phase 6 async pipeline is not enabled")
    command_service, _worker = services
    scheduler = Phase6RetryScheduler(
        execution_repository=SQLAlchemyPipelineStageExecutionRepository(session_factory),
        command_service=command_service,
    )
    return scheduler.schedule_due(limit=limit)


@router.get("/internal/phase7/observability")
async def phase7_observability(session_factory=Depends(get_session_factory)):
    from application.services.phase7_observability import Phase7ObservabilityService
    from domain.queue_topology import PipelineQueueTopology
    from infrastructure.messaging.rabbitmq_observability import (
        RabbitMQQueueMetricsCollector,
        RabbitMQQueueMetricsConfig,
    )

    if session_factory is None:
        raise HTTPException(status_code=503, detail="DATABASE_URL is required for observability")
    topology = PipelineQueueTopology()
    rabbitmq_url = os.environ.get("RABBITMQ_URL", "").strip()
    collector = (
        RabbitMQQueueMetricsCollector(RabbitMQQueueMetricsConfig(url=rabbitmq_url, topology=topology))
        if rabbitmq_url
        else None
    )
    return Phase7ObservabilityService(
        session_factory=session_factory,
        queue_depth_collector=collector,
        topology=topology,
    ).snapshot()


@router.get("/internal/phase7/metrics")
async def phase7_metrics(session_factory=Depends(get_session_factory)):
    from application.services.phase7_metrics import render_prometheus_metrics
    from application.services.phase7_observability import Phase7ObservabilityService
    from domain.queue_topology import PipelineQueueTopology
    from infrastructure.messaging.rabbitmq_observability import (
        RabbitMQQueueMetricsCollector,
        RabbitMQQueueMetricsConfig,
    )

    if session_factory is None:
        raise HTTPException(status_code=503, detail="DATABASE_URL is required for metrics")
    topology = PipelineQueueTopology()
    rabbitmq_url = os.environ.get("RABBITMQ_URL", "").strip()
    collector = (
        RabbitMQQueueMetricsCollector(RabbitMQQueueMetricsConfig(url=rabbitmq_url, topology=topology))
        if rabbitmq_url
        else None
    )
    snapshot = Phase7ObservabilityService(
        session_factory=session_factory,
        queue_depth_collector=collector,
        topology=topology,
    ).snapshot()
    return Response(
        content=render_prometheus_metrics(snapshot),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@router.post("/internal/phase3/spotify/sync-followed-artists")
async def phase3_sync_followed_artists(catalog_service=Depends(get_phase3_catalog_service)):
    if catalog_service is None:
        raise HTTPException(status_code=503, detail="Phase 3 catalog service is not enabled")
    try:
        return catalog_service.sync_followed_artists(trigger="manual")
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/internal/phase3/catalog/resync-active-artists")
async def phase3_refresh_active_artists(
    days: int = 14,
    limit: int | None = None,
    catalog_service=Depends(get_phase3_catalog_service),
):
    if catalog_service is None:
        raise HTTPException(status_code=503, detail="Phase 3 catalog service is not enabled")
    return catalog_service.refresh_active_artists(days=days, limit=limit, trigger="system")


@router.post("/internal/phase5/artifacts/lifecycle")
async def run_artifact_lifecycle(
    artifact_lifecycle_service=Depends(get_artifact_lifecycle_service),
):
    return artifact_lifecycle_service.run_once()
