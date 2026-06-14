from __future__ import annotations

import json
from dataclasses import replace

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, Response

from api.dependencies import (
    get_job_service,
    get_orchestrator,
    get_outbox_dispatcher,
    get_review_workflow_services,
    get_async_pipeline_services,
    get_session_factory,
    get_shadow_write_degraded,
)
from api.service import (
    CandidatePipelineResponse,
    CandidatePipelineRetryResponse,
    TaskRequest,
    TaskResponse,
)
from application.services.review_workflow_service import ReviewConflictError
from domain.entities import OutboxEvent
from domain.enums import CandidateStatus, JobStatus, OutboxStatus, StageStatus, StageType
from domain.message_contracts import PipelineStageMessage, ReviewContext
from domain.time_utils import utc_now

router = APIRouter(tags=["pipeline"])

import logging
system_logger = logging.getLogger("hiphop_app")


def _dispatch_outbox_if_available(outbox_dispatcher) -> dict | None:
    if outbox_dispatcher is None:
        return None
    try:
        return outbox_dispatcher.dispatch_pending()
    except Exception:
        system_logger.exception("Failed to dispatch pending outbox events")
        return None


def _get_latest_render_job_id(candidate_id: str, review_services) -> str | None:
    if review_services is None:
        return None
    audit_logs = review_services.pipeline_service.support.audit_log_repository.list_for_aggregate(
        "candidate", candidate_id
    )
    for entry in reversed(audit_logs):
        if entry.action != "render_job_queued" or not entry.details:
            continue
        try:
            payload = json.loads(entry.details)
        except json.JSONDecodeError:
            continue
        job_id = payload.get("job_id")
        if isinstance(job_id, str) and job_id:
            return job_id
    return None


def _get_latest_candidate_job_id(candidate_id: str, action: str, review_services) -> str | None:
    if review_services is None:
        return None
    audit_logs = review_services.pipeline_service.support.audit_log_repository.list_for_aggregate(
        "candidate", candidate_id
    )
    for entry in reversed(audit_logs):
        if entry.action != action or not entry.details:
            continue
        try:
            payload = json.loads(entry.details)
        except json.JSONDecodeError:
            continue
        job_id = payload.get("job_id")
        if isinstance(job_id, str) and job_id:
            return job_id
    return None


def _serialize_render_job(candidate_id: str, *, job_service, review_services) -> dict | None:
    if review_services is not None:
        ready_final_artifacts = [
            artifact
            for artifact in review_services.pipeline_service.support.list_artifacts_for_candidate(candidate_id)
            if artifact.get("artifact_type") == "final_video"
            and artifact.get("lifecycle_status") == "ready"
            and artifact.get("job_id")
        ]
        if ready_final_artifacts:
            artifact = ready_final_artifacts[-1]
            job_id = artifact["job_id"]
            job = job_service.get_job(job_id)
            return {
                "job_id": job_id,
                "status": JobStatus.COMPLETED.value,
                "progress": job.progress if job is not None else "Final artifact is ready",
                "result": job.result if job is not None else artifact.get("object_uri"),
                "current_stage": job.current_stage.value if job is not None and job.current_stage else StageType.RENDER.value,
                "updated_at": (
                    job.updated_at.isoformat()
                    if job is not None
                    else artifact.get("updated_at") or artifact.get("created_at")
                ),
            }
    job_id = _get_latest_render_job_id(candidate_id, review_services)
    if job_id is None:
        return None
    job = job_service.get_job(job_id)
    if job is None:
        return {"job_id": job_id, "status": "missing", "progress": "Render job record not found"}
    return {
        "job_id": job.job_id,
        "status": job.status.value,
        "progress": job.progress,
        "result": job.result,
        "current_stage": job.current_stage.value if job.current_stage else None,
        "updated_at": job.updated_at.isoformat(),
    }


def _serialize_async_execution(candidate_id: str, *, session_factory) -> dict | None:
    if session_factory is None:
        return None
    from infrastructure.persistence.sqlalchemy_repositories import SQLAlchemyPipelineStageExecutionRepository
    executions = SQLAlchemyPipelineStageExecutionRepository(session_factory).list_for_candidate(candidate_id)
    if not executions:
        return None
    latest = executions[-1]
    try:
        latest_payload = json.loads(latest.result_payload or "{}")
    except json.JSONDecodeError:
        latest_payload = {}
    return {
        "job_id": latest.job_id,
        "current_stage": latest.stage.value,
        "status": latest.status.value,
        "attempt": latest.attempt,
        "max_attempts": latest.max_attempts,
        "next_retry_at": latest.next_retry_at.isoformat() if latest.next_retry_at else None,
        "error_message": latest.error_message,
        "pause_reason": latest_payload.get("pause_reason"),
        "updated_at": latest.updated_at.isoformat(),
        "stages": [
            {
                "stage": e.stage.value,
                "status": e.status.value,
                "attempt": e.attempt,
                "error_message": e.error_message,
                "updated_at": e.updated_at.isoformat(),
            }
            for e in executions
        ],
    }


