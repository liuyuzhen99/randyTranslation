from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from domain.enums import (
    CandidateStatus,
    JobStatus,
    OutboxStatus,
    ReviewStatus,
    ReviewType,
    StageStatus,
    StageType,
    SyncStatus,
)
from domain.time_utils import utc_now


@dataclass
class Artist:
    spotify_id: str
    name: str
    yt_channel_id: Optional[str] = None
    status: str = "active"
    sync_status: SyncStatus = SyncStatus.PENDING
    last_sync_started_at: Optional[datetime] = None
    last_sync_completed_at: Optional[datetime] = None
    last_sync_error: Optional[str] = None
    last_channel_resolved_at: Optional[datetime] = None
    last_discovery_at: Optional[datetime] = None


@dataclass
class Video:
    video_id: str
    spotify_id: Optional[str]
    title: str
    published_at: Optional[datetime] = None
    processed_status: StageStatus = StageStatus.PENDING
    local_video_path: Optional[str] = None
    srt_path: Optional[str] = None
    final_video_path: Optional[str] = None


@dataclass
class Subtitle:
    video_id: str
    line_index: int
    start_time: float
    end_time: float
    en_text: str
    zh_text: Optional[str] = None
    status: StageStatus = StageStatus.PENDING


@dataclass
class OutboxEvent:
    event_id: str
    topic: str
    payload: str
    status: OutboxStatus = OutboxStatus.PENDING
    aggregate_id: Optional[str] = None
    dedupe_key: Optional[str] = None
    correlation_id: Optional[str] = None


@dataclass
class ArtistSyncRun:
    run_id: str
    spotify_id: Optional[str]
    source_kind: str
    status: SyncStatus = SyncStatus.PENDING
    started_at: datetime = field(default_factory=utc_now)
    completed_at: Optional[datetime] = None
    failure_reason: Optional[str] = None
    retry_count: int = 0
    discovered_count: int = 0
    trigger: str = "system"


@dataclass
class VideoCandidate:
    candidate_id: str
    spotify_id: str
    video_id: str
    channel_id: Optional[str]
    title: str
    source_url: str
    source_kind: str = "youtube_rss"
    status: CandidateStatus = CandidateStatus.PENDING_REVIEW
    ingestion_status: SyncStatus = SyncStatus.COMPLETED
    published_at: Optional[datetime] = None
    first_seen_at: datetime = field(default_factory=utc_now)
    last_seen_at: datetime = field(default_factory=utc_now)
    discovery_run_id: Optional[str] = None
    failure_reason: Optional[str] = None


@dataclass
class ReviewItem:
    review_id: str
    subject_kind: str
    subject_id: str
    spotify_id: str
    review_type: ReviewType
    status: ReviewStatus = ReviewStatus.PENDING
    version: int = 1
    decision_comment: Optional[str] = None
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass
class AuditLogEntry:
    log_id: str
    aggregate_type: str
    aggregate_id: str
    action: str
    actor_id: str
    details: Optional[str] = None
    created_at: datetime = field(default_factory=utc_now)


@dataclass
class ArtifactRecord:
    artifact_id: str
    owner_type: str
    owner_id: str
    artifact_type: str
    object_uri: str
    object_key: str
    bucket: str
    storage_provider: str
    content_type: Optional[str] = None
    job_id: Optional[str] = None
    candidate_id: Optional[str] = None
    size_bytes: int = 0
    checksum_sha256: str = ""
    lifecycle_status: str = "ready"
    version: int = 1
    metadata: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    expires_at: Optional[datetime] = None


@dataclass
class JobEvent:
    event_id: str
    job_id: str
    from_status: Optional[JobStatus]
    to_status: JobStatus
    stage: Optional[StageType] = None
    message: str = ""
    retry_count: int = 0
    created_at: datetime = field(default_factory=utc_now)


@dataclass
class PipelineStageExecution:
    execution_id: str
    dedupe_key: str
    job_id: str
    stage: StageType
    candidate_id: Optional[str] = None
    status: StageStatus = StageStatus.PENDING
    attempt: int = 0
    max_attempts: int = 3
    next_retry_at: Optional[datetime] = None
    locked_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    result_payload: Optional[str] = None
    trace_id: Optional[str] = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass
class VectorRecord:
    vector_id: str
    namespace: str
    text: str
    metadata: dict = field(default_factory=dict)


@dataclass
class Job:
    job_id: str
    song_name: str
    status: JobStatus = JobStatus.PENDING
    progress: str = "已加入队列"
    result: Optional[str] = None
    current_stage: Optional[StageType] = None
    retry_count: int = 0
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def to_api_dict(self) -> dict:
        return {
            "status": self.status.value,
            "progress": self.progress,
            "result": self.result,
            "song_name": self.song_name,
        }
