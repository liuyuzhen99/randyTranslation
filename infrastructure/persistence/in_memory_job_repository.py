from __future__ import annotations

from copy import deepcopy
from threading import Lock
from typing import Optional

from domain.entities import Job
from domain.repositories import JobRepository


class InMemoryJobRepository(JobRepository):
    """Temporary adapter to keep legacy API behavior while moving orchestration to services."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = Lock()

    def create(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.job_id] = deepcopy(job)

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            job = self._jobs.get(job_id)
            return deepcopy(job) if job is not None else None

    def update(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.job_id] = deepcopy(job)

    def list_all(self) -> dict[str, Job]:
        with self._lock:
            return {job_id: deepcopy(job) for job_id, job in self._jobs.items()}
