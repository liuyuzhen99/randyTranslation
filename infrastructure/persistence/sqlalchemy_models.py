from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

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


class Base(DeclarativeBase):
    pass


job_status_enum = Enum(JobStatus, native_enum=False, create_constraint=True, validate_strings=True)
stage_status_enum = Enum(
    StageStatus,
    native_enum=False,
    create_constraint=True,
    validate_strings=True,
)
stage_type_enum = Enum(StageType, native_enum=False, create_constraint=True, validate_strings=True)
outbox_status_enum = Enum(
    OutboxStatus,
    native_enum=False,
    create_constraint=True,
    validate_strings=True,
)
sync_status_enum = Enum(
    SyncStatus,
    native_enum=False,
    create_constraint=True,
    validate_strings=True,
)
candidate_status_enum = Enum(
    CandidateStatus,
    native_enum=False,
    create_constraint=True,
    validate_strings=True,
)
review_type_enum = Enum(
    ReviewType,
    native_enum=False,
    create_constraint=True,
    validate_strings=True,
)
review_status_enum = Enum(
    ReviewStatus,
    native_enum=False,
    create_constraint=True,
    validate_strings=True,
)


def build_job_status_enum(constraint_name: str) -> Enum:
    return Enum(
        JobStatus,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        name=constraint_name,
    )


def build_stage_type_enum(constraint_name: str) -> Enum:
    return Enum(
        StageType,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        name=constraint_name,
    )


class ArtistModel(Base):
    __tablename__ = "artists"

    spotify_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    yt_channel_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    sync_status: Mapped[SyncStatus] = mapped_column(sync_status_enum, nullable=False)
    last_sync_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    last_sync_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_channel_resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    last_discovery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)


class ArtistSyncRunModel(Base):
    __tablename__ = "artist_sync_runs"
    __table_args__ = (
        Index("ix_artist_sync_runs_spotify_id_started_at", "spotify_id", "started_at"),
        Index("ix_artist_sync_runs_status", "status"),
        CheckConstraint("retry_count >= 0", name="ck_artist_sync_runs_retry_count_non_negative"),
    )

    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    spotify_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("artists.spotify_id", ondelete="SET NULL"),
        nullable=True,
    )
    source_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[SyncStatus] = mapped_column(sync_status_enum, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    discovered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    trigger: Mapped[str] = mapped_column(String(32), nullable=False, default="system")


class VideoModel(Base):
    __tablename__ = "videos"
    __table_args__ = (
        Index("ix_videos_spotify_id", "spotify_id"),
        Index("ix_videos_processed_status", "processed_status"),
    )

    video_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    spotify_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("artists.spotify_id"),
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    processed_status: Mapped[StageStatus] = mapped_column(stage_status_enum, nullable=False)
    local_video_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    srt_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_video_path: Mapped[str | None] = mapped_column(Text, nullable=True)


class SubtitleModel(Base):
    __tablename__ = "subtitles"
    __table_args__ = (
        UniqueConstraint("video_id", "line_index", name="uq_subtitles_video_line_index"),
    )

    subtitle_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("videos.video_id", ondelete="CASCADE"),
        nullable=False,
    )
    line_index: Mapped[int] = mapped_column(Integer, nullable=False)
    start_time: Mapped[float] = mapped_column(Float, nullable=False)
    end_time: Mapped[float] = mapped_column(Float, nullable=False)
    en_text: Mapped[str] = mapped_column(Text, nullable=False)
    zh_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[StageStatus] = mapped_column(stage_status_enum, nullable=False)


class JobModel(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_status", "status"),
        CheckConstraint("retry_count >= 0", name="ck_jobs_retry_count_non_negative"),
    )

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    song_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        build_job_status_enum("ck_jobs_status"),
        nullable=False,
    )
    progress: Mapped[str] = mapped_column(Text, nullable=False)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    current_stage: Mapped[StageType | None] = mapped_column(stage_type_enum, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)


class JobEventModel(Base):
    __tablename__ = "job_events"
    __table_args__ = (
        Index("ix_job_events_job_id_created_at", "job_id", "created_at"),
        CheckConstraint("retry_count >= 0", name="ck_job_events_retry_count_non_negative"),
    )

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("jobs.job_id", ondelete="CASCADE"),
        nullable=False,
    )
    from_status: Mapped[JobStatus | None] = mapped_column(
        build_job_status_enum("ck_job_events_from_status"),
        nullable=True,
    )
    to_status: Mapped[JobStatus] = mapped_column(
        build_job_status_enum("ck_job_events_to_status"),
        nullable=False,
    )
    stage: Mapped[StageType | None] = mapped_column(
        build_stage_type_enum("ck_job_events_stage"),
        nullable=True,
    )
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)


