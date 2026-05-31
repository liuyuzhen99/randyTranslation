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

    from domain.exceptions import NotFoundError

    @app_instance.exception_handler(NotFoundError)
    async def not_found_handler(request: Request, exc: NotFoundError):
        return JSONResponse(
            status_code=404,
            content={"code": "not_found", "resource": exc.resource, "id": exc.id},
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

    from api.routers import artists, artifacts, internal, pipeline, reviews

    app_instance.include_router(internal.router)
    app_instance.include_router(artists.router)
    app_instance.include_router(artifacts.router)
    app_instance.include_router(reviews.router)
    app_instance.include_router(pipeline.router)

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
