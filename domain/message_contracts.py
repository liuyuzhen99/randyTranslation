from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from domain.entities import Job
from domain.enums import StageType
from domain.time_utils import utc_now


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


@dataclass(frozen=True)
class RetryContext:
    attempt: int
    max_attempts: int
    backoff_seconds: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "backoff_seconds": self.backoff_seconds,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RetryContext:
        return cls(
            attempt=int(payload.get("attempt", 0)),
            max_attempts=int(payload.get("max_attempts", 3)),
            backoff_seconds=int(payload.get("backoff_seconds", 0)),
        )


@dataclass(frozen=True)
class ReviewContext:
    candidate_id: str | None = None
    review_id: str | None = None
    review_type: str | None = None
    expected_version: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "review_id": self.review_id,
            "review_type": self.review_type,
            "expected_version": self.expected_version,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> ReviewContext:
        payload = payload or {}
        return cls(
            candidate_id=payload.get("candidate_id"),
            review_id=payload.get("review_id"),
            review_type=payload.get("review_type"),
            expected_version=payload.get("expected_version"),
        )


@dataclass(frozen=True)
class PipelineStageMessage:
    schema_version: str
    message_type: str
    job_id: str
    stage: StageType
    song_name: str
    dedupe_key: str
    trace_id: str
    retry: RetryContext
    review: ReviewContext
    created_at: str
    payload: dict[str, Any]

    @classmethod
    def build(
        cls,
        *,
        message_type: str,
        job_id: str,
        stage: StageType,
        song_name: str,
        trace_id: str | None = None,
        attempt: int = 0,
        max_attempts: int = 3,
        backoff_seconds: int = 0,
        review: ReviewContext | None = None,
        payload: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> PipelineStageMessage:
        return cls(
            schema_version="v1",
            message_type=message_type,
            job_id=job_id,
            stage=stage,
            song_name=song_name,
            dedupe_key=f"pipeline:{job_id}:{stage.value}:attempt:{attempt}",
            trace_id=trace_id or job_id,
            retry=RetryContext(
                attempt=attempt,
                max_attempts=max_attempts,
                backoff_seconds=backoff_seconds,
            ),
            review=review or ReviewContext(),
            created_at=created_at or utc_now().isoformat(),
            payload=payload or {},
        )

    def to_payload(self) -> str:
        return json.dumps(
            {
                "schema_version": self.schema_version,
                "message_type": self.message_type,
                "job": {
                    "job_id": self.job_id,
                    "song_name": self.song_name,
                },
                "stage": self.stage.value,
                "dedupe_key": self.dedupe_key,
                "trace_id": self.trace_id,
                "retry": self.retry.to_dict(),
                "review": self.review.to_dict(),
                "created_at": self.created_at,
                "payload": self.payload,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @classmethod
    def from_payload(cls, payload: str) -> PipelineStageMessage:
        data = json.loads(payload)
        job_data = data["job"]
        return cls(
            schema_version=data["schema_version"],
            message_type=data["message_type"],
            job_id=job_data["job_id"],
            stage=StageType(data["stage"]),
            song_name=job_data["song_name"],
            dedupe_key=data["dedupe_key"],
            trace_id=data["trace_id"],
            retry=RetryContext.from_dict(data["retry"]),
            review=ReviewContext.from_dict(data.get("review")),
            created_at=data["created_at"],
            payload=data.get("payload") or {},
        )
