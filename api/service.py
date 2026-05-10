from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import unquote

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

# Allow running this file directly via `python path/to/api/service.py`.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from application.services.phase3_catalog_service import ArtistListFilters, CandidateListFilters
from application.services.artifact_lifecycle_service import (
    ArtifactLifecyclePolicy,
    ArtifactLifecycleService,
)
from application.services.phase4_workflow_service import (
    ReviewConflictError,
)
from api.config import (
    create_artifact_repository,
    create_job_repository,
    create_media_storage,
    create_phase4_workflow_services,
    create_phase6_async_pipeline_services,
    create_phase3_catalog_service,
    create_phase2_outbox_dispatcher,
    create_phase2_reconcile_service,
    create_phase2_shadow_write_service,
    create_sqlalchemy_session_factory,
    create_vector_repository,
    load_runtime_settings,
    validate_startup_env,
)
from application.services.job_service import JobService
from application.services.async_pipeline import InProcessPipelineWorker
from application.services.pipeline_orchestrator import PipelineOrchestrator
from application.services.phase9_cutover import Phase9CutoverReadinessService
from domain.time_utils import utc_now
from domain.entities import OutboxEvent
from domain.enums import CandidateStatus, JobStatus, OutboxStatus, ReviewType, StageStatus, StageType
from domain.message_contracts import PipelineStageMessage, ReviewContext
import json
from infrastructure.persistence.sqlalchemy_repositories import SQLAlchemyPipelineStageExecutionRepository
from infrastructure.persistence.sqlalchemy_repositories import SQLAlchemyOutboxRepository
from infrastructure.pipeline.legacy_producer_adapter import create_default_producer_backend
from application.services.phase7_tracing import phase7_span
from utils.logger_manager import LogManager

system_logger = LogManager.get_task_logger("SYSTEM")
LEGACY_SUNSET_HTTP_DATE = "Thu, 31 Dec 2026 00:00:00 GMT"


class TaskRequest(BaseModel):
    song_name: str
    candidate_id: str | None = None


class TaskResponse(BaseModel):
    task_id: str
    message: str
    candidate_id: str | None = None


class CandidatePipelineResponse(BaseModel):
    candidate_id: str
    candidate_status: str
    review_id: str
    review_type: str
    review_status: str
    version: int
    task_id: str | None = None
    message: str | None = None


class CandidatePipelineRetryResponse(BaseModel):
    candidate_id: str
    job_id: str
    stage: str
    attempt: int
    message: str
    dispatch: dict | None = None


class PaginationResponse(BaseModel):
    page: int
    page_size: int
    total: int
    total_pages: int


class ResponseMeta(BaseModel):
    generated_at: str
    update_mode: str = "polling"
    refresh_hint_seconds: int = 15


class ArtistListResponse(BaseModel):
    items: list[dict]
    pagination: PaginationResponse
    meta: ResponseMeta


class CandidateListResponse(BaseModel):
    artist_id: str
    items: list[dict]
    pagination: PaginationResponse
    meta: ResponseMeta


class ArtistResyncResponse(BaseModel):
    run_id: str
    artist_id: str
    status: str
    discovered_count: int
    started_at: str
    completed_at: str
    channel_run_id: str
    discovery_run_id: str


class Phase3SpotifySyncResponse(BaseModel):
    synced_count: int
    created_count: int
    updated_count: int
    completed_at: str


class Phase3BatchRefreshResponse(BaseModel):
    requested: int
    refreshed: int
    failed: int
    failures: list[dict]


class Phase4ListResponse(BaseModel):
    items: list[dict]
    pagination: PaginationResponse
    meta: ResponseMeta


class ReviewDecisionRequest(BaseModel):
    expected_version: int
    comment: str | None = None


class ReviewDecisionResponse(BaseModel):
    review_id: str
    status: str
    version: int
    subject_id: str
    candidate_status: str
    next_review_id: str | None = None
    next_review_type: str | None = None
    decided_at: str | None = None


class TranscriptSegmentRequest(BaseModel):
    start_time: float
    end_time: float
    text: str


class TranscriptSubmissionRequest(BaseModel):
    segments: list[TranscriptSegmentRequest]
    auto_approve_review: bool = False
    comment: str | None = None


class TranscriptSubmissionResponse(BaseModel):
    candidate_id: str
    video_id: str
    segment_count: int
    auto_approve_review: bool
    review_id: str | None = None
    review_status: str | None = None
    candidate_status: str
    next_review_id: str | None = None
    next_review_type: str | None = None


class TasteAuditRequest(BaseModel):
    decision: str
    score: float | None = None
    key_lyrics: list[str] | None = None
    comment: str | None = None


class TasteAuditResponse(ReviewDecisionResponse):
    score: float | None = None
    key_lyrics: list[str] = Field(default_factory=list)


class TranslationLineRequest(BaseModel):
    line_index: int
    zh_text: str


class TranslationSubmissionRequest(BaseModel):
    translations: list[TranslationLineRequest]
    auto_approve_review: bool = False
    comment: str | None = None


class TranslationSubmissionResponse(BaseModel):
    candidate_id: str
    video_id: str
    line_count: int
    auto_approve_review: bool
    review_id: str | None = None
    review_status: str | None = None
    candidate_status: str
    next_review_id: str | None = None
    next_review_type: str | None = None


class ArtifactUrlResponse(BaseModel):
    artifact_id: str
    artifact_type: str
    object_uri: str
    url: str
    expires_in_seconds: int


class ArtifactDetailResponse(BaseModel):
    artifact_id: str
    owner_type: str
    owner_id: str
    artifact_type: str
    status: str
    object_uri: str
    object_key: str
    bucket: str
    storage_provider: str
    content_type: str | None = None
    job_id: str | None = None
    candidate_id: str | None = None
    size_bytes: int
    checksum_sha256: str
    version: int
    metadata: dict
    created_at: str
    updated_at: str
    expires_at: str | None = None
    preview_url: str | None = None
    preview_url_expires_in_seconds: int | None = None
    fallback_download_url: str | None = None


class ErrorEnvelope(BaseModel):
    error: dict
    meta: ResponseMeta


@dataclass
class RuntimeServices:
    job_repository: object
    job_service: object
    media_storage: object
    artifact_repository: object
    orchestrator: object
    shadow_write_service: object
    reconcile_service: object
    outbox_dispatcher: object
    vector_repository: object
    phase3_catalog_service: object
    phase4_workflow_services: object
    artifact_lifecycle_service: object
    phase6_async_pipeline_services: object
    session_factory: object
    runtime_settings: object