def _serialize_pipeline_activity(candidate_id: str, *, job_service, review_services, session_factory) -> dict | None:
    job_id = _get_latest_candidate_job_id(candidate_id, "pipeline_job_queued", review_services)
    executions = []
    if session_factory is not None:
        from infrastructure.persistence.sqlalchemy_repositories import (
            SQLAlchemyOutboxRepository,
            SQLAlchemyPipelineStageExecutionRepository,
        )
        executions = SQLAlchemyPipelineStageExecutionRepository(session_factory).list_for_candidate(candidate_id)
    if job_id is None and executions:
        job_id = executions[-1].job_id
    if job_id is None:
        return None

    job = job_service.get_job(job_id)
    logs: list[dict] = []
    if job is not None:
        logs.append({
            "timestamp": job.updated_at.isoformat(),
            "level": "info" if job.status != JobStatus.FAILED else "error",
            "stage": job.current_stage.value if job.current_stage else None,
            "message": job.progress,
        })
    for execution in executions:
        message = f"{execution.stage.value}: {execution.status.value}"
        try:
            payload = json.loads(execution.result_payload or "{}")
        except json.JSONDecodeError:
            payload = {}
        payload_body = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
        for log_entry in payload_body.get("_logs", []) if isinstance(payload_body.get("_logs"), list) else []:
            if not isinstance(log_entry, dict):
                continue
            logs.append({
                "timestamp": log_entry.get("timestamp"),
                "level": log_entry.get("level", "info"),
                "stage": log_entry.get("stage") or execution.stage.value,
                "message": str(log_entry.get("message") or "").strip() or f"{execution.stage.value}: log entry",
            })
        pause_reason = payload.get("pause_reason") or payload_body.get("pause_reason")
        if pause_reason:
            message = f"{message} ({pause_reason})"
        if execution.error_message:
            message = f"{message}: {execution.error_message}"
        logs.append({
            "timestamp": execution.updated_at.isoformat(),
            "level": "error" if execution.error_message else "info",
            "stage": execution.stage.value,
            "message": message,
        })

    if session_factory is not None:
        from infrastructure.persistence.sqlalchemy_repositories import SQLAlchemyOutboxRepository
        pending_outbox = [
            event for event in SQLAlchemyOutboxRepository(session_factory).list_pending()
            if event.aggregate_id == job_id
        ]
        for event in pending_outbox:
            try:
                msg = PipelineStageMessage.from_payload(event.payload)
                stage = msg.stage.value
                text = f"{stage}: queued in outbox ({event.topic})"
            except Exception:
                stage = None
                text = f"queued in outbox ({event.topic})"
            logs.append({"timestamp": None, "level": "info", "stage": stage, "message": text})

    logs.sort(key=lambda item: item["timestamp"] or "")
    return {
        "job_id": job_id,
        "status": job.status.value if job is not None else "missing",
        "progress": job.progress if job is not None else "Job record not found",
        "current_stage": job.current_stage.value if job is not None and job.current_stage else None,
        "updated_at": job.updated_at.isoformat() if job is not None else None,
        "logs": logs[-30:],
    }


@router.post("/create_task", response_model=TaskResponse)
async def create_task(
    request: TaskRequest,
    background_tasks: BackgroundTasks,
    http_request: Request,
    response: Response,
    job_service=Depends(get_job_service),
    pipeline_services=Depends(get_async_pipeline_services),
    outbox_dispatcher=Depends(get_outbox_dispatcher),
    orchestrator=Depends(get_orchestrator),
):
    response.headers["Deprecation"] = "true"
    response.headers["Link"] = '</v1/candidates/{candidate_id}/render>; rel="successor-version"'
    system_logger.info(f"收到创建任务请求: 歌名={request.song_name}")
    job = job_service.create_job(request.song_name)

    if pipeline_services is not None:
        command_service, _worker = pipeline_services
        command_service.enqueue_first_stage(
            job,
            candidate_id=request.candidate_id,
            trace_id=getattr(http_request.state, "request_id", None),
        )
        _dispatch_outbox_if_available(outbox_dispatcher)
        system_logger.info(f"任务 {job.job_id} 已写入 pipeline outbox，歌名: {request.song_name}")
        return {"task_id": job.job_id, "message": "任务已写入异步 pipeline，请稍后通过 ID 查询进度", "candidate_id": request.candidate_id}

    if request.candidate_id:
        background_tasks.add_task(orchestrator.run, job.job_id, request.song_name, request.candidate_id)
    else:
        background_tasks.add_task(orchestrator.run, job.job_id, request.song_name)
    system_logger.info(f"任务 {job.job_id} 已创建并加入后台队列，歌名: {request.song_name}")
    return {"task_id": job.job_id, "message": "任务已启动，请稍后通过 ID 查询进度", "candidate_id": request.candidate_id}


