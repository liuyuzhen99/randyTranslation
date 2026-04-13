from __future__ import annotations

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel

from api.config import validate_startup_env
from application.services.job_service import JobService
from application.services.pipeline_orchestrator import PipelineOrchestrator
from infrastructure.persistence.in_memory_job_repository import InMemoryJobRepository
from infrastructure.pipeline.legacy_producer_adapter import create_default_producer_backend
from infrastructure.storage.local_media_storage import LocalFilesystemMediaStorage
from utils.logger_manager import LogManager

system_logger = LogManager.get_task_logger("SYSTEM")

app = FastAPI(title="Hip-hop MV 自动化工坊 API")


class TaskRequest(BaseModel):
    song_name: str


class TaskResponse(BaseModel):
    task_id: str
    message: str


job_repository = InMemoryJobRepository()
job_service = JobService(job_repository)
media_storage = LocalFilesystemMediaStorage()
producer_backend = create_default_producer_backend()
orchestrator = PipelineOrchestrator(job_repository, media_storage, producer_backend)


@app.on_event("startup")
def validate_environment() -> None:
    validate_startup_env()


@app.post("/create_task", response_model=TaskResponse)
async def create_task(request: TaskRequest, background_tasks: BackgroundTasks):
    system_logger.info(f"收到创建任务请求: 歌名={request.song_name}")
    job = job_service.create_job(request.song_name)

    background_tasks.add_task(orchestrator.run, job.job_id, request.song_name)
    system_logger.info(f"任务 {job.job_id} 已创建并加入后台队列，歌名: {request.song_name}")

    return {"task_id": job.job_id, "message": "任务已启动，请稍后通过 ID 查询进度"}


@app.get("/check_status/{task_id}")
async def check_status(task_id: str):
    system_logger.info(f"查询任务状态: {task_id}")
    job = job_service.get_job(task_id)
    if job is None:
        system_logger.warning(f"查询了不存在的任务ID: {task_id}")
        raise HTTPException(status_code=404, detail="任务不存在")
    return job.to_api_dict()


@app.get("/list_tasks")
async def list_tasks():
    system_logger.info("查询所有任务状态")
    return {task_id: job.to_api_dict() for task_id, job in job_service.list_jobs().items()}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
