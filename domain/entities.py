from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from domain.enums import JobStatus, OutboxStatus, StageStatus, StageType
from domain.time_utils import utc_now


@dataclass
class Artist:
    spotify_id: str
    name: str
    yt_channel_id: Optional[str] = None
    status: str = "active"


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
