from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import re
import sys

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Allow running this file directly via `python path/to/api/service.py`.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from application.services.phase3_catalog_service import ArtistListFilters, CandidateListFilters
from application.services.phase4_workflow_service import (
    ReviewConflictError,
)
from api.config import (
    create_job_repository,
    create_phase4_workflow_services,
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
from infrastructure.pipeline.legacy_producer_adapter import create_default_producer_backend
from infrastructure.storage.local_media_storage import LocalFilesystemMediaStorage
from utils.logger_manager import LogManager

system_logger = LogManager.get_task_logger("SYSTEM")


class TaskRequest(BaseModel):
    song_name: str


class TaskResponse(BaseModel):
    task_id: str
    message: str


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
    media_storage = LocalFilesystemMediaStorage()
    orchestrator = PipelineOrchestrator(
        job_repository,
        media_storage,
        create_default_producer_backend,
        shadow_write_service=shadow_write_service,
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
    return (
        job_repository,
        job_service,
        media_storage,
        orchestrator,
        shadow_write_service,
        reconcile_service,
        outbox_dispatcher,
        phase3_catalog_service,
        phase4_workflow_services,
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
        orchestrator,
        shadow_write_service,
        reconcile_service,
        outbox_dispatcher,
        phase3_catalog_service,
        phase4_workflow_services,
        session_factory,
        runtime_settings,
    ) = build_runtime_services(
        outbox_publisher=outbox_publisher,
        phase3_providers=phase3_providers,
    )

    app_instance.state.job_repository = job_repository
    app_instance.state.job_service = job_service
    app_instance.state.media_storage = media_storage
    app_instance.state.orchestrator = orchestrator
    app_instance.state.shadow_write_service = shadow_write_service
    app_instance.state.reconcile_service = reconcile_service
    app_instance.state.outbox_dispatcher = outbox_dispatcher
    app_instance.state.phase3_catalog_service = phase3_catalog_service
    app_instance.state.phase4_workflow_services = phase4_workflow_services
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

        background_tasks.add_task(app_instance.state.orchestrator.run, job.job_id, request.song_name)
        system_logger.info(f"任务 {job.job_id} 已创建并加入后台队列，歌名: {request.song_name}")

        return {"task_id": job.job_id, "message": "任务已启动，请稍后通过 ID 查询进度"}

    @app_instance.get("/check_status/{task_id}")
    async def check_status(task_id: str):
        system_logger.info(f"查询任务状态: {task_id}")
        job = app_instance.state.job_service.get_job(task_id)
        if job is None:
            system_logger.warning(f"查询了不存在的任务ID: {task_id}")
            raise HTTPException(status_code=404, detail="任务不存在")
        return job.to_api_dict()

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
            return phase4_services.audit_service.approve_review(
                review_id=review_id,
                actor_id=(x_actor_id or "manual-review"),
                expected_version=request.expected_version,
                comment=request.comment,
            )
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
session_factory = app.state.session_factory
runtime_settings = app.state.runtime_settings


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
