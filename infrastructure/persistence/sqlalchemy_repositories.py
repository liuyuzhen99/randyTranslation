from __future__ import annotations

from contextlib import contextmanager
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from domain.entities import Artist, Job, JobEvent, OutboxEvent, Subtitle, Video
from domain.job_lifecycle import validate_job_transition
from domain.time_utils import utc_now
from domain.repositories import (
    ArtistRepository,
    JobEventRepository,
    JobRepository,
    OutboxRepository,
    SubtitleRepository,
    VideoRepository,
)
from infrastructure.persistence.sqlalchemy_models import (
    ArtistModel,
    Base,
    JobEventModel,
    JobModel,
    OutboxModel,
    SubtitleModel,
    VideoModel,
)


class SQLAlchemySessionFactory:
    """Small session wrapper so repositories share one engine/config entry point."""

    def __init__(self, database_url: str) -> None:
        self.engine: Engine = create_engine(database_url, future=True)
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
                )
                session.add(existing)
                return

            existing.name = artist.name
            existing.yt_channel_id = artist.yt_channel_id
            existing.status = artist.status

    def get(self, spotify_id: str) -> Artist | None:
        with self.session_factory.session_scope() as session:
            artist = session.get(ArtistModel, spotify_id)
            if artist is None:
                return None
            return Artist(
                spotify_id=artist.spotify_id,
                name=artist.name,
                yt_channel_id=artist.yt_channel_id,
                status=artist.status,
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


class SQLAlchemyOutboxRepository(OutboxRepository):
    def __init__(self, session_factory: SQLAlchemySessionFactory) -> None:
        self.session_factory = session_factory

    def add(self, event: OutboxEvent) -> None:
        with self.session_factory.session_scope() as session:
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
            rows = (
                session.execute(select(OutboxModel).where(OutboxModel.status == "pending"))
                .scalars()
                .all()
            )
            return [
                OutboxEvent(
                    event_id=row.event_id,
                    topic=row.topic,
                    payload=row.payload,
                    status=row.status,
                    aggregate_id=row.aggregate_id,
                    dedupe_key=row.dedupe_key,
                    correlation_id=row.correlation_id,
                )
                for row in rows
            ]
