import tempfile
import unittest

from sqlalchemy import create_engine, inspect
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

from domain.entities import Artist, Job, JobEvent, OutboxEvent, Subtitle, Video
from domain.enums import JobStatus, OutboxStatus, StageStatus, StageType
from domain.exceptions import InvalidJobTransitionError
from domain.job_lifecycle import transition_job, validate_job_transition
from domain.time_utils import utc_now
from application.services.job_service import JobService
from application.services.outbox_dispatcher import OutboxDispatcher
from application.services.phase2_reconcile_service import Phase2ReconcileService
from application.services.phase2_shadow_write_service import Phase2ShadowWriteService
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
from infrastructure.persistence.in_memory_job_repository import InMemoryJobRepository


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
                    published_at=utc_now(),
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

    def test_shadow_write_service_records_job_creation_transactionally(self):
        with tempfile.TemporaryDirectory() as temp_root:
            session_factory = SQLAlchemySessionFactory(f"sqlite:///{temp_root}/phase2.db")
            session_factory.create_schema()
            service = Phase2ShadowWriteService(session_factory)
            job = Job(job_id="job-shadow-1", song_name="song")

            service.record_job_created(job)

            self.assertEqual(SQLAlchemyJobRepository(session_factory).get(job.job_id).song_name, "song")
            self.assertEqual(len(SQLAlchemyJobEventRepository(session_factory).list_for_job(job.job_id)), 1)
            self.assertEqual(len(SQLAlchemyOutboxRepository(session_factory).list_pending()), 1)

    def test_reconcile_service_reports_consistent_shadow_write_state(self):
        with tempfile.TemporaryDirectory() as temp_root:
            session_factory = SQLAlchemySessionFactory(f"sqlite:///{temp_root}/phase2.db")
            session_factory.create_schema()
            primary_repository = SQLAlchemyJobRepository(session_factory)
            shadow_service = Phase2ShadowWriteService(session_factory)
            job_service = JobService(primary_repository, shadow_write_service=shadow_service)
            job = job_service.create_job("song")

            report = Phase2ReconcileService(primary_repository, session_factory).generate_report()

            self.assertTrue(report.is_consistent)
            self.assertEqual(report.primary_job_count, 1)
            self.assertEqual(report.shadow_job_count, 1)
            self.assertEqual(report.pending_outbox_count, 1)
            self.assertEqual(report.shadow_job_event_count, 1)
            self.assertEqual(report.missing_job_ids_in_shadow, [])
            self.assertNotEqual(job.job_id, "")

    def test_reconcile_service_reports_missing_shadow_jobs(self):
        with tempfile.TemporaryDirectory() as temp_root:
            primary_repository = InMemoryJobRepository()
            primary_repository.create(Job(job_id="job-missing", song_name="song"))
            session_factory = SQLAlchemySessionFactory(f"sqlite:///{temp_root}/phase2.db")
            session_factory.create_schema()

            report = Phase2ReconcileService(primary_repository, session_factory).generate_report()

            self.assertFalse(report.is_consistent)
            self.assertEqual(report.missing_job_ids_in_shadow, ["job-missing"])

    def test_reconcile_service_writes_report_file(self):
        with tempfile.TemporaryDirectory() as temp_root:
            primary_repository = InMemoryJobRepository()
            primary_repository.create(Job(job_id="job-report", song_name="song"))
            session_factory = SQLAlchemySessionFactory(f"sqlite:///{temp_root}/phase2.db")
            session_factory.create_schema()
            service = Phase2ReconcileService(primary_repository, session_factory)

            report = service.write_report(f"{temp_root}/reports/reconcile.json")

            self.assertFalse(report.is_consistent)
            with open(f"{temp_root}/reports/reconcile.json", "r", encoding="utf-8") as file_obj:
                content = file_obj.read()
            self.assertIn('"missing_job_ids_in_shadow"', content)

    def test_outbox_dispatcher_marks_events_published(self):
        with tempfile.TemporaryDirectory() as temp_root:
            session_factory = SQLAlchemySessionFactory(f"sqlite:///{temp_root}/phase2.db")
            session_factory.create_schema()
            outbox_repository = SQLAlchemyOutboxRepository(session_factory)
            outbox_repository.add(
                OutboxEvent(
                    event_id="outbox-publish-1",
                    topic="job.lifecycle",
                    payload='{"hello":"world"}',
                    status=OutboxStatus.PENDING,
                    dedupe_key="dispatch-1",
                    correlation_id="job-1",
                )
            )

            published_calls = []

            class Publisher:
                def publish(self, topic: str, payload: str, correlation_id=None) -> None:
                    published_calls.append((topic, payload, correlation_id))

            summary = OutboxDispatcher(outbox_repository, Publisher()).dispatch_pending()

            self.assertEqual(summary, {"published": 1, "failed": 0})
            self.assertEqual(len(published_calls), 1)
            self.assertEqual(
                outbox_repository.get("outbox-publish-1").status,
                OutboxStatus.PUBLISHED,
            )

    def test_outbox_dispatcher_marks_failures(self):
        with tempfile.TemporaryDirectory() as temp_root:
            session_factory = SQLAlchemySessionFactory(f"sqlite:///{temp_root}/phase2.db")
            session_factory.create_schema()
            outbox_repository = SQLAlchemyOutboxRepository(session_factory)
            outbox_repository.add(
                OutboxEvent(
                    event_id="outbox-fail-1",
                    topic="job.lifecycle",
                    payload='{"hello":"world"}',
                    status=OutboxStatus.PENDING,
                    dedupe_key="dispatch-fail-1",
                    correlation_id="job-1",
                )
            )

            class FailingPublisher:
                def publish(self, topic: str, payload: str, correlation_id=None) -> None:
                    raise RuntimeError("network down")

            summary = OutboxDispatcher(outbox_repository, FailingPublisher()).dispatch_pending()

            self.assertEqual(summary, {"published": 0, "failed": 1})
            self.assertEqual(
                outbox_repository.get("outbox-fail-1").status,
                OutboxStatus.FAILED,
            )

    def test_job_service_shadow_writes_initial_records(self):
        with tempfile.TemporaryDirectory() as temp_root:
            session_factory = SQLAlchemySessionFactory(f"sqlite:///{temp_root}/phase2.db")
            session_factory.create_schema()
            shadow_service = Phase2ShadowWriteService(session_factory)
            primary_repository = SQLAlchemyJobRepository(session_factory)
            service = JobService(primary_repository, shadow_write_service=shadow_service)

            job = service.create_job("song")

            self.assertIsNotNone(primary_repository.get(job.job_id))
            self.assertEqual(len(SQLAlchemyJobEventRepository(session_factory).list_for_job(job.job_id)), 1)

    def test_alembic_upgrade_and_downgrade_manage_phase2_schema(self):
        with tempfile.TemporaryDirectory() as temp_root:
            database_path = f"{temp_root}/alembic.db"
            config = Config("alembic.ini")
            config.set_main_option(
                "script_location",
                "alembic",
            )
            config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")

            command.upgrade(config, "head")
            inspector = inspect(create_engine(f"sqlite:///{database_path}", future=True))
            self.assertIn("jobs", inspector.get_table_names())
            self.assertIn("job_events", inspector.get_table_names())

            command.downgrade(config, "base")
            inspector = inspect(create_engine(f"sqlite:///{database_path}", future=True))
            self.assertNotIn("jobs", inspector.get_table_names())


if __name__ == "__main__":
    unittest.main()
