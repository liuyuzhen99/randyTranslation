from __future__ import annotations

from uuid import uuid4

from domain.entities import TranslationJob
from domain.repositories import JobRepository


class JobService:
    def __init__(self, repository: JobRepository) -> None:
        self._repository = repository

    def create_job(self, song_name: str) -> TranslationJob:
        job = TranslationJob(job_id=uuid4().hex[:8], song_name=song_name)
        self._repository.save(job)
        return job

    def get_job(self, job_id: str) -> TranslationJob | None:
        return self._repository.get(job_id)

    def list_jobs(self) -> dict[str, TranslationJob]:
        return self._repository.list()
