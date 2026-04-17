from __future__ import annotations

import json
from dataclasses import dataclass

from domain.entities import Job


@dataclass(frozen=True)
class JobLifecycleMessage:
    schema_version: str
    event_type: str
    job_id: str
    song_name: str
    status: str
    stage: str | None
    retry_count: int
    progress: str
    result: str | None
    trace_id: str
    timestamp: str

    @classmethod
    def from_job(
        cls,
        job: Job,
        *,
        event_type: str,
        trace_id: str | None = None,
    ) -> JobLifecycleMessage:
        return cls(
            schema_version="v1",
            event_type=event_type,
            job_id=job.job_id,
            song_name=job.song_name,
            status=job.status.value,
            stage=job.current_stage.value if job.current_stage else None,
            retry_count=job.retry_count,
            progress=job.progress,
            result=job.result,
            trace_id=trace_id or job.job_id,
            timestamp=job.updated_at.isoformat(),
        )

    def to_payload(self) -> str:
        return json.dumps(
            {
                "schema_version": self.schema_version,
                "event_type": self.event_type,
                "job_id": self.job_id,
                "song_name": self.song_name,
                "status": self.status,
                "stage": self.stage,
                "retry_count": self.retry_count,
                "progress": self.progress,
                "result": self.result,
                "trace_id": self.trace_id,
                "timestamp": self.timestamp,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @classmethod
    def from_payload(cls, payload: str) -> JobLifecycleMessage:
        data = json.loads(payload)
        return cls(
            schema_version=data["schema_version"],
            event_type=data["event_type"],
            job_id=data["job_id"],
            song_name=data["song_name"],
            status=data["status"],
            stage=data.get("stage"),
            retry_count=data["retry_count"],
            progress=data["progress"],
            result=data.get("result"),
            trace_id=data["trace_id"],
            timestamp=data["timestamp"],
        )

    def compare_to_job(self, job: Job) -> list[str]:
        mismatches: list[str] = []
        expected_values = {
            "job_id": job.job_id,
            "song_name": job.song_name,
            "status": job.status.value,
            "stage": job.current_stage.value if job.current_stage else None,
            "retry_count": job.retry_count,
            "progress": job.progress,
            "result": job.result,
            "trace_id": job.job_id,
        }
        for field_name, expected in expected_values.items():
            if getattr(self, field_name) != expected:
                mismatches.append(field_name)
        return mismatches
