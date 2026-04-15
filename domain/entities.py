from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from domain.enums import JobStatus


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
    processed_status: str = "new"
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
    status: str = "raw"


@dataclass
class OutboxEvent:
    event_id: str
    topic: str
    payload: str
    status: str = "pending"


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

    def to_api_dict(self) -> dict:
        return {
            "status": self.status.value,
            "progress": self.progress,
            "result": self.result,
            "song_name": self.song_name,
        }
