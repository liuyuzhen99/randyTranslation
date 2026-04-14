from __future__ import annotations

from domain.entities import TranslationJob


class InMemoryJobRepository:
    def __init__(self) -> None:
        self._jobs: dict[str, TranslationJob] = {}

    def save(self, job: TranslationJob) -> None:
        self._jobs[job.job_id] = job

    def get(self, job_id: str) -> TranslationJob | None:
        return self._jobs.get(job_id)

    def list(self) -> dict[str, TranslationJob]:
        return dict(self._jobs)
