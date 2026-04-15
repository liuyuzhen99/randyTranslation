from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel

from api.config import (
    create_job_repository,
    create_phase2_outbox_dispatcher,
    create_phase2_reconcile_service,
    create_phase2_shadow_write_service,
    create_sqlalchemy_session_factory,
    load_runtime_settings,
    validate_startup_env,
)
from application.services.job_service import JobService
from application.services.pipeline_orchestrator import PipelineOrchestrator
from application.services.outbox_dispatcher import LoggingOutboxPublisher
from infrastructure.pipeline.legacy_producer_adapter import create_default_producer_backend
from infrastructure.storage.local_media_storage import LocalFilesystemMediaStorage
from utils.logger_manager import LogManager

system_logger = LogManager.get_task_logger("SYSTEM")


class TaskRequest(BaseModel):
    song_name: str


class TaskResponse(BaseModel):
    task_id: str
    message: str


def build_runtime_services():
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
        publisher=LoggingOutboxPublisher(),
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
        session_factory,
        runtime_settings,
    )


@asynccontextmanager
async def app_lifespan(app_instance: FastAPI):
    validate_startup_env()
    yield


def create_app() -> FastAPI:
    app_instance = FastAPI(title="Hip-hop MV 自动化工坊 API", lifespan=app_lifespan)
    (
        job_repository,
        job_service,
        media_storage,
        orchestrator,
        shadow_write_service,
        reconcile_service,
        outbox_dispatcher,
        session_factory,
        runtime_settings,
    ) = build_runtime_services()

    app_instance.state.job_repository = job_repository
    app_instance.state.job_service = job_service
    app_instance.state.media_storage = media_storage
    app_instance.state.orchestrator = orchestrator
    app_instance.state.shadow_write_service = shadow_write_service
    app_instance.state.reconcile_service = reconcile_service
    app_instance.state.outbox_dispatcher = outbox_dispatcher
    app_instance.state.session_factory = session_factory
    app_instance.state.runtime_settings = runtime_settings

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

    return app_instance


app = create_app()
job_repository = app.state.job_repository
job_service = app.state.job_service
media_storage = app.state.media_storage
orchestrator = app.state.orchestrator
shadow_write_service = app.state.shadow_write_service
reconcile_service = app.state.reconcile_service
outbox_dispatcher = app.state.outbox_dispatcher
session_factory = app.state.session_factory
runtime_settings = app.state.runtime_settings


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
