from __future__ import annotations

import logging
import uuid

from domain.entities import Job
from domain.repositories import JobRepository

logger = logging.getLogger(__name__)


class JobService:
    def __init__(self, job_repository: JobRepository, shadow_write_service=None) -> None:
        self.job_repository = job_repository
        self.shadow_write_service = shadow_write_service

    def create_job(self, song_name: str) -> Job:
        task_id = str(uuid.uuid4())[:8]
        job = Job(job_id=task_id, song_name=song_name)
        self.job_repository.create(job)
        if self.shadow_write_service is not None:
            try:
                self.shadow_write_service.record_job_created(job)
            except Exception:
                logger.exception("Phase 2 shadow-write failed during job creation for %s", task_id)
        return job

    def get_job(self, task_id: str) -> Job | None:
        return self.job_repository.get(task_id)

    def list_jobs(self) -> dict[str, Job]:
        return self.job_repository.list_all()
