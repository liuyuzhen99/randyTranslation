from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable, Optional

from domain.entities import (
    Artist,
    ArtistSyncRun,
    Job,
    JobEvent,
    OutboxEvent,
    Subtitle,
    VectorRecord,
    Video,
    VideoCandidate,
)


class ArtistRepository(ABC):
    @abstractmethod
    def upsert(self, artist: Artist) -> None:
        raise NotImplementedError

    @abstractmethod
    def get(self, spotify_id: str) -> Optional[Artist]:
        raise NotImplementedError

    @abstractmethod
    def list_all(self) -> list[Artist]:
        raise NotImplementedError


class VideoRepository(ABC):
    @abstractmethod
    def upsert(self, video: Video) -> None:
        raise NotImplementedError

    @abstractmethod
    def get(self, video_id: str) -> Optional[Video]:
        raise NotImplementedError


class SubtitleRepository(ABC):
    @abstractmethod
    def replace_for_video(self, video_id: str, subtitles: Iterable[Subtitle]) -> None:
        raise NotImplementedError

    @abstractmethod
    def list_for_video(self, video_id: str) -> list[Subtitle]:
        raise NotImplementedError


class JobRepository(ABC):
    @abstractmethod
    def create(self, job: Job) -> None:
        raise NotImplementedError

    @abstractmethod
    def get(self, job_id: str) -> Optional[Job]:
        raise NotImplementedError

    @abstractmethod
    def update(self, job: Job) -> None:
        raise NotImplementedError

    @abstractmethod
    def list_all(self) -> dict[str, Job]:
        raise NotImplementedError


class OutboxRepository(ABC):
    @abstractmethod
    def add(self, event: OutboxEvent) -> None:
        raise NotImplementedError

    @abstractmethod
    def list_pending(self) -> list[OutboxEvent]:
        raise NotImplementedError

    @abstractmethod
    def get(self, event_id: str) -> OutboxEvent | None:
        raise NotImplementedError

    @abstractmethod
    def update(self, event: OutboxEvent) -> None:
        raise NotImplementedError


class JobEventRepository(ABC):
    @abstractmethod
    def add(self, event: JobEvent) -> None:
        raise NotImplementedError

    @abstractmethod
    def list_for_job(self, job_id: str) -> list[JobEvent]:
        raise NotImplementedError


class ArtistSyncRunRepository(ABC):
    @abstractmethod
    def create(self, run: ArtistSyncRun) -> None:
        raise NotImplementedError

    @abstractmethod
    def update(self, run: ArtistSyncRun) -> None:
        raise NotImplementedError

    @abstractmethod
    def get(self, run_id: str) -> ArtistSyncRun | None:
        raise NotImplementedError

    @abstractmethod
    def list_for_artist(self, spotify_id: str) -> list[ArtistSyncRun]:
        raise NotImplementedError


class CandidateRepository(ABC):
    @abstractmethod
    def upsert(self, candidate: VideoCandidate) -> None:
        raise NotImplementedError

    @abstractmethod
    def get(self, candidate_id: str) -> VideoCandidate | None:
        raise NotImplementedError

    @abstractmethod
    def list_for_artist(self, spotify_id: str) -> list[VideoCandidate]:
        raise NotImplementedError


class VectorRepository(ABC):
    @abstractmethod
    def upsert(self, record: VectorRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    def search(self, namespace: str, text: str, limit: int = 5) -> list[VectorRecord]:
        raise NotImplementedError