class PipelineStageExecutionModel(Base):
    __tablename__ = "pipeline_stage_executions"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_pipeline_stage_executions_dedupe_key"),
        Index("ix_pipeline_stage_executions_job_stage", "job_id", "stage"),
        Index("ix_pipeline_stage_executions_candidate", "candidate_id", "created_at"),
        Index("ix_pipeline_stage_executions_status_retry", "status", "next_retry_at"),
        CheckConstraint("attempt >= 0", name="ck_pipeline_stage_executions_attempt_non_negative"),
        CheckConstraint("max_attempts >= 1", name="ck_pipeline_stage_executions_max_attempts_positive"),
    )

    execution_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    job_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("jobs.job_id", ondelete="CASCADE"),
        nullable=False,
    )
    stage: Mapped[StageType] = mapped_column(stage_type_enum, nullable=False)
    candidate_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("video_candidates.candidate_id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[StageStatus] = mapped_column(stage_status_enum, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)


class OutboxModel(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_outbox_dedupe_key"),
        Index("ix_outbox_status", "status"),
        Index("ix_outbox_status_event_id", "status", "event_id"),
        Index("ix_outbox_correlation_id", "correlation_id"),
    )

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    topic: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[OutboxStatus] = mapped_column(outbox_status_enum, nullable=False)
    aggregate_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)


class VideoCandidateModel(Base):
    __tablename__ = "video_candidates"
    __table_args__ = (
        UniqueConstraint("spotify_id", "video_id", name="uq_video_candidates_artist_video"),
        Index("ix_video_candidates_spotify_id_published_at", "spotify_id", "published_at"),
        Index("ix_video_candidates_status", "status"),
    )

    candidate_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    spotify_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("artists.spotify_id", ondelete="CASCADE"),
        nullable=False,
    )
    video_id: Mapped[str] = mapped_column(String(128), nullable=False)
    channel_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_kind: Mapped[str] = mapped_column(String(64), nullable=False, default="youtube_rss")
    status: Mapped[CandidateStatus] = mapped_column(candidate_status_enum, nullable=False)
    ingestion_status: Mapped[SyncStatus] = mapped_column(sync_status_enum, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    discovery_run_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("artist_sync_runs.run_id", ondelete="SET NULL"),
        nullable=True,
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ReviewItemModel(Base):
    __tablename__ = "review_items"
    __table_args__ = (
        Index("ix_review_items_subject", "subject_kind", "subject_id"),
        Index("ix_review_items_status_created_at", "status", "created_at"),
        UniqueConstraint("subject_kind", "subject_id", "review_type", name="uq_review_items_subject_type"),
        CheckConstraint("version >= 1", name="ck_review_items_version_positive"),
    )

    review_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    subject_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    spotify_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("artists.spotify_id", ondelete="CASCADE"),
        nullable=False,
    )
    review_type: Mapped[ReviewType] = mapped_column(review_type_enum, nullable=False)
    status: Mapped[ReviewStatus] = mapped_column(review_status_enum, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    decision_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)


class AuditLogEntryModel(Base):
    __tablename__ = "audit_log_entries"
    __table_args__ = (
        Index("ix_audit_log_entries_aggregate", "aggregate_type", "aggregate_id", "created_at"),
        Index("ix_audit_log_entries_actor", "actor_id"),
    )

    log_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    aggregate_type: Mapped[str] = mapped_column(String(32), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)


class ArtifactModel(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("owner_type", "owner_id", "artifact_type", "version", name="uq_artifacts_owner_type_version"),
        Index("ix_artifacts_job_id", "job_id"),
        Index("ix_artifacts_owner", "owner_type", "owner_id"),
        Index("ix_artifacts_object_uri", "object_uri"),
        CheckConstraint("version >= 1", name="ck_artifacts_version_positive"),
        CheckConstraint("size_bytes >= 0", name="ck_artifacts_size_non_negative"),
    )

    artifact_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    owner_type: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(64), nullable=False)
    object_uri: Mapped[str] = mapped_column(Text, nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    job_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("jobs.job_id", ondelete="SET NULL"),
        nullable=True,
    )
    candidate_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("video_candidates.candidate_id", ondelete="SET NULL"),
        nullable=True,
    )
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(String(32), nullable=False, default="ready")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
