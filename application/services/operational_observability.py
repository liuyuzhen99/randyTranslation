from __future__ import annotations

from collections import defaultdict
from typing import Protocol

from domain.enums import ReviewStatus, StageStatus, SyncStatus
from domain.queue_topology import PipelineQueueTopology
from domain.time_utils import utc_now
from infrastructure.persistence.sqlalchemy_models import (
    ArtistSyncRunModel,
    PipelineStageExecutionModel,
    ReviewItemModel,
)
from infrastructure.persistence.sqlalchemy_repositories import SQLAlchemySessionFactory
from sqlalchemy import select


class QueueDepthCollector(Protocol):
    def collect_depths(self) -> dict[str, int]:
        ...


class OperationalObservabilityService:
    def __init__(
        self,
        *,
        session_factory: SQLAlchemySessionFactory,
        queue_depth_collector: QueueDepthCollector | None = None,
        topology: PipelineQueueTopology | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.queue_depth_collector = queue_depth_collector
        self.topology = topology or PipelineQueueTopology()

    def snapshot(self) -> dict:
        queue_depths = self._queue_depths()
        stage_metrics = self._stage_metrics()
        discovery_metrics = self._discovery_metrics()
        review_metrics = self._review_metrics()
        return {
            "queue_depth": queue_depths,
            "dlq_count": queue_depths.get(self.topology.dead_letter_queue, 0),
            "stage_latency_seconds": stage_metrics["latency_seconds"],
            "stage_status_counts": stage_metrics["status_counts"],
            "stage_success_failure_rate": stage_metrics["success_failure_rate"],
            "retry_count": stage_metrics["retry_count"],
            "discovery_freshness": discovery_metrics,
            "review_aging_seconds": review_metrics,
        }

    def _queue_depths(self) -> dict[str, int]:
        if self.queue_depth_collector is None:
            return {binding.queue_name: 0 for binding in self.topology.bindings()}
        return self.queue_depth_collector.collect_depths()

    def _stage_metrics(self) -> dict[str, dict]:
        latency_values: dict[str, list[float]] = defaultdict(list)
        status_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

        with self.session_factory.session_scope() as session:
            rows = (
                session.execute(select(PipelineStageExecutionModel))
                .scalars()
                .all()
            )

        for row in rows:
            stage = row.stage.value
            status = row.status.value
            status_counts[stage][status] += 1
            if (
                row.status == StageStatus.COMPLETED
                and row.locked_at is not None
                and row.completed_at is not None
            ):
                latency_values[stage].append(
                    max(0.0, (row.completed_at - row.locked_at).total_seconds())
                )

        latency_seconds = {
            stage: {
                "count": len(values),
                "avg": round(sum(values) / len(values), 3),
                "p95": round(_percentile(values, 95), 3),
            }
            for stage, values in latency_values.items()
            if values
        }

        return {
            "latency_seconds": latency_seconds,
            "status_counts": {
                stage: dict(counts)
                for stage, counts in status_counts.items()
            },
            "success_failure_rate": _success_failure_rate(status_counts),
            "retry_count": _retry_counts(rows),
        }

    def _discovery_metrics(self) -> dict:
        with self.session_factory.session_scope() as session:
            rows = (
                session.execute(select(ArtistSyncRunModel))
                .scalars()
                .all()
            )

        completed = [
            row.completed_at
            for row in rows
            if row.status == SyncStatus.COMPLETED and row.completed_at is not None
        ]
        failed = [row for row in rows if row.status == SyncStatus.FAILED]
        latest_completed_at = max(completed) if completed else None
        age_seconds = (
            max(0.0, (utc_now() - latest_completed_at).total_seconds())
            if latest_completed_at is not None
            else None
        )
        return {
            "latest_completed_at": latest_completed_at.isoformat() if latest_completed_at else None,
            "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
            "completed_count": len(completed),
            "failed_count": len(failed),
            "retry_count": sum(row.retry_count for row in rows),
        }

    def _review_metrics(self) -> dict:
        now = utc_now()
        with self.session_factory.session_scope() as session:
            rows = (
                session.execute(
                    select(ReviewItemModel).where(ReviewItemModel.status == ReviewStatus.PENDING)
                )
                .scalars()
                .all()
            )
        ages = [max(0.0, (now - row.created_at).total_seconds()) for row in rows]
        if not ages:
            return {"pending_count": 0, "oldest": 0.0, "avg": 0.0, "p95": 0.0}
        return {
            "pending_count": len(ages),
            "oldest": round(max(ages), 3),
            "avg": round(sum(ages) / len(ages), 3),
            "p95": round(_percentile(ages, 95), 3),
        }


def _percentile(values: list[float], percentile: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round((percentile / 100) * (len(ordered) - 1)))
    return ordered[index]


def _success_failure_rate(status_counts: dict[str, dict[str, int]]) -> dict[str, dict[str, float | int]]:
    rates: dict[str, dict[str, float | int]] = {}
    for stage, counts in status_counts.items():
        success = counts.get(StageStatus.COMPLETED.value, 0)
        failure = counts.get(StageStatus.FAILED.value, 0) + counts.get(StageStatus.DLQ.value, 0)
        total = sum(counts.values())
        rates[stage] = {
            "total": total,
            "success": success,
            "failure": failure,
            "success_rate": round(success / total, 4) if total else 0.0,
            "failure_rate": round(failure / total, 4) if total else 0.0,
        }
    return rates


def _retry_counts(rows: list[PipelineStageExecutionModel]) -> dict[str, int]:
    retry_counts: dict[str, int] = defaultdict(int)
    for row in rows:
        retry_counts[row.stage.value] += max(0, row.attempt - 1)
    return dict(retry_counts)
