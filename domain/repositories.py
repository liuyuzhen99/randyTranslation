from __future__ import annotations

from typing import Protocol

from domain.entities import TranslationJob


class JobRepository(Protocol):
    def save(self, job: TranslationJob) -> None:
        ...

    def get(self, job_id: str) -> TranslationJob | None:
        ...

    def list(self) -> dict[str, TranslationJob]:
        ...
