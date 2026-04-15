from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel

from api.config import (
    create_job_repository,
    create_phase2_shadow_write_service,
    load_runtime_settings,
    validate_startup_env,
)
from application.services.job_service import JobService
from application.services.pipeline_orchestrator import PipelineOrchestrator
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
    job_repository = create_job_repository(runtime_settings=runtime_settings)
    shadow_write_service = create_phase2_shadow_write_service(runtime_settings=runtime_settings)
    job_service = JobService(job_repository, shadow_write_service=shadow_write_service)
    media_storage = LocalFilesystemMediaStorage()
    orchestrator = PipelineOrchestrator(
        job_repository,
        media_storage,
        create_default_producer_backend,
        shadow_write_service=shadow_write_service,
    )
    return job_repository, job_service, media_storage, orchestrator, shadow_write_service


@asynccontextmanager
async def app_lifespan(app_instance: FastAPI):
    validate_startup_env()
    yield


def create_app() -> FastAPI:
    app_instance = FastAPI(title="Hip-hop MV 自动化工坊 API", lifespan=app_lifespan)
    job_repository, job_service, media_storage, orchestrator, shadow_write_service = build_runtime_services()

    app_instance.state.job_repository = job_repository
    app_instance.state.job_service = job_service
    app_instance.state.media_storage = media_storage
    app_instance.state.orchestrator = orchestrator
    app_instance.state.shadow_write_service = shadow_write_service

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

    return app_instance


app = create_app()
job_repository = app.state.job_repository
job_service = app.state.job_service
media_storage = app.state.media_storage
orchestrator = app.state.orchestrator
shadow_write_service = app.state.shadow_write_service


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
