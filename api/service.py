from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import unquote

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request
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
    load_runtime_settings,
    validate_startup_env,
)
from application.services.job_service import JobService
from application.services.pipeline_orchestrator import PipelineOrchestrator
from domain.time_utils import utc_now
from domain.enums import ReviewType, StageType
from domain.message_contracts import PipelineStageMessage, ReviewContext
import json
from infrastructure.persistence.sqlalchemy_repositories import SQLAlchemyPipelineStageExecutionRepository
from infrastructure.pipeline.legacy_producer_adapter import create_default_producer_backend
from utils.logger_manager import LogManager

system_logger = LogManager.get_task_logger("SYSTEM")


class TaskRequest(BaseModel):
    song_name: str
    candidate_id: str | None = None


class TaskResponse(BaseModel):
    task_id: str
    message: str
    candidate_id: str | None = None


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
    )
    return (
        job_repository,
        job_service,
        media_storage,
        artifact_repository,
        orchestrator,
        shadow_write_service,
        reconcile_service,
        outbox_dispatcher,
        phase3_catalog_service,
        phase4_workflow_services,
        artifact_lifecycle_service,
        phase6_async_pipeline_services,
        session_factory,
        runtime_settings,
    )


@asynccontextmanager
async def app_lifespan(app_instance: FastAPI):
    validate_startup_env()
    yield


def create_app(outbox_publisher=None, phase3_providers=None) -> FastAPI:
    app_instance = FastAPI(title="Hip-hop MV 自动化工坊 API", lifespan=app_lifespan)
    (
        job_repository,
        job_service,
        media_storage,
        artifact_repository,
        orchestrator,
        shadow_write_service,
        reconcile_service,
        outbox_dispatcher,
        phase3_catalog_service,
        phase4_workflow_services,
        artifact_lifecycle_service,
        phase6_async_pipeline_services,
        session_factory,
        runtime_settings,
    ) = build_runtime_services(
        outbox_publisher=outbox_publisher,
        phase3_providers=phase3_providers,
    )

    app_instance.state.job_repository = job_repository
    app_instance.state.job_service = job_service
    app_instance.state.media_storage = media_storage
    app_instance.state.artifact_repository = artifact_repository
    app_instance.state.orchestrator = orchestrator
    app_instance.state.shadow_write_service = shadow_write_service
    app_instance.state.reconcile_service = reconcile_service
    app_instance.state.outbox_dispatcher = outbox_dispatcher
    app_instance.state.phase3_catalog_service = phase3_catalog_service
    app_instance.state.phase4_workflow_services = phase4_workflow_services
    app_instance.state.artifact_lifecycle_service = artifact_lifecycle_service
    app_instance.state.phase6_async_pipeline_services = phase6_async_pipeline_services
    app_instance.state.session_factory = session_factory
    app_instance.state.runtime_settings = runtime_settings

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
        request_id = request.headers.get("X-Request-Id") or f"req-{utc_now().strftime('%Y%m%d%H%M%S%f')}"
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response

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
    async def create_task(request: TaskRequest, background_tasks: BackgroundTasks):
        system_logger.info(f"收到创建任务请求: 歌名={request.song_name}")
        job = app_instance.state.job_service.create_job(request.song_name)

        if app_instance.state.phase6_async_pipeline_services is not None:
            command_service, _worker = app_instance.state.phase6_async_pipeline_services
            command_service.enqueue_first_stage(
                job,
                candidate_id=request.candidate_id,
                trace_id=getattr(request, "request_id", None),
            )
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
    async def check_status(task_id: str):
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

        job = app_instance.state.job_service.create_job(candidate.title)
        if app_instance.state.phase6_async_pipeline_services is not None:
            command_service, _worker = app_instance.state.phase6_async_pipeline_services
            command_service.enqueue_first_stage(
                job,
                candidate_id=candidate.candidate_id,
            )
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

    @app_instance.get("/list_tasks")
    async def list_tasks():
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
