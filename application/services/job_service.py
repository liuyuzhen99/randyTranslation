from __future__ import annotations

import uuid

from domain.entities import Job
from domain.repositories import JobRepository


class JobService:
    def __init__(self, job_repository: JobRepository) -> None:
        self.job_repository = job_repository

    def create_job(self, song_name: str) -> Job:
        task_id = str(uuid.uuid4())[:8]
        job = Job(job_id=task_id, song_name=song_name)
        self.job_repository.create(job)
        return job

    def get_job(self, task_id: str) -> Job | None:
        return self.job_repository.get(task_id)

    def list_jobs(self) -> dict[str, Job]:
        return self.job_repository.list_all()
