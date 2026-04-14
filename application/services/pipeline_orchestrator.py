from __future__ import annotations

from domain.repositories import JobRepository
from domain.storage import MediaStorage


class PipelineOrchestrator:
    def __init__(
        self,
        repository: JobRepository,
        media_storage: MediaStorage,
        producer_backend: object,
    ) -> None:
        self._repository = repository
        self._media_storage = media_storage
        self._producer_backend = producer_backend

    def run(self, task_id: str, song_name: str) -> None:
        job = self._repository.get(task_id)
        if job is None:
            return

        self._media_storage.ensure_task_workspace(task_id)

        if hasattr(self._producer_backend, "run"):
            self._producer_backend.run(task_id, song_name)

        job.status = "completed"
        job.progress = 100
        job.result = {"message": "处理完成"}
        self._repository.save(job)
