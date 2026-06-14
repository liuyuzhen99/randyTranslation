from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from domain.message_contracts import JobLifecycleMessage
from domain.repositories import JobRepository
from domain.time_utils import utc_now
from infrastructure.persistence.sqlalchemy_repositories import (
    SQLAlchemyJobEventRepository,
    SQLAlchemyJobRepository,
    SQLAlchemyOutboxRepository,
    SQLAlchemySessionFactory,
)


@dataclass(frozen=True)
class DualWriteReconcileThresholds:
    max_missing_jobs: int = 0
    max_job_field_mismatches: int = 0
    max_invalid_outbox_payloads: int = 0
    max_outbox_payload_mismatches: int = 0


@dataclass
class DualWriteReconcileReport:
    generated_at: str
    primary_job_count: int
    shadow_job_count: int
    pending_outbox_count: int
    shadow_job_event_count: int
    thresholds: DualWriteReconcileThresholds = field(default_factory=DualWriteReconcileThresholds)
    missing_job_ids_in_shadow: list[str] = field(default_factory=list)
    mismatched_job_fields: dict[str, list[str]] = field(default_factory=dict)
    invalid_outbox_event_ids: list[str] = field(default_factory=list)
    mismatched_outbox_payloads: dict[str, list[str]] = field(default_factory=dict)

    @property
    def is_consistent(self) -> bool:
        return (
            not self.missing_job_ids_in_shadow
            and not self.mismatched_job_fields
            and not self.invalid_outbox_event_ids
            and not self.mismatched_outbox_payloads
        )

    @property
    def is_within_threshold(self) -> bool:
        return (
            len(self.missing_job_ids_in_shadow) <= self.thresholds.max_missing_jobs
            and len(self.mismatched_job_fields) <= self.thresholds.max_job_field_mismatches
            and len(self.invalid_outbox_event_ids) <= self.thresholds.max_invalid_outbox_payloads
            and len(self.mismatched_outbox_payloads) <= self.thresholds.max_outbox_payload_mismatches
        )

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "primary_job_count": self.primary_job_count,
            "shadow_job_count": self.shadow_job_count,
            "pending_outbox_count": self.pending_outbox_count,
            "shadow_job_event_count": self.shadow_job_event_count,
            "thresholds": {
                "max_missing_jobs": self.thresholds.max_missing_jobs,
                "max_job_field_mismatches": self.thresholds.max_job_field_mismatches,
                "max_invalid_outbox_payloads": self.thresholds.max_invalid_outbox_payloads,
                "max_outbox_payload_mismatches": self.thresholds.max_outbox_payload_mismatches,
            },
            "missing_job_ids_in_shadow": self.missing_job_ids_in_shadow,
            "mismatched_job_fields": self.mismatched_job_fields,
            "invalid_outbox_event_ids": self.invalid_outbox_event_ids,
            "mismatched_outbox_payloads": self.mismatched_outbox_payloads,
            "is_consistent": self.is_consistent,
            "is_within_threshold": self.is_within_threshold,
        }


class DualWriteReconcileService:
    """Compare primary runtime job state with the SQLAlchemy shadow-write database."""

    def __init__(
        self,
        primary_job_repository: JobRepository,
        session_factory: SQLAlchemySessionFactory,
        thresholds: DualWriteReconcileThresholds | None = None,
    ) -> None:
        self.primary_job_repository = primary_job_repository
        self.shadow_job_repository = SQLAlchemyJobRepository(session_factory)
        self.shadow_job_event_repository = SQLAlchemyJobEventRepository(session_factory)
        self.shadow_outbox_repository = SQLAlchemyOutboxRepository(session_factory)
        self.thresholds = thresholds or DualWriteReconcileThresholds()

    def generate_report(self) -> DualWriteReconcileReport:
        primary_jobs = self.primary_job_repository.list_all()
        shadow_jobs = self.shadow_job_repository.list_all()

        missing_job_ids = sorted(set(primary_jobs) - set(shadow_jobs))
        mismatched_job_fields: dict[str, list[str]] = {}
        invalid_outbox_event_ids: list[str] = []
        mismatched_outbox_payloads: dict[str, list[str]] = {}

        for job_id, primary_job in primary_jobs.items():
            shadow_job = shadow_jobs.get(job_id)
            if shadow_job is None:
                continue

            mismatches: list[str] = []
            for field_name in ("song_name", "status", "progress", "result", "current_stage", "retry_count"):
                if getattr(primary_job, field_name) != getattr(shadow_job, field_name):
                    mismatches.append(field_name)
            if mismatches:
                mismatched_job_fields[job_id] = mismatches

        shadow_job_event_count = sum(
            len(self.shadow_job_event_repository.list_for_job(job_id))
            for job_id in shadow_jobs
        )
        for event in self.shadow_outbox_repository.list_pending():
            correlation_id = event.correlation_id or event.aggregate_id or event.event_id
            primary_job = primary_jobs.get(correlation_id)
            if primary_job is None:
                continue
            try:
                message = JobLifecycleMessage.from_payload(event.payload)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                invalid_outbox_event_ids.append(event.event_id)
                continue

            mismatches = message.compare_to_job(primary_job)
            if mismatches:
                mismatched_outbox_payloads[event.event_id] = mismatches

        return DualWriteReconcileReport(
            generated_at=utc_now().isoformat(),
            primary_job_count=len(primary_jobs),
            shadow_job_count=len(shadow_jobs),
            pending_outbox_count=len(self.shadow_outbox_repository.list_pending()),
            shadow_job_event_count=shadow_job_event_count,
            thresholds=self.thresholds,
            missing_job_ids_in_shadow=missing_job_ids,
            mismatched_job_fields=mismatched_job_fields,
            invalid_outbox_event_ids=invalid_outbox_event_ids,
            mismatched_outbox_payloads=mismatched_outbox_payloads,
        )

    def write_report(self, report_path: str) -> DualWriteReconcileReport:
        report = self.generate_report()
        path = Path(report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return report
