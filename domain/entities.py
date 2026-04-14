from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class TranslationJob:
    job_id: str
    song_name: str
    status: str = "queued"
    progress: int = 0
    result: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "progress": self.progress,
            "result": self.result,
            "song_name": self.song_name,
        }