def build_runtime_services(outbox_publisher=None, phase3_providers=None):
    runtime_settings = load_runtime_settings()
    session_factory = create_sqlalchemy_session_factory(runtime_settings=runtime_settings)
    job_repository = create_job_repository(
        runtime_settings=runtime_settings,
        session_factory=session_factory,
    )
    shadow_write_service = create_phase2_shadow_write_service(
        runtime_settings=runtime_settings,
        session_factory=session_factory,
    )
    job_service = JobService(job_repository, shadow_write_service=shadow_write_service)
    media_storage = create_media_storage(runtime_settings=runtime_settings)
    artifact_repository = create_artifact_repository(
        runtime_settings=runtime_settings,
        session_factory=session_factory,
    )
    orchestrator = PipelineOrchestrator(
        job_repository,
        media_storage,
        create_default_producer_backend,
        shadow_write_service=shadow_write_service,
        artifact_repository=artifact_repository,
        final_artifact_retention_days=runtime_settings.artifact_final_retention_days,
    )
    artifact_lifecycle_service = ArtifactLifecycleService(
        artifact_repository=artifact_repository,
        media_storage=media_storage,
        policy=ArtifactLifecyclePolicy(
            temp_retention_days=runtime_settings.artifact_temp_retention_days,
            final_artifact_retention_days=runtime_settings.artifact_final_retention_days,
        ),
    )
    reconcile_service = create_phase2_reconcile_service(
        primary_job_repository=job_repository,
        runtime_settings=runtime_settings,
        session_factory=session_factory,
    )
    outbox_dispatcher = create_phase2_outbox_dispatcher(
        publisher=outbox_publisher,
        runtime_settings=runtime_settings,
        session_factory=session_factory,
    )
    try:
        vector_repository = create_vector_repository(runtime_settings=runtime_settings)
    except Exception as exc:
        system_logger.warning("Vector repository is unavailable; RAG retrieval will be skipped. error=%s", exc)
        vector_repository = None
    phase3_catalog_service = create_phase3_catalog_service(
        providers=phase3_providers,
        runtime_settings=runtime_settings,
        session_factory=session_factory,
    )
    phase4_workflow_services = create_phase4_workflow_services(
        runtime_settings=runtime_settings,
        session_factory=session_factory,
    )
    phase6_async_pipeline_services = create_phase6_async_pipeline_services(
        runtime_settings=runtime_settings,
        session_factory=session_factory,
        job_repository=job_repository,
        media_storage=media_storage,
        producer_backend_factory=create_default_producer_backend,
        workflow_services=phase4_workflow_services,
        artifact_repository=artifact_repository,
        vector_repository=vector_repository,
    )
    return RuntimeServices(
        job_repository=job_repository,
        job_service=job_service,
        media_storage=media_storage,
        artifact_repository=artifact_repository,
        orchestrator=orchestrator,
        shadow_write_service=shadow_write_service,
        reconcile_service=reconcile_service,
        outbox_dispatcher=outbox_dispatcher,
        vector_repository=vector_repository,
        phase3_catalog_service=phase3_catalog_service,
        phase4_workflow_services=phase4_workflow_services,
        artifact_lifecycle_service=artifact_lifecycle_service,
        phase6_async_pipeline_services=phase6_async_pipeline_services,
        session_factory=session_factory,
        runtime_settings=runtime_settings,
    )


@asynccontextmanager
async def app_lifespan(app_instance: FastAPI):
    validate_startup_env()
    worker = None
    runtime_settings = getattr(app_instance.state, "runtime_settings", None)
    services = getattr(app_instance.state, "phase6_async_pipeline_services", None)
    session_factory = getattr(app_instance.state, "session_factory", None)
    if (
        runtime_settings is not None
        and runtime_settings.phase6_async_pipeline_enabled
        and runtime_settings.phase6_service_worker_enabled
        and services is not None
        and session_factory is not None
    ):
        _command_service, phase6_worker = services
        worker = InProcessPipelineWorker(
            outbox_repository=SQLAlchemyOutboxRepository(session_factory),
            worker=phase6_worker,
            poll_interval_seconds=runtime_settings.phase6_service_worker_poll_seconds,
        )
        app_instance.state.phase6_service_worker = worker
        worker.start()
        system_logger.info("Phase 6 in-process worker started with service lifespan.")
    try:
        yield
    finally:
        if worker is not None:
            worker.stop()
            system_logger.info("Phase 6 in-process worker stopped.")


