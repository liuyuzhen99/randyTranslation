import tempfile
import unittest
from datetime import datetime

from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError

from domain.entities import Artist, Job, JobEvent, OutboxEvent, Subtitle, Video
from domain.enums import JobStatus, OutboxStatus, StageStatus, StageType
from domain.exceptions import InvalidJobTransitionError
from domain.job_lifecycle import transition_job, validate_job_transition
from infrastructure.persistence.sqlalchemy_models import Base
from infrastructure.persistence.sqlalchemy_repositories import (
    SQLAlchemyArtistRepository,
    SQLAlchemyJobEventRepository,
    SQLAlchemyJobRepository,
    SQLAlchemyOutboxRepository,
    SQLAlchemySessionFactory,
    SQLAlchemySubtitleRepository,
    SQLAlchemyVideoRepository,
)


class Phase2PostgresFoundationTests(unittest.TestCase):
    def test_job_lifecycle_rejects_invalid_transition(self):
        with self.assertRaises(InvalidJobTransitionError):
            validate_job_transition(JobStatus.PENDING, JobStatus.COMPLETED)

    def test_job_lifecycle_allows_retry_from_failed_to_processing(self):
        failed_job = Job(job_id="job1", song_name="song", status=JobStatus.FAILED, retry_count=1)

        retried_job = transition_job(
            failed_job,
            JobStatus.PROCESSING,
            progress="retrying",
            stage=StageType.DOWNLOAD,
        )

        self.assertEqual(retried_job.status, JobStatus.PROCESSING)
        self.assertEqual(retried_job.retry_count, 2)
        self.assertEqual(retried_job.current_stage, StageType.DOWNLOAD)

    def test_sqlalchemy_metadata_exposes_phase2_core_tables_and_indexes(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(engine)
        inspector = inspect(engine)

        self.assertTrue(
            {"artists", "videos", "subtitles", "jobs", "job_events", "outbox"}.issubset(
                set(inspector.get_table_names())
            )
        )
        job_indexes = {index["name"] for index in inspector.get_indexes("jobs")}
        video_indexes = {index["name"] for index in inspector.get_indexes("videos")}
        self.assertIn("ix_jobs_status", job_indexes)
        self.assertIn("ix_videos_spotify_id", video_indexes)
        self.assertIn("ix_videos_processed_status", video_indexes)

    def test_sqlalchemy_job_repository_enforces_transition_rules(self):
        with tempfile.TemporaryDirectory() as temp_root:
            session_factory = SQLAlchemySessionFactory(f"sqlite:///{temp_root}/phase2.db")
            session_factory.create_schema()
            repository = SQLAlchemyJobRepository(session_factory)
            job = Job(job_id="job2", song_name="song")
            repository.create(job)

            invalid_update = Job(
                job_id="job2",
                song_name="song",
                status=JobStatus.COMPLETED,
                progress="skipped",
            )

            with self.assertRaises(InvalidJobTransitionError):
                repository.update(invalid_update)

    def test_sqlalchemy_repositories_round_trip_core_entities(self):
        with tempfile.TemporaryDirectory() as temp_root:
            session_factory = SQLAlchemySessionFactory(f"sqlite:///{temp_root}/phase2.db")
            session_factory.create_schema()
            artist_repository = SQLAlchemyArtistRepository(session_factory)
            video_repository = SQLAlchemyVideoRepository(session_factory)
            subtitle_repository = SQLAlchemySubtitleRepository(session_factory)
            job_repository = SQLAlchemyJobRepository(session_factory)
            job_event_repository = SQLAlchemyJobEventRepository(session_factory)
            outbox_repository = SQLAlchemyOutboxRepository(session_factory)

            artist_repository.upsert(Artist(spotify_id="artist-1", name="Artist"))
            video_repository.upsert(
                Video(
                    video_id="video-1",
                    spotify_id="artist-1",
                    title="Track",
                    published_at=datetime.utcnow(),
                    processed_status=StageStatus.PROCESSING,
                )
            )
            subtitle_repository.replace_for_video(
                "video-1",
                [
                    Subtitle(
                        video_id="video-1",
                        line_index=0,
                        start_time=0.0,
                        end_time=1.0,
                        en_text="hello",
                        zh_text="你好",
                        status=StageStatus.COMPLETED,
                    )
                ],
            )
            job_repository.create(Job(job_id="job3", song_name="song"))
            job_event_repository.add(
                JobEvent(
                    event_id="evt-1",
                    job_id="job3",
                    from_status=None,
                    to_status=JobStatus.PENDING,
                    stage=StageType.DOWNLOAD,
                    message="created",
                )
            )
            outbox_repository.add(
                OutboxEvent(
                    event_id="outbox-1",
                    topic="pipeline.command",
                    payload="{}",
                    status=OutboxStatus.PENDING,
                    dedupe_key="job3-download-0",
                    correlation_id="job3",
                )
            )

            self.assertEqual(artist_repository.get("artist-1").name, "Artist")
            self.assertEqual(video_repository.get("video-1").spotify_id, "artist-1")
            self.assertEqual(len(subtitle_repository.list_for_video("video-1")), 1)
            self.assertEqual(job_repository.get("job3").status, JobStatus.PENDING)
            self.assertEqual(len(job_event_repository.list_for_job("job3")), 1)
            self.assertEqual(len(outbox_repository.list_pending()), 1)

    def test_sqlalchemy_outbox_dedupe_key_is_unique(self):
        with tempfile.TemporaryDirectory() as temp_root:
            session_factory = SQLAlchemySessionFactory(f"sqlite:///{temp_root}/phase2.db")
            session_factory.create_schema()
            repository = SQLAlchemyOutboxRepository(session_factory)
            event = OutboxEvent(
                event_id="outbox-1",
                topic="pipeline.command",
                payload="{}",
                status=OutboxStatus.PENDING,
                dedupe_key="job4-download-0",
                correlation_id="job4",
            )
            repository.add(event)

            with self.assertRaises(IntegrityError):
                repository.add(
                    OutboxEvent(
                        event_id="outbox-2",
                        topic="pipeline.command",
                        payload="{}",
                        status=OutboxStatus.PENDING,
                        dedupe_key="job4-download-0",
                        correlation_id="job4",
                    )
                )


if __name__ == "__main__":
    unittest.main()