@router.get("/check_status/{task_id}")
async def check_status(
    task_id: str,
    response: Response,
    job_service=Depends(get_job_service),
):
    response.headers["Deprecation"] = "true"
    response.headers["Link"] = '</v1/pipeline>; rel="successor-version"'
    system_logger.info(f"查询任务状态: {task_id}")
    job = job_service.get_job(task_id)
    if job is None:
        system_logger.warning(f"查询了不存在的任务ID: {task_id}")
        from domain.exceptions import NotFoundError
        raise NotFoundError("job", task_id)
    return job.to_api_dict()


@router.get("/list_tasks")
async def list_tasks(
    response: Response,
    job_service=Depends(get_job_service),
):
    response.headers["Deprecation"] = "true"
    response.headers["Link"] = '</v1/pipeline>; rel="successor-version"'
    system_logger.info("查询所有任务状态")
    return {task_id: job.to_api_dict() for task_id, job in job_service.list_jobs().items()}


@router.post("/v1/candidates/{candidate_id}/render", response_model=TaskResponse)
async def render_candidate(
    candidate_id: str,
    job_service=Depends(get_job_service),
    review_services=Depends(get_review_workflow_services),
    pipeline_services=Depends(get_async_pipeline_services),
    outbox_dispatcher=Depends(get_outbox_dispatcher),
):
    if review_services is None:
        raise HTTPException(status_code=503, detail="Review workflow services are not enabled")
    try:
        candidate = review_services.pipeline_service.support.get_candidate_or_raise(candidate_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Candidate not found") from exc
    if candidate.status == CandidateStatus.DISCOVERED:
        raise HTTPException(status_code=409, detail="Candidate must be added to pipeline before render")

    existing_render_job = _serialize_render_job(candidate.candidate_id, job_service=job_service, review_services=review_services)
    if existing_render_job is not None and existing_render_job.get("status") in {JobStatus.PENDING.value, JobStatus.PROCESSING.value}:
        return {"task_id": existing_render_job["job_id"], "message": "候选视频已有渲染任务正在排队或执行，已返回现有任务 ID", "candidate_id": candidate.candidate_id}

    if pipeline_services is None:
        raise HTTPException(status_code=503, detail="Async pipeline is not enabled")

    job = job_service.create_job(candidate.title)
    _record_render_job(candidate.candidate_id, job.job_id, review_services=review_services, actor_id="frontend-user-1")
    command_service, _worker = pipeline_services
    command_service.enqueue_first_stage(job, candidate_id=candidate.candidate_id)
    _dispatch_outbox_if_available(outbox_dispatcher)
    return {"task_id": job.job_id, "message": "候选视频渲染任务已写入异步 pipeline，请稍后通过 ID 查询进度", "candidate_id": candidate.candidate_id}


@router.post("/v1/candidates/{candidate_id}/pipeline", response_model=CandidatePipelineResponse)
async def add_candidate_to_pipeline(
    candidate_id: str,
    background_tasks: BackgroundTasks,
    request: Request,
    x_actor_id: str | None = Header(default=None),
    job_service=Depends(get_job_service),
    review_services=Depends(get_review_workflow_services),
    pipeline_services=Depends(get_async_pipeline_services),
    outbox_dispatcher=Depends(get_outbox_dispatcher),
):
    if review_services is None:
        raise HTTPException(status_code=503, detail="Review workflow services are not enabled")
    try:
        actor_id = x_actor_id or "frontend-user-1"
        response_payload = review_services.pipeline_service.add_candidate(candidate_id=candidate_id, actor_id=actor_id)
        candidate = review_services.pipeline_service.support.get_candidate_or_raise(candidate_id)
        task_id, message = _start_candidate_pipeline_job(
            candidate=candidate,
            actor_id=actor_id,
            background_tasks=background_tasks,
            trace_id=getattr(request.state, "request_id", None),
            job_service=job_service,
            review_services=review_services,
            pipeline_services=pipeline_services,
            outbox_dispatcher=outbox_dispatcher,
        )
        response_payload["task_id"] = task_id
        response_payload["message"] = message
        if pipeline_services is not None:
            response_payload["candidate_status"] = CandidateStatus.DOWNLOADING.value
        return response_payload
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ReviewConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/v1/candidates/{candidate_id}/pipeline/retry", response_model=CandidatePipelineRetryResponse)
async def retry_candidate_pipeline(
    candidate_id: str,
    review_services=Depends(get_review_workflow_services),
    pipeline_services=Depends(get_async_pipeline_services),
    outbox_dispatcher=Depends(get_outbox_dispatcher),
    session_factory=Depends(get_session_factory),
):
    try:
        return _manual_retry_candidate_pipeline(
            candidate_id,
            review_services=review_services,
            pipeline_services=pipeline_services,
            outbox_dispatcher=outbox_dispatcher,
            session_factory=session_factory,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/v1/candidates/{candidate_id}/workflow-detail")
async def get_candidate_workflow_detail(
    candidate_id: str,
    review_services=Depends(get_review_workflow_services),
):
    if review_services is None:
        raise HTTPException(status_code=503, detail="Review workflow services are not enabled")
    try:
        return review_services.pipeline_service.get_candidate_detail(candidate_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/v1/pipeline")
async def list_pipeline(
    job_service=Depends(get_job_service),
    review_services=Depends(get_review_workflow_services),
    session_factory=Depends(get_session_factory),
    shadow_write_degraded: bool = Depends(get_shadow_write_degraded),
):
    if review_services is None:
        raise HTTPException(status_code=503, detail="Review workflow services are not enabled")
    items = review_services.pipeline_service.list_pipeline()
    for item in items:
        async_execution = _serialize_async_execution(item["candidate_id"], session_factory=session_factory)
        if async_execution is not None:
            item["async_execution"] = async_execution
        render_job = _serialize_render_job(item["candidate_id"], job_service=job_service, review_services=review_services)
        if render_job is not None:
            item["render_job"] = render_job
        pipeline_activity = _serialize_pipeline_activity(
            item["candidate_id"],
            job_service=job_service,
            review_services=review_services,
            session_factory=session_factory,
        )
        if pipeline_activity is not None:
            item["pipeline_activity"] = pipeline_activity
    total = len(items)
    meta = {"generated_at": utc_now().isoformat(), "update_mode": "polling", "refresh_hint_seconds": 15}
    if shadow_write_degraded:
        meta["shadow_write_degraded"] = True
    return {
        "items": items,
        "pagination": {"page": 1, "page_size": total or 1, "total": total, "total_pages": 1},
        "meta": meta,
    }


@router.get("/v1/library")
async def list_library(
    review_services=Depends(get_review_workflow_services),
    shadow_write_degraded: bool = Depends(get_shadow_write_degraded),
):
    if review_services is None:
        raise HTTPException(status_code=503, detail="Review workflow services are not enabled")
    items = review_services.library_service.list_library()
    total = len(items)
    meta = {"generated_at": utc_now().isoformat(), "update_mode": "polling", "refresh_hint_seconds": 15}
    if shadow_write_degraded:
        meta["shadow_write_degraded"] = True
    return {
        "items": items,
        "pagination": {"page": 1, "page_size": total or 1, "total": total, "total_pages": 1},
        "meta": meta,
    }


# ── internal helpers ──────────────────────────────────────────────────────────

def _record_render_job(candidate_id: str, job_id: str, *, review_services, actor_id: str = "system") -> None:
    if review_services is None:
        return
    review_services.pipeline_service.support.log_structured(
        aggregate_type="candidate",
        aggregate_id=candidate_id,
        action="render_job_queued",
        actor_id=actor_id,
        payload={"job_id": job_id},
    )


def _record_pipeline_job(candidate_id: str, job_id: str, actor_id: str, *, mode: str, review_services) -> None:
    if review_services is None:
        return
    review_services.pipeline_service.support.log_structured(
        aggregate_type="candidate",
        aggregate_id=candidate_id,
        action="pipeline_job_queued",
        actor_id=actor_id,
        payload={"job_id": job_id, "mode": mode},
    )


def _get_active_pipeline_job(candidate_id: str, *, job_service, review_services):
    job_id = _get_latest_candidate_job_id(candidate_id, "pipeline_job_queued", review_services)
    if job_id is None:
        return None
    job = job_service.get_job(job_id)
    if job is None:
        return None
    if job.status in {JobStatus.PENDING, JobStatus.PROCESSING}:
        return job
    return None


def _start_candidate_pipeline_job(
    *,
    candidate,
    actor_id: str,
    background_tasks: BackgroundTasks,
    trace_id: str | None,
    job_service,
    review_services,
    pipeline_services,
    outbox_dispatcher,
) -> tuple[str, str]:
    active_job = _get_active_pipeline_job(candidate.candidate_id, job_service=job_service, review_services=review_services)
    if active_job is not None:
        return active_job.job_id, "候选视频已有 pipeline job 正在排队或执行，已返回现有任务 ID"

    job = job_service.create_job(candidate.title)
    if review_services is not None and pipeline_services is not None:
        candidate.status = CandidateStatus.DOWNLOADING
        candidate.last_seen_at = utc_now()
        review_services.pipeline_service.support.candidate_repository.upsert(candidate)
    if pipeline_services is not None:
        command_service, _worker = pipeline_services
        command_service.enqueue_first_stage(job, candidate_id=candidate.candidate_id, trace_id=trace_id)
        _dispatch_outbox_if_available(outbox_dispatcher)
        _record_pipeline_job(candidate.candidate_id, job.job_id, actor_id, mode="async_pipeline", review_services=review_services)
        return job.job_id, "候选视频已加入异步 pipeline，已排队下载并提取 transcript"

    _record_pipeline_job(candidate.candidate_id, job.job_id, actor_id, mode="async_pipeline_unavailable", review_services=review_services)
    return job.job_id, "候选视频已加入 pipeline，但 MV pipeline 未启用，尚未开始提取 transcript"


def _manual_retry_candidate_pipeline(
    candidate_id: str,
    *,
    review_services,
    pipeline_services,
    outbox_dispatcher,
    session_factory,
) -> CandidatePipelineRetryResponse:
    if review_services is None or pipeline_services is None or session_factory is None:
        raise HTTPException(status_code=503, detail="Async pipeline is not enabled")

    from infrastructure.persistence.sqlalchemy_repositories import (
        SQLAlchemyOutboxRepository,
        SQLAlchemyPipelineStageExecutionRepository,
    )

    review_services.pipeline_service.support.get_candidate_or_raise(candidate_id)
    execution_repository = SQLAlchemyPipelineStageExecutionRepository(session_factory)
    retryable_executions = [
        execution
        for execution in execution_repository.list_for_candidate(candidate_id)
        if execution.status in {StageStatus.RETRY_SCHEDULED, StageStatus.DLQ}
    ]
    retryable_executions.sort(key=lambda e: e.updated_at)

    for execution in reversed(retryable_executions):
        if not execution.result_payload:
            continue
        retry_message = PipelineStageMessage.from_payload(execution.result_payload)
        command_service, _worker = pipeline_services
        outbox_repository = SQLAlchemyOutboxRepository(session_factory)
        event_id = f"{retry_message.message_type}:{retry_message.dedupe_key}"
        existing_event = outbox_repository.get(event_id)
        if existing_event is None:
            command_service.enqueue_stage(retry_message)
        else:
            outbox_repository.update(
                OutboxEvent(
                    event_id=existing_event.event_id,
                    topic=command_service.topology.stage_queue(retry_message.stage),
                    payload=retry_message.to_payload(),
                    status=OutboxStatus.PENDING,
                    aggregate_id=retry_message.job_id,
                    dedupe_key=retry_message.dedupe_key,
                    correlation_id=retry_message.trace_id,
                )
            )
        execution_repository.upsert(
            replace(execution, status=StageStatus.RETRY_SCHEDULED, next_retry_at=None, updated_at=utc_now())
        )
        dispatch_result = _dispatch_outbox_if_available(outbox_dispatcher)
        review_services.pipeline_service.support.log_structured(
            aggregate_type="candidate",
            aggregate_id=candidate_id,
            action="pipeline_retry_requested",
            actor_id="frontend-user-1",
            payload={
                "job_id": retry_message.job_id,
                "stage": retry_message.stage.value,
                "attempt": retry_message.retry.attempt,
                "dedupe_key": retry_message.dedupe_key,
            },
        )
        return CandidatePipelineRetryResponse(
            candidate_id=candidate_id,
            job_id=retry_message.job_id,
            stage=retry_message.stage.value,
            attempt=retry_message.retry.attempt,
            message="Pipeline retry has been queued. Start or keep a worker running for this stage queue.",
            dispatch=dispatch_result,
        )

    raise HTTPException(status_code=409, detail="No retryable failed pipeline stage found for this candidate")