def create_app(outbox_publisher=None, phase3_providers=None) -> FastAPI:
    app_instance = FastAPI(title="Hip-hop MV 自动化工坊 API", lifespan=app_lifespan)
    svc = build_runtime_services(
        outbox_publisher=outbox_publisher,
        phase3_providers=phase3_providers,
    )

    app_instance.state.job_repository = svc.job_repository
    app_instance.state.job_service = svc.job_service
    app_instance.state.media_storage = svc.media_storage
    app_instance.state.artifact_repository = svc.artifact_repository
    app_instance.state.orchestrator = svc.orchestrator
    app_instance.state.shadow_write_service = svc.shadow_write_service
    app_instance.state.reconcile_service = svc.reconcile_service
    app_instance.state.outbox_dispatcher = svc.outbox_dispatcher
    app_instance.state.vector_repository = svc.vector_repository
    app_instance.state.phase3_catalog_service = svc.phase3_catalog_service
    app_instance.state.phase4_workflow_services = svc.phase4_workflow_services
    app_instance.state.artifact_lifecycle_service = svc.artifact_lifecycle_service
    app_instance.state.phase6_async_pipeline_services = svc.phase6_async_pipeline_services
    app_instance.state.session_factory = svc.session_factory
    app_instance.state.runtime_settings = svc.runtime_settings
    app_instance.state.phase6_service_worker = None

    def build_response_meta(
        generated_at: str | None = None,
        request_id: str | None = None,
        update_mode: str = "polling",
        refresh_hint_seconds: int = 15,
    ) -> dict:
        meta = {
            "generated_at": generated_at or utc_now().isoformat(),
            "update_mode": update_mode,
            "refresh_hint_seconds": refresh_hint_seconds,
        }
        if request_id:
            meta["request_id"] = request_id
        return meta

    def build_error_envelope(
        *,
        code: str,
        message: str,
        status_code: int,
        request_id: str | None = None,
        details=None,
    ) -> JSONResponse:
        payload = {
            "error": {
                "code": code,
                "message": message,
                "status_code": status_code,
            },
            "meta": build_response_meta(request_id=request_id),
        }
        if details is not None:
            payload["error"]["details"] = details
        return JSONResponse(status_code=status_code, content=payload)

    def add_legacy_deprecation_headers(response: Response, replacement_path: str) -> None:
        response.headers["Deprecation"] = "true"
        response.headers["Sunset"] = LEGACY_SUNSET_HTTP_DATE
        response.headers["Link"] = (
            f'<{replacement_path}>; rel="successor-version", '
            f'</docs/phase7-legacy-compatibility.md>; rel="deprecation"'
        )

    def resolve_artifact_status(artifact) -> str:
        if artifact.lifecycle_status in {"deleted", "delete_failed"}:
            return artifact.lifecycle_status
        if artifact.expires_at is not None and artifact.expires_at <= utc_now():
            return "expired"
        return artifact.lifecycle_status or "ready"

    def serialize_artifact_detail(
        artifact,
        *,
        include_preview_url: bool = True,
        expires_in_seconds: int = 900,
    ) -> dict:
        status = resolve_artifact_status(artifact)
        preview_url = None
        if include_preview_url and status == "ready":
            preview_url = app_instance.state.media_storage.create_presigned_url(
                artifact.object_uri,
                expires_in_seconds=expires_in_seconds,
            )
        return {
            "artifact_id": artifact.artifact_id,
            "owner_type": artifact.owner_type,
            "owner_id": artifact.owner_id,
            "artifact_type": artifact.artifact_type,
            "status": status,
            "object_uri": artifact.object_uri,
            "object_key": artifact.object_key,
            "bucket": artifact.bucket,
            "storage_provider": artifact.storage_provider,
            "content_type": artifact.content_type,
            "job_id": artifact.job_id,
            "candidate_id": artifact.candidate_id,
            "size_bytes": artifact.size_bytes,
            "checksum_sha256": artifact.checksum_sha256,
            "version": artifact.version,
            "metadata": artifact.metadata,
            "created_at": artifact.created_at.isoformat(),
            "updated_at": artifact.updated_at.isoformat(),
            "expires_at": artifact.expires_at.isoformat() if artifact.expires_at else None,
            "preview_url": preview_url,
            "preview_url_expires_in_seconds": expires_in_seconds if preview_url else None,
            "fallback_download_url": f"/v1/artifacts/{artifact.artifact_id}/download"
            if status == "ready"
            else None,
        }

    def serialize_async_execution(candidate_id: str) -> dict | None:
        if app_instance.state.session_factory is None:
            return None
        executions = SQLAlchemyPipelineStageExecutionRepository(
            app_instance.state.session_factory
        ).list_for_candidate(candidate_id)
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
                    "stage": execution.stage.value,
                    "status": execution.status.value,
                    "attempt": execution.attempt,
                    "error_message": execution.error_message,
                    "updated_at": execution.updated_at.isoformat(),
                }
                for execution in executions
            ],
        }

    def get_latest_render_job_id(candidate_id: str) -> str | None:
        phase4_services = app_instance.state.phase4_workflow_services
        if phase4_services is None:
            return None
        audit_logs = phase4_services.pipeline_service.support.audit_log_repository.list_for_aggregate(
            "candidate",
            candidate_id,
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

    def serialize_render_job(candidate_id: str) -> dict | None:
        phase4_services = app_instance.state.phase4_workflow_services
        if phase4_services is not None:
            ready_final_artifacts = [
                artifact
                for artifact in phase4_services.pipeline_service.support.list_artifacts_for_candidate(candidate_id)
                if artifact.get("artifact_type") == "final_video"
                and artifact.get("lifecycle_status") == "ready"
                and artifact.get("job_id")
            ]
            if ready_final_artifacts:
                artifact = ready_final_artifacts[-1]
                job_id = artifact["job_id"]
                job = app_instance.state.job_service.get_job(job_id)
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

        job_id = get_latest_render_job_id(candidate_id)
        if job_id is None:
            return None
        job = app_instance.state.job_service.get_job(job_id)
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

    def record_render_job(candidate_id: str, job_id: str, actor_id: str = "system") -> None:
        phase4_services = app_instance.state.phase4_workflow_services
        if phase4_services is None:
            return
        phase4_services.pipeline_service.support.log_structured(
            aggregate_type="candidate",
            aggregate_id=candidate_id,
            action="render_job_queued",
            actor_id=actor_id,
            payload={"job_id": job_id},
        )

    def get_latest_candidate_job_id(candidate_id: str, action: str) -> str | None:
        phase4_services = app_instance.state.phase4_workflow_services
        if phase4_services is None:
            return None
        audit_logs = phase4_services.pipeline_service.support.audit_log_repository.list_for_aggregate(
            "candidate",
            candidate_id,
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

    def get_active_pipeline_job(candidate_id: str):
        job_id = get_latest_candidate_job_id(candidate_id, "pipeline_job_queued")
        if job_id is None:
            return None
        job = app_instance.state.job_service.get_job(job_id)
        if job is None:
            return None
        if job.status in {JobStatus.PENDING, JobStatus.PROCESSING}:
            return job
        return None

    def record_pipeline_job(candidate_id: str, job_id: str, actor_id: str, *, mode: str) -> None:
        phase4_services = app_instance.state.phase4_workflow_services
        if phase4_services is None:
            return
        phase4_services.pipeline_service.support.log_structured(
            aggregate_type="candidate",
            aggregate_id=candidate_id,
            action="pipeline_job_queued",
            actor_id=actor_id,
            payload={"job_id": job_id, "mode": mode},
        )

    def dispatch_outbox_if_available(context: str) -> dict | None:
        outbox_dispatcher = app_instance.state.outbox_dispatcher
        if outbox_dispatcher is None:
            return None
        try:
            return outbox_dispatcher.dispatch_pending()
        except Exception:
            system_logger.exception("Failed to dispatch pending outbox events after %s", context)
            return None

    def start_candidate_pipeline_job(
        *,
        candidate,
        actor_id: str,
        background_tasks: BackgroundTasks,
        trace_id: str | None = None,
    ) -> tuple[str, str]:
        active_job = get_active_pipeline_job(candidate.candidate_id)
        if active_job is not None:
            return active_job.job_id, "候选视频已有 pipeline job 正在排队或执行，已返回现有任务 ID"

        job = app_instance.state.job_service.create_job(candidate.title)
        phase4_services = app_instance.state.phase4_workflow_services
        if phase4_services is not None and app_instance.state.phase6_async_pipeline_services is not None:
            candidate.status = CandidateStatus.DOWNLOADING
            candidate.last_seen_at = utc_now()
            phase4_services.pipeline_service.support.candidate_repository.upsert(candidate)
        if app_instance.state.phase6_async_pipeline_services is not None:
            command_service, _worker = app_instance.state.phase6_async_pipeline_services
            command_service.enqueue_first_stage(
                job,
                candidate_id=candidate.candidate_id,
                trace_id=trace_id,
            )
            dispatch_outbox_if_available("candidate pipeline enqueue")
            record_pipeline_job(candidate.candidate_id, job.job_id, actor_id, mode="phase6_async")
            return job.job_id, "候选视频已加入异步 pipeline，已排队下载并提取 transcript"

        record_pipeline_job(candidate.candidate_id, job.job_id, actor_id, mode="phase6_unavailable")
        return job.job_id, "候选视频已加入 pipeline，但 Phase 6 async 未启用，尚未开始提取 transcript"

    # 展示pipeline详情中的日志，后续可升级为websocket
    def serialize_pipeline_activity(candidate_id: str) -> dict | None:
        job_id = get_latest_candidate_job_id(candidate_id, "pipeline_job_queued")
        executions = []
        if app_instance.state.session_factory is not None:
            executions = SQLAlchemyPipelineStageExecutionRepository(
                app_instance.state.session_factory
            ).list_for_candidate(candidate_id)
        if job_id is None and executions:
            job_id = executions[-1].job_id
        if job_id is None:
            return None

        job = app_instance.state.job_service.get_job(job_id)
        logs: list[dict] = []
        if job is not None:
            logs.append(
                {
                    "timestamp": job.updated_at.isoformat(),
                    "level": "info" if job.status != JobStatus.FAILED else "error",
                    "stage": job.current_stage.value if job.current_stage else None,
                    "message": job.progress,
                }
            )
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
                logs.append(
                    {
                        "timestamp": log_entry.get("timestamp"),
                        "level": log_entry.get("level", "info"),
                        "stage": log_entry.get("stage") or execution.stage.value,
                        "message": str(log_entry.get("message") or "").strip() or f"{execution.stage.value}: log entry",
                    }
                )
            pause_reason = payload.get("pause_reason")
            if pause_reason is None:
                pause_reason = payload_body.get("pause_reason")
            if pause_reason:
                message = f"{message} ({pause_reason})"
            if execution.error_message:
                message = f"{message}: {execution.error_message}"
            logs.append(
                {
                    "timestamp": execution.updated_at.isoformat(),
                    "level": "error" if execution.error_message else "info",
                    "stage": execution.stage.value,
                    "message": message,
                }
            )

        if app_instance.state.session_factory is not None:
            pending_outbox = [
                event
                for event in SQLAlchemyOutboxRepository(app_instance.state.session_factory).list_pending()
                if event.aggregate_id == job_id
            ]
            for event in pending_outbox:
                try:
                    message = PipelineStageMessage.from_payload(event.payload)
                    stage = message.stage.value
                    text = f"{stage}: queued in outbox ({event.topic})"
                except Exception:
                    stage = None
                    text = f"queued in outbox ({event.topic})"
                logs.append(
                    {
                        "timestamp": None,
                        "level": "info",
                        "stage": stage,
                        "message": text,
                    }
                )

        logs.sort(key=lambda item: item["timestamp"] or "")
        return {
            "job_id": job_id,
            "status": job.status.value if job is not None else "missing",
            "progress": job.progress if job is not None else "Job record not found",
            "current_stage": job.current_stage.value if job is not None and job.current_stage else None,
            "updated_at": job.updated_at.isoformat() if job is not None else None,
            "logs": logs[-30:],
        }

    def manual_retry_candidate_pipeline(candidate_id: str) -> CandidatePipelineRetryResponse:
        phase4_services = app_instance.state.phase4_workflow_services
        services = app_instance.state.phase6_async_pipeline_services
        session_factory = app_instance.state.session_factory
        if phase4_services is None or services is None or session_factory is None:
            raise HTTPException(status_code=503, detail="Phase 6 async pipeline is not enabled")

        phase4_services.pipeline_service.support.get_candidate_or_raise(candidate_id)
        execution_repository = SQLAlchemyPipelineStageExecutionRepository(session_factory)
        retryable_executions = [
            execution
            for execution in execution_repository.list_for_candidate(candidate_id)
            if execution.status in {StageStatus.RETRY_SCHEDULED, StageStatus.DLQ}
        ]
        retryable_executions.sort(key=lambda execution: execution.updated_at)

        for execution in reversed(retryable_executions):
            if not execution.result_payload:
                continue
            retry_message = PipelineStageMessage.from_payload(execution.result_payload)
            command_service, _worker = services
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
                replace(
                    execution,
                    status=StageStatus.RETRY_SCHEDULED,
                    next_retry_at=None,
                    updated_at=utc_now(),
                )
            )
            dispatch_result = None
            if app_instance.state.outbox_dispatcher is not None:
                dispatch_result = app_instance.state.outbox_dispatcher.dispatch_pending()
            phase4_services.pipeline_service.support.log_structured(
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

    def resume_phase6_after_review(candidate_id: str, review_type: ReviewType, song_name: str) -> None:
        services = app_instance.state.phase6_async_pipeline_services
        if services is None:
            return
        next_stage = None
        if review_type == ReviewType.MANUAL_REVIEW:
            next_stage = StageType.TRANSLATE
        elif review_type == ReviewType.TRANSLATION_REVIEW:
            next_stage = StageType.RENDER
        if next_stage is None:
            return
        executions = SQLAlchemyPipelineStageExecutionRepository(
            app_instance.state.session_factory
        ).list_for_candidate(candidate_id)
        if not executions:
            return
        latest = executions[-1]
        try:
            resume_payload = json.loads(latest.result_payload or "{}")
        except json.JSONDecodeError:
            resume_payload = {}
        resume_payload.pop("pause", None)
        resume_payload.pop("pause_reason", None)
        command_service, _worker = services
        command_service.enqueue_stage(
            PipelineStageMessage.build(
                message_type="pipeline.stage.command",
                job_id=latest.job_id,
                stage=next_stage,
                song_name=song_name,
                trace_id=latest.trace_id,
                max_attempts=latest.max_attempts,
                payload=resume_payload,
                review=ReviewContext(candidate_id=candidate_id),
            )
        )
        if app_instance.state.outbox_dispatcher is not None:
            app_instance.state.outbox_dispatcher.dispatch_pending()

    async def serve_artifact_uri(uri: str, expires_at: str | None = None):
        if expires_at is not None:
            try:
                if utc_now() > datetime.fromisoformat(unquote(expires_at)):
                    raise HTTPException(status_code=403, detail="Artifact URL has expired")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid expires_at value") from exc
        suffix = Path(unquote(uri)).suffix or ".bin"
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        temp_file.close()
        try:
            app_instance.state.media_storage.download_artifact(unquote(uri), temp_file.name)
        except Exception as exc:
            if os.path.exists(temp_file.name):
                os.remove(temp_file.name)
            raise HTTPException(status_code=404, detail="Artifact not found") from exc
        return FileResponse(
            temp_file.name,
            filename=Path(unquote(uri)).name or "artifact",
            background=BackgroundTask(os.remove, temp_file.name),
        )

    @app_instance.middleware("http")
    async def attach_request_id(request: Request, call_next):
        request_id = (
            request.headers.get("X-Request-Id")
            or request.headers.get("X-Correlation-Id")
            or f"req-{utc_now().strftime('%Y%m%d%H%M%S%f')}"
        )
        request.state.request_id = request_id
        with phase7_span(
            "api.request",
            {
                "http.method": request.method,
                "http.route": request.url.path,
                "http.request_id": request_id,
            },
        ) as span:
            response = await call_next(request)
            span.set_attribute("http.status_code", response.status_code)
        if _legacy_successor_path(request.url.path) is not None:
            add_legacy_deprecation_headers(
                response,
                _legacy_successor_path(request.url.path) or "/v1/pipeline",
            )
        response.headers["X-Request-Id"] = request_id
        response.headers["X-Correlation-Id"] = request_id
        system_logger.info(
            "event=request_completed correlation_id=%s method=%s path=%s status_code=%s",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
        )
        return response

    def _legacy_successor_path(path: str) -> str | None:
        if path == "/create_task":
            return "/v1/candidates/{candidate_id}/render"
        if path == "/list_tasks" or path.startswith("/check_status/"):
            return "/v1/pipeline"
        return None

    @app_instance.get("/healthz")
    async def healthz():
        from application.services.phase7_health import Phase7HealthService

        result = Phase7HealthService(
            session_factory=app_instance.state.session_factory,
            media_storage=app_instance.state.media_storage,
        ).liveness()
        return {"status": result.status, "checks": result.checks}

    @app_instance.get("/readyz")
    async def readyz():
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
            session_factory=app_instance.state.session_factory,
            media_storage=app_instance.state.media_storage,
            queue_probe=queue_probe,
            qdrant_url=os.environ.get("QDRANT_URL", "").strip(),
            qdrant_api_key=os.environ.get("QDRANT_API_KEY", "").strip(),
        ).readiness()
        return JSONResponse(
            status_code=200 if result.status == "ok" else 503,
            content={"status": result.status, "checks": result.checks},
        )

    @app_instance.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        if request.url.path.startswith("/v1/"):
            detail = exc.detail
            if isinstance(detail, dict):
                message = str(detail.get("message") or detail.get("detail") or "Request failed")
                details = detail
            else:
                message = str(detail)
                details = None
            code = re.sub(r"[^a-z0-9_]+", "_", message.lower()).strip("_") or "request_failed"
            return build_error_envelope(
                code=code,
                message=message,
                status_code=exc.status_code,
                request_id=getattr(request.state, "request_id", None),
                details=details,
            )
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app_instance.exception_handler(RequestValidationError)
    async def request_validation_exception_handler(request: Request, exc: RequestValidationError):
        if request.url.path.startswith("/v1/"):
            return build_error_envelope(
                code="request_validation_error",
                message="Request validation error",
                status_code=422,
                request_id=getattr(request.state, "request_id", None),
                details=exc.errors(),
            )
        return JSONResponse(status_code=422, content={"detail": exc.errors()})

    @app_instance.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception):
        if request.url.path.startswith("/v1/"):
            return build_error_envelope(
                code="internal_server_error",
                message="Internal server error",
                status_code=500,
                request_id=getattr(request.state, "request_id", None),
            )
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    @app_instance.post("/create_task", response_model=TaskResponse)
    async def create_task(
        request: TaskRequest,
        background_tasks: BackgroundTasks,
        http_request: Request,
        response: Response,
    ):
        add_legacy_deprecation_headers(response, "/v1/candidates/{candidate_id}/render")
        system_logger.info(f"收到创建任务请求: 歌名={request.song_name}")
        job = app_instance.state.job_service.create_job(request.song_name)

        if app_instance.state.phase6_async_pipeline_services is not None:
            command_service, _worker = app_instance.state.phase6_async_pipeline_services
            command_service.enqueue_first_stage(
                job,
                candidate_id=request.candidate_id,
                trace_id=getattr(http_request.state, "request_id", None),
            )
            dispatch_outbox_if_available("legacy create_task enqueue")
            system_logger.info(f"任务 {job.job_id} 已写入 Phase 6 outbox，歌名: {request.song_name}")
            return {
                "task_id": job.job_id,
                "message": "任务已写入异步 pipeline，请稍后通过 ID 查询进度",
                "candidate_id": request.candidate_id,
            }

        if request.candidate_id:
            background_tasks.add_task(
                app_instance.state.orchestrator.run,
                job.job_id,
                request.song_name,
                request.candidate_id,
            )
        else:
            background_tasks.add_task(app_instance.state.orchestrator.run, job.job_id, request.song_name)
        system_logger.info(f"任务 {job.job_id} 已创建并加入后台队列，歌名: {request.song_name}")

        return {
            "task_id": job.job_id,
            "message": "任务已启动，请稍后通过 ID 查询进度",
            "candidate_id": request.candidate_id,
        }

    @app_instance.get("/check_status/{task_id}")
    async def check_status(task_id: str, response: Response):
        add_legacy_deprecation_headers(response, "/v1/pipeline")
        system_logger.info(f"查询任务状态: {task_id}")
        job = app_instance.state.job_service.get_job(task_id)
        if job is None:
            system_logger.warning(f"查询了不存在的任务ID: {task_id}")
            raise HTTPException(status_code=404, detail="任务不存在")
        return job.to_api_dict()

    @app_instance.post("/v1/candidates/{candidate_id}/render", response_model=TaskResponse)
    async def render_candidate(candidate_id: str, background_tasks: BackgroundTasks):
        phase4_services = app_instance.state.phase4_workflow_services
        if phase4_services is None:
            raise HTTPException(status_code=503, detail="Phase 4 workflow services are not enabled")
        try:
            candidate = phase4_services.pipeline_service.support.get_candidate_or_raise(candidate_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Candidate not found") from exc
        if candidate.status.value == "discovered":
            phase4_services.pipeline_service.add_candidate(candidate_id, actor_id="frontend-user-1")
            candidate = phase4_services.pipeline_service.support.get_candidate_or_raise(candidate_id)

        existing_render_job = serialize_render_job(candidate.candidate_id)
        if existing_render_job is not None and existing_render_job.get("status") in {
            JobStatus.PENDING.value,
            JobStatus.PROCESSING.value,
        }:
            return {
                "task_id": existing_render_job["job_id"],
                "message": "候选视频已有渲染任务正在排队或执行，已返回现有任务 ID",
                "candidate_id": candidate.candidate_id,
            }

        job = app_instance.state.job_service.create_job(candidate.title)
        record_render_job(candidate.candidate_id, job.job_id, actor_id="frontend-user-1")
        if app_instance.state.phase6_async_pipeline_services is not None:
            command_service, _worker = app_instance.state.phase6_async_pipeline_services
            command_service.enqueue_first_stage(
                job,
                candidate_id=candidate.candidate_id,
            )
            dispatch_outbox_if_available("candidate render enqueue")
            return {
                "task_id": job.job_id,
                "message": "候选视频渲染任务已写入异步 pipeline，请稍后通过 ID 查询进度",
                "candidate_id": candidate.candidate_id,
            }
        background_tasks.add_task(
            app_instance.state.orchestrator.run,
            job.job_id,
            candidate.title,
            candidate.candidate_id,
        )
        return {
            "task_id": job.job_id,
            "message": "候选视频渲染任务已启动，请稍后通过 ID 查询进度",
            "candidate_id": candidate.candidate_id,
        }

    @app_instance.post("/v1/candidates/{candidate_id}/pipeline", response_model=CandidatePipelineResponse)
    async def add_candidate_to_pipeline(
        candidate_id: str,
        background_tasks: BackgroundTasks,
        request: Request,
        x_actor_id: str | None = Header(default=None),
    ):
        phase4_services = app_instance.state.phase4_workflow_services
        if phase4_services is None:
            raise HTTPException(status_code=503, detail="Phase 4 workflow services are not enabled")
        try:
            actor_id = x_actor_id or "frontend-user-1"
            response_payload = phase4_services.pipeline_service.add_candidate(
                candidate_id=candidate_id,
                actor_id=actor_id,
            )
            candidate = phase4_services.pipeline_service.support.get_candidate_or_raise(candidate_id)
            task_id, message = start_candidate_pipeline_job(
                candidate=candidate,
                actor_id=actor_id,
                background_tasks=background_tasks,
                trace_id=getattr(request.state, "request_id", None),
            )
            response_payload["task_id"] = task_id
            response_payload["message"] = message
            if app_instance.state.phase6_async_pipeline_services is not None:
                response_payload["candidate_status"] = CandidateStatus.DOWNLOADING.value
            return response_payload
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ReviewConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app_instance.post("/v1/candidates/{candidate_id}/pipeline/retry", response_model=CandidatePipelineRetryResponse)
    async def retry_candidate_pipeline(candidate_id: str):
        try:
            return manual_retry_candidate_pipeline(candidate_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app_instance.get("/v1/candidates/{candidate_id}/workflow-detail")
    async def get_candidate_workflow_detail(candidate_id: str):
        phase4_services = app_instance.state.phase4_workflow_services
        if phase4_services is None:
            raise HTTPException(status_code=503, detail="Phase 4 workflow services are not enabled")
        try:
            return phase4_services.pipeline_service.get_candidate_detail(candidate_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app_instance.get("/list_tasks")
    async def list_tasks(response: Response):
        add_legacy_deprecation_headers(response, "/v1/pipeline")
        system_logger.info("查询所有任务状态")
        return {
            task_id: job.to_api_dict()
            for task_id, job in app_instance.state.job_service.list_jobs().items()
        }

    @app_instance.get("/internal/phase2/reconcile")
    async def phase2_reconcile():
        reconcile_service = app_instance.state.reconcile_service
        if reconcile_service is None:
            raise HTTPException(status_code=503, detail="Phase 2 reconcile service is not enabled")

        report_path = app_instance.state.runtime_settings.phase2_reconcile_report_path
        if report_path:
            report = reconcile_service.write_report(report_path)
        else:
            report = reconcile_service.generate_report()

        return {
            "report": report.to_dict(),
            "report_path": report_path,
        }

    @app_instance.get("/internal/phase9/cutover-readiness")
    async def phase9_cutover_readiness():
        runtime_settings = app_instance.state.runtime_settings
        reconcile_service = app_instance.state.reconcile_service
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

    @app_instance.post("/internal/phase2/outbox/dispatch")
    async def phase2_outbox_dispatch():
        outbox_dispatcher = app_instance.state.outbox_dispatcher
        if outbox_dispatcher is None:
            raise HTTPException(status_code=503, detail="Phase 2 outbox dispatcher is not enabled")
        return outbox_dispatcher.dispatch_pending()

    @app_instance.get("/internal/phase6/queue-topology")
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

    @app_instance.post("/internal/phase6/worker/handle")
    async def phase6_worker_handle(payload: dict):
        services = app_instance.state.phase6_async_pipeline_services
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

    @app_instance.post("/internal/phase6/retry-scheduler/run")
    async def phase6_retry_scheduler_run(limit: int = 100):
        from application.services.retry_scheduler import Phase6RetryScheduler
        from infrastructure.persistence.sqlalchemy_repositories import SQLAlchemyPipelineStageExecutionRepository

        services = app_instance.state.phase6_async_pipeline_services
        if services is None or app_instance.state.session_factory is None:
            raise HTTPException(status_code=503, detail="Phase 6 async pipeline is not enabled")
        command_service, _worker = services
        scheduler = Phase6RetryScheduler(
            execution_repository=SQLAlchemyPipelineStageExecutionRepository(app_instance.state.session_factory),
            command_service=command_service,
        )
        return scheduler.schedule_due(limit=limit)

    @app_instance.get("/internal/phase7/observability")
    async def phase7_observability():
        from application.services.phase7_observability import Phase7ObservabilityService
        from domain.queue_topology import PipelineQueueTopology
        from infrastructure.messaging.rabbitmq_observability import (
            RabbitMQQueueMetricsCollector,
            RabbitMQQueueMetricsConfig,
        )

        if app_instance.state.session_factory is None:
            raise HTTPException(status_code=503, detail="DATABASE_URL is required for observability")
        topology = PipelineQueueTopology()
        rabbitmq_url = os.environ.get("RABBITMQ_URL", "").strip()
        collector = (
            RabbitMQQueueMetricsCollector(RabbitMQQueueMetricsConfig(url=rabbitmq_url, topology=topology))
            if rabbitmq_url
            else None
        )
        return Phase7ObservabilityService(
            session_factory=app_instance.state.session_factory,
            queue_depth_collector=collector,
            topology=topology,
        ).snapshot()

    @app_instance.get("/internal/phase7/metrics")
    async def phase7_metrics():
        from application.services.phase7_metrics import render_prometheus_metrics
        from application.services.phase7_observability import Phase7ObservabilityService
        from domain.queue_topology import PipelineQueueTopology
        from infrastructure.messaging.rabbitmq_observability import (
            RabbitMQQueueMetricsCollector,
            RabbitMQQueueMetricsConfig,
        )

        if app_instance.state.session_factory is None:
            raise HTTPException(status_code=503, detail="DATABASE_URL is required for metrics")
        topology = PipelineQueueTopology()
        rabbitmq_url = os.environ.get("RABBITMQ_URL", "").strip()
        collector = (
            RabbitMQQueueMetricsCollector(RabbitMQQueueMetricsConfig(url=rabbitmq_url, topology=topology))
            if rabbitmq_url
            else None
        )
        snapshot = Phase7ObservabilityService(
            session_factory=app_instance.state.session_factory,
            queue_depth_collector=collector,
            topology=topology,
        ).snapshot()
        return Response(
            content=render_prometheus_metrics(snapshot),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app_instance.post(
        "/internal/phase3/spotify/sync-followed-artists",
        response_model=Phase3SpotifySyncResponse,
    )
    async def phase3_sync_followed_artists():
        catalog_service = app_instance.state.phase3_catalog_service
        if catalog_service is None:
            raise HTTPException(status_code=503, detail="Phase 3 catalog service is not enabled")
        try:
            return catalog_service.sync_followed_artists(trigger="manual")
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app_instance.post(
        "/internal/phase3/catalog/resync-active-artists",
        response_model=Phase3BatchRefreshResponse,
    )
    async def phase3_refresh_active_artists(days: int = 14, limit: int | None = None):
        catalog_service = app_instance.state.phase3_catalog_service
        if catalog_service is None:
            raise HTTPException(status_code=503, detail="Phase 3 catalog service is not enabled")
        return catalog_service.refresh_active_artists(days=days, limit=limit, trigger="system")

    @app_instance.get("/v1/artists", response_model=ArtistListResponse)
    async def list_artists(
        page: int = 1,
        page_size: int = 20,
        q: str = "",
        sync_status: str = "",
        sort: str = "candidate_count_desc",
    ):
        catalog_service = app_instance.state.phase3_catalog_service
        if catalog_service is None:
            raise HTTPException(status_code=503, detail="Phase 3 catalog service is not enabled")

        items, total = catalog_service.list_artists(
            filters=ArtistListFilters(
                page=max(page, 1),
                page_size=min(max(page_size, 1), 100),
                query=q,
                sync_status=sync_status,
                sort=sort,
            ),
        )
        normalized_page = max(page, 1)
        normalized_page_size = min(max(page_size, 1), 100)
        total_pages = max((total + normalized_page_size - 1) // normalized_page_size, 1)
        return {
            "items": items,
            "pagination": {
                "page": normalized_page,
                "page_size": normalized_page_size,
                "total": total,
                "total_pages": total_pages,
            },
            "meta": build_response_meta(),
        }

    @app_instance.get("/v1/artists/{artist_id}/candidates", response_model=CandidateListResponse)
    async def list_artist_candidates(
        artist_id: str,
        page: int = 1,
        page_size: int = 20,
        status: str = "",
    ):
        catalog_service = app_instance.state.phase3_catalog_service
        if catalog_service is None:
            raise HTTPException(status_code=503, detail="Phase 3 catalog service is not enabled")
        if catalog_service.artist_repository.get(artist_id) is None:
            raise HTTPException(status_code=404, detail="Artist not found")

        items, total = catalog_service.list_candidates(
            artist_id=artist_id,
            filters=CandidateListFilters(
                page=max(page, 1),
                page_size=min(max(page_size, 1), 100),
                status=status,
            ),
        )
        normalized_page = max(page, 1)
        normalized_page_size = min(max(page_size, 1), 100)
        total_pages = max((total + normalized_page_size - 1) // normalized_page_size, 1)
        return {
            "artist_id": artist_id,
            "items": items,
            "pagination": {
                "page": normalized_page,
                "page_size": normalized_page_size,
                "total": total,
                "total_pages": total_pages,
            },
            "meta": build_response_meta(),
        }

    @app_instance.post("/v1/artists/{artist_id}/resync", response_model=ArtistResyncResponse)
    async def resync_artist(artist_id: str, days: int = 14):
        catalog_service = app_instance.state.phase3_catalog_service
        if catalog_service is None:
            raise HTTPException(status_code=503, detail="Phase 3 catalog service is not enabled")
        try:
            return catalog_service.resync_artist(artist_id=artist_id, days=days, trigger="manual")
        except KeyError:
            raise HTTPException(status_code=404, detail="Artist not found")
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app_instance.get("/v1/audit-queue", response_model=Phase4ListResponse)
    async def list_audit_queue(status: str | None = None):
        phase4_services = app_instance.state.phase4_workflow_services
        if phase4_services is None:
            raise HTTPException(status_code=503, detail="Phase 4 workflow services are not enabled")
        items = phase4_services.audit_service.list_queue(status=status)
        total = len(items)
        return {
            "items": items,
            "pagination": {"page": 1, "page_size": total or 1, "total": total, "total_pages": 1},
            "meta": build_response_meta(),
        }

    @app_instance.get("/v1/pipeline", response_model=Phase4ListResponse)
    async def list_pipeline():
        phase4_services = app_instance.state.phase4_workflow_services
        if phase4_services is None:
            raise HTTPException(status_code=503, detail="Phase 4 workflow services are not enabled")
        items = phase4_services.pipeline_service.list_pipeline()
        for item in items:
            async_execution = serialize_async_execution(item["candidate_id"])
            if async_execution is not None:
                item["async_execution"] = async_execution
            render_job = serialize_render_job(item["candidate_id"])
            if render_job is not None:
                item["render_job"] = render_job
            pipeline_activity = serialize_pipeline_activity(item["candidate_id"])
            if pipeline_activity is not None:
                item["pipeline_activity"] = pipeline_activity
        total = len(items)
        return {
            "items": items,
            "pagination": {"page": 1, "page_size": total or 1, "total": total, "total_pages": 1},
            "meta": build_response_meta(),
        }

    @app_instance.get("/v1/library", response_model=Phase4ListResponse)
    async def list_library():
        phase4_services = app_instance.state.phase4_workflow_services
        if phase4_services is None:
            raise HTTPException(status_code=503, detail="Phase 4 workflow services are not enabled")
        items = phase4_services.library_service.list_library()
        total = len(items)
        return {
            "items": items,
            "pagination": {"page": 1, "page_size": total or 1, "total": total, "total_pages": 1},
            "meta": build_response_meta(),
        }

    @app_instance.post("/internal/phase5/artifacts/lifecycle")
    async def run_artifact_lifecycle():
        return app_instance.state.artifact_lifecycle_service.run_once()

    @app_instance.get("/v1/artifacts/download")
    async def download_artifact_uri(
        uri: str,
        expires_at: str | None = None,
    ):
        return await serve_artifact_uri(uri=uri, expires_at=expires_at)

    @app_instance.get("/v1/artifacts/{artifact_id}", response_model=ArtifactDetailResponse)
    async def get_artifact_detail(
        artifact_id: str,
        include_preview_url: bool = True,
        expires_in_seconds: int = Query(default=900, ge=60, le=86400),
    ):
        artifact_repository = app_instance.state.artifact_repository
        if artifact_repository is None:
            raise HTTPException(status_code=503, detail="Artifact repository is not enabled")
        artifact = artifact_repository.get(artifact_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="Artifact not found")
        return serialize_artifact_detail(
            artifact,
            include_preview_url=include_preview_url,
            expires_in_seconds=expires_in_seconds,
        )

    @app_instance.post("/v1/artifacts/{artifact_id}/refresh-url", response_model=ArtifactUrlResponse)
    async def refresh_artifact_url(
        artifact_id: str,
        expires_in_seconds: int = Query(default=900, ge=60, le=86400),
    ):
        artifact_repository = app_instance.state.artifact_repository
        if artifact_repository is None:
            raise HTTPException(status_code=503, detail="Artifact repository is not enabled")
        artifact = artifact_repository.get(artifact_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="Artifact not found")
        if resolve_artifact_status(artifact) != "ready":
            raise HTTPException(status_code=409, detail="Artifact is not ready for preview")
        return {
            "artifact_id": artifact.artifact_id,
            "artifact_type": artifact.artifact_type,
            "object_uri": artifact.object_uri,
            "url": app_instance.state.media_storage.create_presigned_url(
                artifact.object_uri,
                expires_in_seconds=expires_in_seconds,
            ),
            "expires_in_seconds": expires_in_seconds,
        }

    @app_instance.get("/v1/artifacts/{artifact_id}/preview-url", response_model=ArtifactUrlResponse)
    async def get_artifact_preview_url(
        artifact_id: str,
        expires_in_seconds: int = Query(default=900, ge=60, le=86400),
    ):
        artifact_repository = app_instance.state.artifact_repository
        if artifact_repository is None:
            raise HTTPException(status_code=503, detail="Artifact repository is not enabled")
        artifact = artifact_repository.get(artifact_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="Artifact not found")
        if resolve_artifact_status(artifact) != "ready":
            raise HTTPException(status_code=409, detail="Artifact is not ready for preview")
        return {
            "artifact_id": artifact.artifact_id,
            "artifact_type": artifact.artifact_type,
            "object_uri": artifact.object_uri,
            "url": app_instance.state.media_storage.create_presigned_url(
                artifact.object_uri,
                expires_in_seconds=expires_in_seconds,
            ),
            "expires_in_seconds": expires_in_seconds,
        }

    @app_instance.get("/v1/artifacts/{artifact_id}/download")
    async def download_artifact_by_id(artifact_id: str):
        artifact_repository = app_instance.state.artifact_repository
        if artifact_repository is None:
            raise HTTPException(status_code=503, detail="Artifact repository is not enabled")
        artifact = artifact_repository.get(artifact_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="Artifact not found")
        if resolve_artifact_status(artifact) != "ready":
            raise HTTPException(status_code=409, detail="Artifact is not ready for download")
        return await serve_artifact_uri(uri=artifact.object_uri)

    @app_instance.get("/v1/audit-log", response_model=Phase4ListResponse)
    async def list_audit_log(
        aggregate_type: str,
        aggregate_id: str,
    ):
        phase4_services = app_instance.state.phase4_workflow_services
        if phase4_services is None:
            raise HTTPException(status_code=503, detail="Phase 4 workflow services are not enabled")
        items = phase4_services.audit_service.list_audit_logs(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
        )
        total = len(items)
        return {
            "items": items,
            "pagination": {"page": 1, "page_size": total or 1, "total": total, "total_pages": 1},
            "meta": build_response_meta(),
        }

    @app_instance.post("/v1/reviews/{review_id}/approve", response_model=ReviewDecisionResponse)
    async def approve_review(
        review_id: str,
        request: ReviewDecisionRequest,
        x_actor_id: str | None = Header(default=None),
    ):
        phase4_services = app_instance.state.phase4_workflow_services
        if phase4_services is None:
            raise HTTPException(status_code=503, detail="Phase 4 workflow services are not enabled")
        try:
            review_before = phase4_services.audit_service.support.review_repository.get(review_id)
            result = phase4_services.audit_service.approve_review(
                review_id=review_id,
                actor_id=(x_actor_id or "manual-review"),
                expected_version=request.expected_version,
                comment=request.comment,
            )
            if review_before is not None:
                candidate = phase4_services.audit_service.support.get_candidate_or_raise(
                    review_before.subject_id
                )
                resume_phase6_after_review(
                    candidate_id=review_before.subject_id,
                    review_type=review_before.review_type,
                    song_name=candidate.title,
                )
            return result
        except KeyError:
            raise HTTPException(status_code=404, detail="Review not found")
        except ReviewConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app_instance.post("/v1/reviews/{review_id}/reject", response_model=ReviewDecisionResponse)
    async def reject_review(
        review_id: str,
        request: ReviewDecisionRequest,
        x_actor_id: str | None = Header(default=None),
    ):
        phase4_services = app_instance.state.phase4_workflow_services
        if phase4_services is None:
            raise HTTPException(status_code=503, detail="Phase 4 workflow services are not enabled")
        try:
            return phase4_services.audit_service.reject_review(
                review_id=review_id,
                actor_id=(x_actor_id or "manual-review"),
                expected_version=request.expected_version,
                comment=request.comment,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Review not found")
        except ReviewConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app_instance.post(
        "/v1/candidates/{candidate_id}/transcript",
        response_model=TranscriptSubmissionResponse,
    )
    async def submit_transcript(
        candidate_id: str,
        request: TranscriptSubmissionRequest,
        x_actor_id: str | None = Header(default=None),
    ):
        phase4_services = app_instance.state.phase4_workflow_services
        if phase4_services is None:
            raise HTTPException(status_code=503, detail="Phase 4 workflow services are not enabled")
        try:
            return phase4_services.automation_service.submit_transcript(
                candidate_id=candidate_id,
                actor_id=(x_actor_id or "ai-transcriber"),
                segments=[segment.model_dump() for segment in request.segments],
                auto_approve_review=request.auto_approve_review,
                comment=request.comment,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except ReviewConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app_instance.post(
        "/v1/candidates/{candidate_id}/taste-audit",
        response_model=TasteAuditResponse,
    )
    async def submit_taste_audit(
        candidate_id: str,
        request: TasteAuditRequest,
        x_actor_id: str | None = Header(default=None),
    ):
        phase4_services = app_instance.state.phase4_workflow_services
        if phase4_services is None:
            raise HTTPException(status_code=503, detail="Phase 4 workflow services are not enabled")
        normalized_decision = request.decision.strip().lower()
        if normalized_decision not in {"approved", "rejected"}:
            raise HTTPException(
                status_code=400,
                detail="Taste audit decision must be 'approved' or 'rejected'.",
            )
        try:
            return phase4_services.automation_service.record_taste_audit(
                candidate_id=candidate_id,
                actor_id=(x_actor_id or "ai-auditor"),
                approve=(normalized_decision == "approved"),
                comment=request.comment,
                score=request.score,
                key_lyrics=request.key_lyrics,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except ReviewConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app_instance.post(
        "/v1/candidates/{candidate_id}/translation",
        response_model=TranslationSubmissionResponse,
    )
    async def submit_translation(
        candidate_id: str,
        request: TranslationSubmissionRequest,
        x_actor_id: str | None = Header(default=None),
    ):
        phase4_services = app_instance.state.phase4_workflow_services
        if phase4_services is None:
            raise HTTPException(status_code=503, detail="Phase 4 workflow services are not enabled")
        try:
            return phase4_services.automation_service.submit_translation(
                candidate_id=candidate_id,
                actor_id=(x_actor_id or "ai-translator"),
                translations=[line.model_dump() for line in request.translations],
                auto_approve_review=request.auto_approve_review,
                comment=request.comment,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except ReviewConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    return app_instance


app = create_app()
job_repository = app.state.job_repository
job_service = app.state.job_service
media_storage = app.state.media_storage
orchestrator = app.state.orchestrator
shadow_write_service = app.state.shadow_write_service
reconcile_service = app.state.reconcile_service
outbox_dispatcher = app.state.outbox_dispatcher
phase3_catalog_service = app.state.phase3_catalog_service
phase4_workflow_services = app.state.phase4_workflow_services
artifact_lifecycle_service = app.state.artifact_lifecycle_service
session_factory = app.state.session_factory
runtime_settings = app.state.runtime_settings


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
