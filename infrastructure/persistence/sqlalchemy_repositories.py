from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

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
    Video,
    VideoCandidate,
)
from domain.job_lifecycle import validate_job_transition
from domain.enums import StageStatus
from domain.time_utils import utc_now
from domain.repositories import (
    ArtifactRepository,
    AuditLogRepository,
    ArtistRepository,
    ArtistSyncRunRepository,
    CandidateRepository,
    JobEventRepository,
    JobRepository,
    OutboxRepository,
    PipelineStageExecutionRepository,
    ReviewRepository,
    SubtitleRepository,
    VideoRepository,
)
from infrastructure.persistence.sqlalchemy_models import (
    ArtifactModel,
    AuditLogEntryModel,
    ArtistModel,
    ArtistSyncRunModel,
    Base,
    JobEventModel,
    JobModel,
    OutboxModel,
    PipelineStageExecutionModel,
    ReviewItemModel,
    SubtitleModel,
    VideoModel,
    VideoCandidateModel,
)


class SQLAlchemySessionFactory:
    """Small session wrapper so repositories share one engine/config entry point."""

    def __init__(self, database_url: str) -> None:
        connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
        self.engine: Engine = create_engine(database_url, future=True, connect_args=connect_args)
        self._sessionmaker = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session_scope(self):
        session: Session = self._sessionmaker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @contextmanager
    def transactional(self):
        """Yield a session for use across multiple repository calls in one transaction."""
        session: Session = self._sessionmaker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


class SQLAlchemyArtistRepository(ArtistRepository):
    def __init__(self, session_factory: SQLAlchemySessionFactory) -> None:
        self.session_factory = session_factory

    def upsert(self, artist: Artist) -> None:
        with self.session_factory.session_scope() as session:
            existing = session.get(ArtistModel, artist.spotify_id)
            if existing is None:
                existing = ArtistModel(
                    spotify_id=artist.spotify_id,
                    name=artist.name,
                    yt_channel_id=artist.yt_channel_id,
                    status=artist.status,
                    sync_status=artist.sync_status,
                    last_sync_started_at=artist.last_sync_started_at,
                    last_sync_completed_at=artist.last_sync_completed_at,
                    last_sync_error=artist.last_sync_error,
                    last_channel_resolved_at=artist.last_channel_resolved_at,
                    last_discovery_at=artist.last_discovery_at,
                )
                session.add(existing)
                return

            existing.name = artist.name
            existing.yt_channel_id = artist.yt_channel_id
            existing.status = artist.status
            existing.sync_status = artist.sync_status
            existing.last_sync_started_at = artist.last_sync_started_at
            existing.last_sync_completed_at = artist.last_sync_completed_at
            existing.last_sync_error = artist.last_sync_error
            existing.last_channel_resolved_at = artist.last_channel_resolved_at
            existing.last_discovery_at = artist.last_discovery_at

    def get(self, spotify_id: str) -> Artist | None:
        with self.session_factory.session_scope() as session:
            artist = session.get(ArtistModel, spotify_id)
            if artist is None:
                return None
            return self._to_entity(artist)

    def list_all(self) -> list[Artist]:
        with self.session_factory.session_scope() as session:
            rows = session.execute(select(ArtistModel).order_by(ArtistModel.name.asc())).scalars().all()
            return [self._to_entity(row) for row in rows]

    @staticmethod
    def _to_entity(row: ArtistModel) -> Artist:
        return Artist(
            spotify_id=row.spotify_id,
            name=row.name,
            yt_channel_id=row.yt_channel_id,
            status=row.status,
            sync_status=row.sync_status,
            last_sync_started_at=row.last_sync_started_at,
            last_sync_completed_at=row.last_sync_completed_at,
            last_sync_error=row.last_sync_error,
            last_channel_resolved_at=row.last_channel_resolved_at,
            last_discovery_at=row.last_discovery_at,
        )


