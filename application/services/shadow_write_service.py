from __future__ import annotations

import logging
import uuid

from domain.entities import Job
from domain.message_contracts import JobLifecycleMessage
from domain.enums import OutboxStatus
from domain.job_lifecycle import validate_job_transition
from infrastructure.persistence.sqlalchemy_models import JobEventModel, JobModel, OutboxModel
from infrastructure.persistence.sqlalchemy_repositories import SQLAlchemySessionFactory

logger = logging.getLogger(__name__)


class ShadowWriteService:
    """Shadow-write selected job flow records into the SQLAlchemy schema."""

    def __init__(self, session_factory: SQLAlchemySessionFactory) -> None:
        self.session_factory = session_factory

    def record_job_created(self, job: Job) -> None:
        with self.session_factory.session_scope() as session:
            existing = session.get(JobModel, job.job_id)
            if existing is None:
                session.add(self._build_job_model(job))

            self._enqueue_job_event(
                session=session,
                job=job,
                from_status=None,
                message="job created",
            )
            self._enqueue_outbox(
                session=session,
                job=job,
                dedupe_key=f"job:{job.job_id}:created",
                event_type="job.created",
            )

    def record_job_update(self, previous_job: Job, current_job: Job) -> None:
        with self.session_factory.session_scope() as session:
            stored_job = session.get(JobModel, current_job.job_id)
            if stored_job is None:
                session.add(self._build_job_model(current_job))
            else:
                validate_job_transition(
                    current_status=stored_job.status,
                    next_status=current_job.status,
                    retry_count=stored_job.retry_count,
                )
                stored_job.song_name = current_job.song_name
                stored_job.status = current_job.status
                stored_job.progress = current_job.progress
                stored_job.result = current_job.result
                stored_job.current_stage = current_job.current_stage
                stored_job.retry_count = current_job.retry_count
                stored_job.created_at = current_job.created_at
                stored_job.updated_at = current_job.updated_at

            if (
                previous_job.status != current_job.status
                or previous_job.current_stage != current_job.current_stage
                or previous_job.progress != current_job.progress
                or previous_job.result != current_job.result
            ):
                self._enqueue_job_event(
                    session=session,
                    job=current_job,
                    from_status=previous_job.status,
                    message=current_job.progress,
                )

            if previous_job.status != current_job.status or previous_job.current_stage != current_job.current_stage:
                dedupe_key = (
                    f"job:{current_job.job_id}:status:{current_job.status.value}:"
                    f"stage:{current_job.current_stage.value if current_job.current_stage else 'none'}:"
                    f"retry:{current_job.retry_count}"
                )
                self._enqueue_outbox(
                    session=session,
                    job=current_job,
                    dedupe_key=dedupe_key,
                    event_type="job.transition",
                )

    @staticmethod
    def _build_job_model(job: Job) -> JobModel:
        return JobModel(
            job_id=job.job_id,
            song_name=job.song_name,
            status=job.status,
            progress=job.progress,
            result=job.result,
            current_stage=job.current_stage,
            retry_count=job.retry_count,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )

    def _enqueue_job_event(
        self,
        *,
        session,
        job: Job,
        from_status,
        message: str,
    ) -> None:
        session.add(
            JobEventModel(
                event_id=uuid.uuid4().hex,
                job_id=job.job_id,
                from_status=from_status,
                to_status=job.status,
                stage=job.current_stage,
                message=message,
                retry_count=job.retry_count,
                created_at=job.updated_at,
            )
        )

    def _enqueue_outbox(
        self,
        *,
        session,
        job: Job,
        dedupe_key: str,
        event_type: str,
    ) -> None:
        existing = session.query(OutboxModel).filter_by(dedupe_key=dedupe_key).first()
        if existing is not None:
            logger.info("Skipping duplicate outbox shadow-write event for %s", dedupe_key)
            return

        payload = JobLifecycleMessage.from_job(
            job,
            event_type=event_type,
            trace_id=job.job_id,
        ).to_payload()
        session.add(
            OutboxModel(
                event_id=uuid.uuid4().hex,
                topic="job.lifecycle",
                payload=payload,
                status=OutboxStatus.PENDING,
                aggregate_id=job.job_id,
                dedupe_key=dedupe_key,
                correlation_id=job.job_id,
            )
        )
