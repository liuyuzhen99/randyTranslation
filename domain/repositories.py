from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Iterable, Optional

from domain.entities import (
    ArtifactRecord,
    AuditLogEntry,
    Artist,
    ArtistSyncRun,
    Job,
    JobEvent,
    OutboxEvent,
    PipelineStageExecution,
    ReviewItem,
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


class PipelineStageExecutionRepository(ABC):
    @abstractmethod
    def upsert(self, execution: PipelineStageExecution) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_by_dedupe_key(self, dedupe_key: str) -> PipelineStageExecution | None:
        raise NotImplementedError

    @abstractmethod
    def list_for_job(self, job_id: str) -> list[PipelineStageExecution]:
        raise NotImplementedError

    @abstractmethod
    def list_for_candidate(self, candidate_id: str) -> list[PipelineStageExecution]:
        raise NotImplementedError

    @abstractmethod
    def list_due_retries(self, now: datetime, limit: int = 100) -> list[PipelineStageExecution]:
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


class ReviewRepository(ABC):
    @abstractmethod
    def create(self, review: ReviewItem) -> None:
        raise NotImplementedError

    @abstractmethod
    def update(self, review: ReviewItem) -> None:
        raise NotImplementedError

    @abstractmethod
    def get(self, review_id: str) -> ReviewItem | None:
        raise NotImplementedError

    @abstractmethod
    def list_for_subject(self, subject_kind: str, subject_id: str) -> list[ReviewItem]:
        raise NotImplementedError

    @abstractmethod
    def list_pending(self) -> list[ReviewItem]:
        raise NotImplementedError


class AuditLogRepository(ABC):
    @abstractmethod
    def add(self, log_entry: AuditLogEntry) -> None:
        raise NotImplementedError

    @abstractmethod
    def list_for_aggregate(self, aggregate_type: str, aggregate_id: str) -> list[AuditLogEntry]:
        raise NotImplementedError


class ArtifactRepository(ABC):
    @abstractmethod
    def upsert(self, artifact: ArtifactRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    def get(self, artifact_id: str) -> ArtifactRecord | None:
        raise NotImplementedError

    @abstractmethod
    def list_for_job(self, job_id: str) -> list[ArtifactRecord]:
        raise NotImplementedError

    @abstractmethod
    def list_for_owner(self, owner_type: str, owner_id: str) -> list[ArtifactRecord]:
        raise NotImplementedError

    @abstractmethod
    def list_expired(self, now: datetime) -> list[ArtifactRecord]:
        raise NotImplementedError


class VectorRepository(ABC):
    @abstractmethod
    def upsert(self, record: VectorRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    def list_by_namespace(self, namespace: str, limit: int = 1000, offset: int = 0) -> list[VectorRecord]:
        raise NotImplementedError

    @abstractmethod
    def count_by_namespace(self, namespace: str) -> int:
        raise NotImplementedError

    @abstractmethod
    def search(self, namespace: str, text: str, limit: int = 5) -> list[VectorRecord]:
        raise NotImplementedError