class SQLAlchemyVideoRepository(VideoRepository):
    def __init__(self, session_factory: SQLAlchemySessionFactory) -> None:
        self.session_factory = session_factory

    def upsert(self, video: Video) -> None:
        with self.session_factory.session_scope() as session:
            existing = session.get(VideoModel, video.video_id)
            if existing is None:
                existing = VideoModel(
                    video_id=video.video_id,
                    spotify_id=video.spotify_id,
                    title=video.title,
                    published_at=video.published_at,
                    processed_status=video.processed_status,
                    local_video_path=video.local_video_path,
                    srt_path=video.srt_path,
                    final_video_path=video.final_video_path,
                )
                session.add(existing)
                return

            existing.spotify_id = video.spotify_id
            existing.title = video.title
            existing.published_at = video.published_at
            existing.processed_status = video.processed_status
            existing.local_video_path = video.local_video_path
            existing.srt_path = video.srt_path
            existing.final_video_path = video.final_video_path

    def get(self, video_id: str) -> Video | None:
        with self.session_factory.session_scope() as session:
            video = session.get(VideoModel, video_id)
            if video is None:
                return None
            return Video(
                video_id=video.video_id,
                spotify_id=video.spotify_id,
                title=video.title,
                published_at=video.published_at,
                processed_status=video.processed_status,
                local_video_path=video.local_video_path,
                srt_path=video.srt_path,
                final_video_path=video.final_video_path,
            )


class SQLAlchemySubtitleRepository(SubtitleRepository):
    def __init__(self, session_factory: SQLAlchemySessionFactory) -> None:
        self.session_factory = session_factory

    def replace_for_video(self, video_id: str, subtitles: list[Subtitle]) -> None:
        with self.session_factory.session_scope() as session:
            session.query(SubtitleModel).filter_by(video_id=video_id).delete()
            for subtitle in subtitles:
                session.add(
                    SubtitleModel(
                        video_id=subtitle.video_id,
                        line_index=subtitle.line_index,
                        start_time=subtitle.start_time,
                        end_time=subtitle.end_time,
                        en_text=subtitle.en_text,
                        zh_text=subtitle.zh_text,
                        status=subtitle.status,
                    )
                )

    def list_for_video(self, video_id: str) -> list[Subtitle]:
        with self.session_factory.session_scope() as session:
            rows = (
                session.execute(
                    select(SubtitleModel)
                    .where(SubtitleModel.video_id == video_id)
                    .order_by(SubtitleModel.line_index.asc())
                )
                .scalars()
                .all()
            )
            return [
                Subtitle(
                    video_id=row.video_id,
                    line_index=row.line_index,
                    start_time=row.start_time,
                    end_time=row.end_time,
                    en_text=row.en_text,
                    zh_text=row.zh_text,
                    status=row.status,
                )
                for row in rows
            ]


class SQLAlchemyJobRepository(JobRepository):
    def __init__(self, session_factory: SQLAlchemySessionFactory) -> None:
        self.session_factory = session_factory

    def create(self, job: Job) -> None:
        with self.session_factory.session_scope() as session:
            session.add(
                JobModel(
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
            )

    def get(self, job_id: str) -> Job | None:
        with self.session_factory.session_scope() as session:
            row = session.get(JobModel, job_id)
            if row is None:
                return None
            return self._to_entity(row)

    def update(self, job: Job) -> None:
        with self.session_factory.session_scope() as session:
            row = session.get(JobModel, job.job_id)
            if row is None:
                self.create(job)
                return

            validate_job_transition(
                current_status=row.status,
                next_status=job.status,
                retry_count=row.retry_count,
            )
            row.song_name = job.song_name
            row.status = job.status
            row.progress = job.progress
            row.result = job.result
            row.current_stage = job.current_stage
            row.retry_count = job.retry_count
            row.updated_at = job.updated_at or utc_now()

    def list_all(self) -> dict[str, Job]:
        with self.session_factory.session_scope() as session:
            rows = session.execute(select(JobModel)).scalars().all()
            return {row.job_id: self._to_entity(row) for row in rows}

    @staticmethod
    def _to_entity(row: JobModel) -> Job:
        return Job(
            job_id=row.job_id,
            song_name=row.song_name,
            status=row.status,
            progress=row.progress,
            result=row.result,
            current_stage=row.current_stage,
            retry_count=row.retry_count,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class SQLAlchemyJobEventRepository(JobEventRepository):
    def __init__(self, session_factory: SQLAlchemySessionFactory) -> None:
        self.session_factory = session_factory

    def add(self, event: JobEvent) -> None:
        with self.session_factory.session_scope() as session:
            session.add(
                JobEventModel(
                    event_id=event.event_id,
                    job_id=event.job_id,
                    from_status=event.from_status,
                    to_status=event.to_status,
                    stage=event.stage,
                    message=event.message,
                    retry_count=event.retry_count,
                    created_at=event.created_at,
                )
            )

    def list_for_job(self, job_id: str) -> list[JobEvent]:
        with self.session_factory.session_scope() as session:
            rows = (
                session.execute(
                    select(JobEventModel)
                    .where(JobEventModel.job_id == job_id)
                    .order_by(JobEventModel.created_at.asc())
                )
                .scalars()
                .all()
            )
            return [
                JobEvent(
                    event_id=row.event_id,
                    job_id=row.job_id,
                    from_status=row.from_status,
                    to_status=row.to_status,
                    stage=row.stage,
                    message=row.message,
                    retry_count=row.retry_count,
                    created_at=row.created_at,
                )
                for row in rows
            ]


class SQLAlchemyPipelineStageExecutionRepository(PipelineStageExecutionRepository):
    def __init__(self, session_factory: SQLAlchemySessionFactory) -> None:
        self.session_factory = session_factory

    def upsert(self, execution: PipelineStageExecution) -> None:
        with self.session_factory.session_scope() as session:
            self._upsert_with_session(session, execution)

    def upsert_with_session(self, session, execution: PipelineStageExecution) -> None:
        """Upsert using a caller-supplied session (for shared transactions)."""
        self._upsert_with_session(session, execution)

    def _upsert_with_session(self, session, execution: PipelineStageExecution) -> None:
        row = session.get(PipelineStageExecutionModel, execution.execution_id)
        if row is None:
            row = (
                session.execute(
                    select(PipelineStageExecutionModel).where(
                        PipelineStageExecutionModel.dedupe_key == execution.dedupe_key
                    )
                )
                .scalars()
                .first()
            )

        if row is None:
            session.add(
                PipelineStageExecutionModel(
                    execution_id=execution.execution_id,
                    dedupe_key=execution.dedupe_key,
                    job_id=execution.job_id,
                    stage=execution.stage,
                    candidate_id=execution.candidate_id,
                    status=execution.status,
                    attempt=execution.attempt,
                    max_attempts=execution.max_attempts,
                    next_retry_at=execution.next_retry_at,
                    locked_at=execution.locked_at,
                    completed_at=execution.completed_at,
                    error_message=execution.error_message,
                    result_payload=execution.result_payload,
                    trace_id=execution.trace_id,
                    created_at=execution.created_at,
                    updated_at=execution.updated_at,
                )
            )
            return

        row.job_id = execution.job_id
        row.stage = execution.stage
        row.candidate_id = execution.candidate_id
        row.status = execution.status
        row.attempt = execution.attempt
        row.max_attempts = execution.max_attempts
        row.next_retry_at = execution.next_retry_at
        row.locked_at = execution.locked_at
        row.completed_at = execution.completed_at
        row.error_message = execution.error_message
        row.result_payload = execution.result_payload
        row.trace_id = execution.trace_id
        row.updated_at = execution.updated_at

    def get_by_dedupe_key(self, dedupe_key: str) -> PipelineStageExecution | None:
        with self.session_factory.session_scope() as session:
            row = (
                session.execute(
                    select(PipelineStageExecutionModel).where(
                        PipelineStageExecutionModel.dedupe_key == dedupe_key
                    )
                )
                .scalars()
                .first()
            )
            return self._to_entity(row) if row is not None else None

    def list_for_job(self, job_id: str) -> list[PipelineStageExecution]:
        with self.session_factory.session_scope() as session:
            rows = (
                session.execute(
                    select(PipelineStageExecutionModel)
                    .where(PipelineStageExecutionModel.job_id == job_id)
                    .order_by(PipelineStageExecutionModel.created_at.asc())
                )
                .scalars()
                .all()
            )
            return [self._to_entity(row) for row in rows]

    def list_for_candidate(self, candidate_id: str) -> list[PipelineStageExecution]:
        with self.session_factory.session_scope() as session:
            rows = (
                session.execute(
                    select(PipelineStageExecutionModel)
                    .where(PipelineStageExecutionModel.candidate_id == candidate_id)
                    .order_by(PipelineStageExecutionModel.created_at.asc())
                )
                .scalars()
                .all()
            )
            return [self._to_entity(row) for row in rows]

    def list_due_retries(self, now: datetime, limit: int = 100) -> list[PipelineStageExecution]:
        with self.session_factory.session_scope() as session:
            rows = (
                session.execute(
                    select(PipelineStageExecutionModel)
                    .where(PipelineStageExecutionModel.status == StageStatus.RETRY_SCHEDULED)
                    .where(PipelineStageExecutionModel.next_retry_at.is_not(None))
                    .where(PipelineStageExecutionModel.next_retry_at <= now)
                    .order_by(PipelineStageExecutionModel.next_retry_at.asc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [self._to_entity(row) for row in rows]

    @staticmethod
    def _to_entity(row: PipelineStageExecutionModel) -> PipelineStageExecution:
        return PipelineStageExecution(
            execution_id=row.execution_id,
            dedupe_key=row.dedupe_key,
            job_id=row.job_id,
            stage=row.stage,
            candidate_id=row.candidate_id,
            status=row.status,
            attempt=row.attempt,
            max_attempts=row.max_attempts,
            next_retry_at=row.next_retry_at,
            locked_at=row.locked_at,
            completed_at=row.completed_at,
            error_message=row.error_message,
            result_payload=row.result_payload,
            trace_id=row.trace_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class SQLAlchemyArtistSyncRunRepository(ArtistSyncRunRepository):
    def __init__(self, session_factory: SQLAlchemySessionFactory) -> None:
        self.session_factory = session_factory

    def create(self, run: ArtistSyncRun) -> None:
        with self.session_factory.session_scope() as session:
            session.add(
                ArtistSyncRunModel(
                    run_id=run.run_id,
                    spotify_id=run.spotify_id,
                    source_kind=run.source_kind,
                    status=run.status,
                    started_at=run.started_at,
                    completed_at=run.completed_at,
                    failure_reason=run.failure_reason,
                    retry_count=run.retry_count,
                    discovered_count=run.discovered_count,
                    trigger=run.trigger,
                )
            )

    def update(self, run: ArtistSyncRun) -> None:
        with self.session_factory.session_scope() as session:
            row = session.get(ArtistSyncRunModel, run.run_id)
            if row is None:
                self.create(run)
                return

            row.spotify_id = run.spotify_id
            row.source_kind = run.source_kind
            row.status = run.status
            row.started_at = run.started_at
            row.completed_at = run.completed_at
            row.failure_reason = run.failure_reason
            row.retry_count = run.retry_count
            row.discovered_count = run.discovered_count
            row.trigger = run.trigger

    def get(self, run_id: str) -> ArtistSyncRun | None:
        with self.session_factory.session_scope() as session:
            row = session.get(ArtistSyncRunModel, run_id)
            return self._to_entity(row) if row is not None else None

    def list_for_artist(self, spotify_id: str) -> list[ArtistSyncRun]:
        with self.session_factory.session_scope() as session:
            rows = (
                session.execute(
                    select(ArtistSyncRunModel)
                    .where(ArtistSyncRunModel.spotify_id == spotify_id)
                    .order_by(ArtistSyncRunModel.started_at.desc())
                )
                .scalars()
                .all()
            )
            return [self._to_entity(row) for row in rows]

    @staticmethod
    def _to_entity(row: ArtistSyncRunModel) -> ArtistSyncRun:
        return ArtistSyncRun(
            run_id=row.run_id,
            spotify_id=row.spotify_id,
            source_kind=row.source_kind,
            status=row.status,
            started_at=row.started_at,
            completed_at=row.completed_at,
            failure_reason=row.failure_reason,
            retry_count=row.retry_count,
            discovered_count=row.discovered_count,
            trigger=row.trigger,
        )


class SQLAlchemyCandidateRepository(CandidateRepository):
    def __init__(self, session_factory: SQLAlchemySessionFactory) -> None:
        self.session_factory = session_factory

    def upsert(self, candidate: VideoCandidate) -> None:
        with self.session_factory.session_scope() as session:
            existing = session.get(VideoCandidateModel, candidate.candidate_id)
            if existing is None:
                existing = (
                    session.execute(
                        select(VideoCandidateModel).where(
                            VideoCandidateModel.spotify_id == candidate.spotify_id,
                            VideoCandidateModel.video_id == candidate.video_id,
                        )
                    )
                    .scalars()
                    .first()
                )

            if existing is None:
                session.add(
                    VideoCandidateModel(
                        candidate_id=candidate.candidate_id,
                        spotify_id=candidate.spotify_id,
                        video_id=candidate.video_id,
                        channel_id=candidate.channel_id,
                        title=candidate.title,
                        source_url=candidate.source_url,
                        source_kind=candidate.source_kind,
                        status=candidate.status,
                        ingestion_status=candidate.ingestion_status,
                        published_at=candidate.published_at,
                        first_seen_at=candidate.first_seen_at,
                        last_seen_at=candidate.last_seen_at,
                        discovery_run_id=candidate.discovery_run_id,
                        failure_reason=candidate.failure_reason,
                    )
                )
                return

            existing.channel_id = candidate.channel_id
            existing.title = candidate.title
            existing.source_url = candidate.source_url
            existing.source_kind = candidate.source_kind
            existing.status = candidate.status
            existing.ingestion_status = candidate.ingestion_status
            existing.published_at = candidate.published_at
            existing.last_seen_at = candidate.last_seen_at
            existing.discovery_run_id = candidate.discovery_run_id
            existing.failure_reason = candidate.failure_reason

    def get(self, candidate_id: str) -> VideoCandidate | None:
        with self.session_factory.session_scope() as session:
            row = session.get(VideoCandidateModel, candidate_id)
            return self._to_entity(row) if row is not None else None

    def list_for_artist(self, spotify_id: str) -> list[VideoCandidate]:
        with self.session_factory.session_scope() as session:
            rows = (
                session.execute(
                    select(VideoCandidateModel)
                    .where(VideoCandidateModel.spotify_id == spotify_id)
                    .order_by(
                        VideoCandidateModel.published_at.desc().nullslast(),
                        VideoCandidateModel.last_seen_at.desc(),
                    )
                )
                .scalars()
                .all()
            )
            return [self._to_entity(row) for row in rows]

    @staticmethod
    def _to_entity(row: VideoCandidateModel) -> VideoCandidate:
        return VideoCandidate(
            candidate_id=row.candidate_id,
            spotify_id=row.spotify_id,
            video_id=row.video_id,
            channel_id=row.channel_id,
            title=row.title,
            source_url=row.source_url,
            source_kind=row.source_kind,
            status=row.status,
            ingestion_status=row.ingestion_status,
            published_at=row.published_at,
            first_seen_at=row.first_seen_at,
            last_seen_at=row.last_seen_at,
            discovery_run_id=row.discovery_run_id,
            failure_reason=row.failure_reason,
        )


class SQLAlchemyReviewRepository(ReviewRepository):
    def __init__(self, session_factory: SQLAlchemySessionFactory) -> None:
        self.session_factory = session_factory

    def create(self, review: ReviewItem) -> None:
        with self.session_factory.session_scope() as session:
            session.add(
                ReviewItemModel(
                    review_id=review.review_id,
                    subject_kind=review.subject_kind,
                    subject_id=review.subject_id,
                    spotify_id=review.spotify_id,
                    review_type=review.review_type,
                    status=review.status,
                    version=review.version,
                    decision_comment=review.decision_comment,
                    decided_by=review.decided_by,
                    decided_at=review.decided_at,
                    created_at=review.created_at,
                    updated_at=review.updated_at,
                )
            )

    def update(self, review: ReviewItem) -> None:
        with self.session_factory.session_scope() as session:
            row = session.get(ReviewItemModel, review.review_id)
            if row is None:
                self.create(review)
                return

            row.subject_kind = review.subject_kind
            row.subject_id = review.subject_id
            row.spotify_id = review.spotify_id
            row.review_type = review.review_type
            row.status = review.status
            row.version = review.version
            row.decision_comment = review.decision_comment
            row.decided_by = review.decided_by
            row.decided_at = review.decided_at
            row.updated_at = review.updated_at

    def get(self, review_id: str) -> ReviewItem | None:
        with self.session_factory.session_scope() as session:
            row = session.get(ReviewItemModel, review_id)
            return self._to_entity(row) if row is not None else None

    def list_for_subject(self, subject_kind: str, subject_id: str) -> list[ReviewItem]:
        with self.session_factory.session_scope() as session:
            rows = (
                session.execute(
                    select(ReviewItemModel)
                    .where(
                        ReviewItemModel.subject_kind == subject_kind,
                        ReviewItemModel.subject_id == subject_id,
                    )
                    .order_by(ReviewItemModel.created_at.asc())
                )
                .scalars()
                .all()
            )
            return [self._to_entity(row) for row in rows]

    def list_pending(self) -> list[ReviewItem]:
        with self.session_factory.session_scope() as session:
            rows = (
                session.execute(
                    select(ReviewItemModel)
                    .where(ReviewItemModel.status == "pending")
                    .order_by(ReviewItemModel.created_at.asc())
                )
                .scalars()
                .all()
            )
            return [self._to_entity(row) for row in rows]

    @staticmethod
    def _to_entity(row: ReviewItemModel) -> ReviewItem:
        return ReviewItem(
            review_id=row.review_id,
            subject_kind=row.subject_kind,
            subject_id=row.subject_id,
            spotify_id=row.spotify_id,
            review_type=row.review_type,
            status=row.status,
            version=row.version,
            decision_comment=row.decision_comment,
            decided_by=row.decided_by,
            decided_at=row.decided_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class SQLAlchemyAuditLogRepository(AuditLogRepository):
    def __init__(self, session_factory: SQLAlchemySessionFactory) -> None:
        self.session_factory = session_factory

    def add(self, log_entry: AuditLogEntry) -> None:
        with self.session_factory.session_scope() as session:
            session.add(
                AuditLogEntryModel(
                    log_id=log_entry.log_id,
                    aggregate_type=log_entry.aggregate_type,
                    aggregate_id=log_entry.aggregate_id,
                    action=log_entry.action,
                    actor_id=log_entry.actor_id,
                    details=log_entry.details,
                    created_at=log_entry.created_at,
                )
            )

    def list_for_aggregate(self, aggregate_type: str, aggregate_id: str) -> list[AuditLogEntry]:
        with self.session_factory.session_scope() as session:
            rows = (
                session.execute(
                    select(AuditLogEntryModel)
                    .where(
                        AuditLogEntryModel.aggregate_type == aggregate_type,
                        AuditLogEntryModel.aggregate_id == aggregate_id,
                    )
                    .order_by(AuditLogEntryModel.created_at.asc())
                )
                .scalars()
                .all()
            )
            return [
                AuditLogEntry(
                    log_id=row.log_id,
                    aggregate_type=row.aggregate_type,
                    aggregate_id=row.aggregate_id,
                    action=row.action,
                    actor_id=row.actor_id,
                    details=row.details,
                    created_at=row.created_at,
                )
                for row in rows
            ]


class SQLAlchemyArtifactRepository(ArtifactRepository):
    def __init__(self, session_factory: SQLAlchemySessionFactory) -> None:
        self.session_factory = session_factory

    def upsert(self, artifact: ArtifactRecord) -> None:
        with self.session_factory.session_scope() as session:
            row = session.get(ArtifactModel, artifact.artifact_id)
            if row is None:
                row = ArtifactModel(
                    artifact_id=artifact.artifact_id,
                    owner_type=artifact.owner_type,
                    owner_id=artifact.owner_id,
                    artifact_type=artifact.artifact_type,
                    object_uri=artifact.object_uri,
                    object_key=artifact.object_key,
                    bucket=artifact.bucket,
                    storage_provider=artifact.storage_provider,
                    content_type=artifact.content_type,
                    job_id=artifact.job_id,
                    candidate_id=artifact.candidate_id,
                    size_bytes=artifact.size_bytes,
                    checksum_sha256=artifact.checksum_sha256,
                    lifecycle_status=artifact.lifecycle_status,
                    version=artifact.version,
                    metadata_json=json.dumps(artifact.metadata, ensure_ascii=False, sort_keys=True),
                    created_at=artifact.created_at,
                    updated_at=artifact.updated_at,
                    expires_at=artifact.expires_at,
                )
                session.add(row)
                return

            row.owner_type = artifact.owner_type
            row.owner_id = artifact.owner_id
            row.artifact_type = artifact.artifact_type
            row.object_uri = artifact.object_uri
            row.object_key = artifact.object_key
            row.bucket = artifact.bucket
            row.storage_provider = artifact.storage_provider
            row.content_type = artifact.content_type
            row.job_id = artifact.job_id
            row.candidate_id = artifact.candidate_id
            row.size_bytes = artifact.size_bytes
            row.checksum_sha256 = artifact.checksum_sha256
            row.lifecycle_status = artifact.lifecycle_status
            row.version = artifact.version
            row.metadata_json = json.dumps(artifact.metadata, ensure_ascii=False, sort_keys=True)
            row.updated_at = artifact.updated_at
            row.expires_at = artifact.expires_at

    def get(self, artifact_id: str) -> ArtifactRecord | None:
        with self.session_factory.session_scope() as session:
            row = session.get(ArtifactModel, artifact_id)
            return self._to_entity(row) if row is not None else None

    def list_for_job(self, job_id: str) -> list[ArtifactRecord]:
        with self.session_factory.session_scope() as session:
            rows = (
                session.execute(
                    select(ArtifactModel)
                    .where(ArtifactModel.job_id == job_id)
                    .order_by(ArtifactModel.created_at.asc())
                )
                .scalars()
                .all()
            )
            return [self._to_entity(row) for row in rows]

    def list_for_owner(self, owner_type: str, owner_id: str) -> list[ArtifactRecord]:
        with self.session_factory.session_scope() as session:
            rows = (
                session.execute(
                    select(ArtifactModel)
                    .where(ArtifactModel.owner_type == owner_type, ArtifactModel.owner_id == owner_id)
                    .order_by(ArtifactModel.artifact_type.asc(), ArtifactModel.version.desc())
                )
                .scalars()
                .all()
            )
            return [self._to_entity(row) for row in rows]

    def list_expired(self, now) -> list[ArtifactRecord]:
        with self.session_factory.session_scope() as session:
            rows = (
                session.execute(
                    select(ArtifactModel)
                    .where(
                        ArtifactModel.expires_at.is_not(None),
                        ArtifactModel.expires_at <= now,
                        ArtifactModel.lifecycle_status.in_(("ready", "expired")),
                    )
                    .order_by(ArtifactModel.expires_at.asc())
                )
                .scalars()
                .all()
            )
            return [self._to_entity(row) for row in rows]

    @staticmethod
    def _to_entity(row: ArtifactModel) -> ArtifactRecord:
        try:
            metadata = json.loads(row.metadata_json or "{}")
        except json.JSONDecodeError:
            metadata = {"raw": row.metadata_json}
        return ArtifactRecord(
            artifact_id=row.artifact_id,
            owner_type=row.owner_type,
            owner_id=row.owner_id,
            artifact_type=row.artifact_type,
            object_uri=row.object_uri,
            object_key=row.object_key,
            bucket=row.bucket,
            storage_provider=row.storage_provider,
            content_type=row.content_type,
            job_id=row.job_id,
            candidate_id=row.candidate_id,
            size_bytes=row.size_bytes,
            checksum_sha256=row.checksum_sha256,
            lifecycle_status=row.lifecycle_status,
            version=row.version,
            metadata=metadata,
            created_at=row.created_at,
            updated_at=row.updated_at,
            expires_at=row.expires_at,
        )


class SQLAlchemyOutboxRepository(OutboxRepository):
    def __init__(self, session_factory: SQLAlchemySessionFactory) -> None:
        self.session_factory = session_factory
        self._is_postgres = session_factory.engine.dialect.name == "postgresql"

    def add(self, event: OutboxEvent) -> None:
        with self.session_factory.session_scope() as session:
            self._add_with_session(session, event)

    def add_with_session(self, session, event: OutboxEvent) -> None:
        """Add using a caller-supplied session (for shared transactions)."""
        self._add_with_session(session, event)

    def _add_with_session(self, session, event: OutboxEvent) -> None:
        session.add(
            OutboxModel(
                event_id=event.event_id,
                topic=event.topic,
                payload=event.payload,
                status=event.status,
                aggregate_id=event.aggregate_id,
                dedupe_key=event.dedupe_key,
                correlation_id=event.correlation_id,
            )
        )

    def list_pending(self) -> list[OutboxEvent]:
        with self.session_factory.session_scope() as session:
            stmt = (
                select(OutboxModel)
                .where(OutboxModel.status == "pending")
                .order_by(OutboxModel.event_id.asc())
                .limit(50)
            )
            if self._is_postgres:
                stmt = stmt.with_for_update(skip_locked=True)
            rows = session.execute(stmt).scalars().all()
            return [self._to_entity(row) for row in rows]

    def get(self, event_id: str) -> OutboxEvent | None:
        with self.session_factory.session_scope() as session:
            row = session.get(OutboxModel, event_id)
            if row is None:
                return None
            return self._to_entity(row)

    def update(self, event: OutboxEvent) -> None:
        with self.session_factory.session_scope() as session:
            row = session.get(OutboxModel, event.event_id)
            if row is None:
                self.add(event)
                return

            row.topic = event.topic
            row.payload = event.payload
            row.status = event.status
            row.aggregate_id = event.aggregate_id
            row.dedupe_key = event.dedupe_key
            row.correlation_id = event.correlation_id

    @staticmethod
    def _to_entity(row: OutboxModel) -> OutboxEvent:
        return OutboxEvent(
            event_id=row.event_id,
            topic=row.topic,
            payload=row.payload,
            status=row.status,
            aggregate_id=row.aggregate_id,
            dedupe_key=row.dedupe_key,
            correlation_id=row.correlation_id,
        )
