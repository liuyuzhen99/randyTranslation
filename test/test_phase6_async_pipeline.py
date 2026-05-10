import os
import time
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

import api.service as api_service
from application.services.phase3_catalog_service import CandidateDiscoveryPayload, Phase3Providers
from application.services.async_pipeline import AsyncPipelineCommandService, PipelineStageWorker
from application.services.phase7_health import Phase7HealthService
from application.services.phase7_metrics import render_prometheus_metrics
from application.services.phase7_observability import Phase7ObservabilityService
from application.services.retry_scheduler import Phase6RetryScheduler
from domain.entities import Job, PipelineStageExecution
from domain.enums import JobStatus, OutboxStatus, StageStatus, StageType
from domain.message_contracts import PipelineStageMessage, ReviewContext
from domain.queue_topology import PipelineQueueTopology, STAGE_ORDER, next_stage
from domain.time_utils import utc_now
from infrastructure.persistence.sqlalchemy_repositories import (
    SQLAlchemyJobRepository,
    SQLAlchemyOutboxRepository,
    SQLAlchemyPipelineStageExecutionRepository,
    SQLAlchemySessionFactory,
)
from infrastructure.messaging.rabbitmq_consumer import RabbitMQWorkerConfig, RabbitMQWorkerConsumer
from infrastructure.messaging.rabbitmq_topology import RabbitMQTopologyConfig, RabbitMQTopologyManager


class FakeProducerBackend:
    def __init__(self):
        self.temp_dir = ""

    def download_step(self, song_name: str, output_path: str):
        with open(output_path, "w", encoding="utf-8") as file_obj:
            file_obj.write(song_name)
        return output_path

    def transcribe_step(self, video_ref, audio_path: str):
        with open(audio_path, "w", encoding="utf-8") as file_obj:
            file_obj.write("audio")
        return [{"start": 0.0, "end": 1.0, "text": "hello"}], ["hello"]

    def audit_step(self, english_texts: list[str], *, title: str = "", references: list[dict] | None = None) -> dict:
        return {"score": 80, "decision": "Pass", "reason": "ok", "key_lyrics": english_texts[:1]}

    def generate_bilingual_srt(self, segments, english_texts, output_file: str):
        with open(output_file, "w", encoding="utf-8") as file_obj:
            file_obj.write("1\n00:00:00,000 --> 00:00:01,000\nhello\n你好\n\n")
        return output_file

    def burn_video(self, video_ref, srt_file: str, final_path: str):
        with open(final_path, "w", encoding="utf-8") as file_obj:
            file_obj.write("final")


class RecordingPublisher:
    def __init__(self):
        self.published = []

    def publish(self, topic: str, payload: str, correlation_id=None) -> None:
        self.published.append(
            {"topic": topic, "payload": payload, "correlation_id": correlation_id}
        )


class Phase6AsyncPipelineTests(unittest.TestCase):
    PROJECT_ROOT = Path(__file__).resolve().parents[1]

    def _session_factory(self, temp_root: str) -> SQLAlchemySessionFactory:
        session_factory = SQLAlchemySessionFactory(f"sqlite:///{os.path.join(temp_root, 'phase6.db')}")
        session_factory.create_schema()
        return session_factory

    def test_phase6_topology_matches_roadmap_stage_queues(self):
        topology = PipelineQueueTopology()

        self.assertEqual(topology.command_queue, "pipeline.command")
        self.assertEqual(topology.dead_letter_queue, "pipeline.dlq")
        self.assertEqual(
            [binding.stage for binding in topology.bindings() if binding.stage is not None],
            list(STAGE_ORDER),
        )
        self.assertEqual(next_stage(StageType.DOWNLOAD), StageType.TRANSCRIBE)
        self.assertEqual(next_stage(StageType.RENDER), None)

    def test_phase6_stage_message_round_trips_review_and_retry_context(self):
        message = PipelineStageMessage.build(
            message_type="pipeline.stage.command",
            job_id="job-phase6-1",
            stage=StageType.MANUAL_REVIEW,
            song_name="Sunday Again",
            trace_id="trace-1",
            attempt=2,
            max_attempts=5,
            backoff_seconds=120,
            review=ReviewContext(
                candidate_id="candidate-1",
                review_id="review-1",
                review_type="manual_review",
                expected_version=3,
            ),
            payload={"source": "test"},
        )

        parsed = PipelineStageMessage.from_payload(message.to_payload())

        self.assertEqual(parsed.schema_version, "v1")
        self.assertEqual(parsed.job_id, "job-phase6-1")
        self.assertEqual(parsed.stage, StageType.MANUAL_REVIEW)
        self.assertEqual(parsed.retry.attempt, 2)
        self.assertEqual(parsed.retry.max_attempts, 5)
        self.assertEqual(parsed.retry.backoff_seconds, 120)
        self.assertEqual(parsed.review.review_id, "review-1")
        self.assertEqual(parsed.payload, {"source": "test"})

    def test_phase6_command_service_uses_outbox_as_only_publish_path(self):
        with TemporaryDirectory() as temp_root:
            session_factory = self._session_factory(temp_root)
            job_repository = SQLAlchemyJobRepository(session_factory)
            outbox_repository = SQLAlchemyOutboxRepository(session_factory)
            job = Job(job_id="job-phase6-2", song_name="Async Song")
            job_repository.create(job)

            command_service = AsyncPipelineCommandService(
                outbox_repository=outbox_repository,
                max_attempts=4,
            )
            message = command_service.enqueue_first_stage(job, candidate_id="candidate-2")
            pending = outbox_repository.list_pending()

            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].topic, "pipeline.command")
            self.assertEqual(pending[0].status, OutboxStatus.PENDING)
            self.assertEqual(pending[0].aggregate_id, job.job_id)
            self.assertEqual(pending[0].dedupe_key, message.dedupe_key)
            parsed = PipelineStageMessage.from_payload(pending[0].payload)
            self.assertEqual(parsed.stage, StageType.DOWNLOAD)
            self.assertEqual(parsed.review.candidate_id, "candidate-2")
            self.assertEqual(parsed.retry.max_attempts, 4)

    def test_phase6_worker_replay_does_not_repeat_side_effects(self):
        with TemporaryDirectory() as temp_root:
            session_factory = self._session_factory(temp_root)
            job_repository = SQLAlchemyJobRepository(session_factory)
            outbox_repository = SQLAlchemyOutboxRepository(session_factory)
            execution_repository = SQLAlchemyPipelineStageExecutionRepository(session_factory)
            job_repository.create(Job(job_id="job-phase6-3", song_name="Replay Song"))
            command_service = AsyncPipelineCommandService(outbox_repository=outbox_repository)
            calls = []
            worker = PipelineStageWorker(
                job_repository=job_repository,
                execution_repository=execution_repository,
                command_service=command_service,
                handlers={StageType.DOWNLOAD: lambda message: calls.append(message.dedupe_key) or {"ok": True}},
            )
            message = PipelineStageMessage.build(
                message_type="pipeline.stage.command",
                job_id="job-phase6-3",
                stage=StageType.DOWNLOAD,
                song_name="Replay Song",
            )

            first = worker.handle(message)
            second = worker.handle(message)

            self.assertEqual(first.action, "ack")
            self.assertEqual(second.action, "ack_duplicate")
            self.assertEqual(calls, [message.dedupe_key])
            execution = execution_repository.get_by_dedupe_key(message.dedupe_key)
            self.assertEqual(execution.status, StageStatus.COMPLETED)
            self.assertEqual(
                [
                    PipelineStageMessage.from_payload(event.payload).stage
                    for event in outbox_repository.list_pending()
                ],
                [StageType.TRANSCRIBE],
            )

    def test_phase6_worker_retries_then_routes_to_dlq(self):
        with TemporaryDirectory() as temp_root:
            session_factory = self._session_factory(temp_root)
            job_repository = SQLAlchemyJobRepository(session_factory)
            outbox_repository = SQLAlchemyOutboxRepository(session_factory)
            execution_repository = SQLAlchemyPipelineStageExecutionRepository(session_factory)
            job_repository.create(Job(job_id="job-phase6-4", song_name="Failing Song"))
            command_service = AsyncPipelineCommandService(outbox_repository=outbox_repository)

            def fail(_message):
                raise RuntimeError("transient failure")

            worker = PipelineStageWorker(
                job_repository=job_repository,
                execution_repository=execution_repository,
                command_service=command_service,
                handlers={StageType.AUDIT: fail},
                backoff_base_seconds=5,
            )
            first_message = PipelineStageMessage.build(
                message_type="pipeline.stage.command",
                job_id="job-phase6-4",
                stage=StageType.AUDIT,
                song_name="Failing Song",
                max_attempts=2,
            )
            retry_result = worker.handle(first_message)
            replay_result = worker.handle(first_message)
            self.assertNotIn(
                "pipeline.stage.audit",
                {event.topic for event in outbox_repository.list_pending()},
            )
            execution = execution_repository.get_by_dedupe_key(first_message.dedupe_key)
            execution_repository.upsert(
                replace(execution, next_retry_at=utc_now() - timedelta(seconds=1))
            )
            schedule_result = Phase6RetryScheduler(
                execution_repository=execution_repository,
                command_service=command_service,
            ).schedule_due()
            retry_message = next(
                PipelineStageMessage.from_payload(event.payload)
                for event in outbox_repository.list_pending()
                if event.topic == "pipeline.stage.audit"
            )
            dlq_result = worker.handle(retry_message)

            self.assertEqual(retry_result.action, "nack_retry")
            self.assertEqual(retry_result.next_retry_seconds, 5)
            self.assertEqual(replay_result.action, "ack_retry_scheduled")
            self.assertEqual(schedule_result["scheduled"], 1)
            self.assertEqual(dlq_result.action, "dlq")
            self.assertEqual(job_repository.get("job-phase6-4").status, JobStatus.FAILED)
            self.assertIn(
                "pipeline.dlq",
                {event.topic for event in outbox_repository.list_pending()},
            )

    def test_phase6_create_task_writes_command_outbox_instead_of_running_background(self):
        with TemporaryDirectory() as temp_root:
            env = {
                "DEEPSEEK_API_KEY": "test-key",
                "DEEPSEEK_BASE_URL": "https://example.local",
                "JOB_REPOSITORY_BACKEND": "sqlalchemy",
                "DATABASE_URL": f"sqlite:///{os.path.join(temp_root, 'phase6-api.db')}",
                "PHASE2_AUTO_CREATE_SCHEMA": "true",
                "PHASE2_SHADOW_WRITE_ENABLED": "false",
                "PHASE6_ASYNC_PIPELINE_ENABLED": "true",
                "PHASE6_SERVICE_WORKER_ENABLED": "false",
                "PHASE6_MAX_STAGE_ATTEMPTS": "4",
                "PHASE2_OUTBOX_DISPATCH_ENABLED": "false",
                "RABBITMQ_URL": "",
            }
            with patch.dict(os.environ, env, clear=False):
                app = api_service.create_app()
                with TestClient(app) as client:
                    response = client.post(
                        "/create_task",
                        json={"song_name": "Async API Song", "candidate_id": "candidate-api-1"},
                        headers={"X-Correlation-Id": "trace-phase7-contract"},
                    )

            self.assertEqual(response.status_code, 200)
            self.assertIn("异步 pipeline", response.json()["message"])
            self.assertEqual(response.headers["Deprecation"], "true")
            self.assertEqual(response.headers["X-Correlation-Id"], "trace-phase7-contract")
            outbox_repository = SQLAlchemyOutboxRepository(app.state.session_factory)
            pending = [
                event
                for event in outbox_repository.list_pending()
                if event.topic == "pipeline.command"
            ]
            self.assertEqual(len(pending), 1)
            message = PipelineStageMessage.from_payload(pending[0].payload)
            self.assertEqual(message.stage, StageType.DOWNLOAD)
            self.assertEqual(message.retry.max_attempts, 4)
            self.assertEqual(message.review.candidate_id, "candidate-api-1")
            self.assertEqual(message.trace_id, "trace-phase7-contract")
            self.assertEqual(pending[0].correlation_id, "trace-phase7-contract")

    def test_add_candidate_to_pipeline_queues_phase6_transcript_work(self):
        with TemporaryDirectory() as temp_root:
            env = {
                "DEEPSEEK_API_KEY": "test-key",
                "DEEPSEEK_BASE_URL": "https://example.local",
                "JOB_REPOSITORY_BACKEND": "sqlalchemy",
                "DATABASE_URL": f"sqlite:///{os.path.join(temp_root, 'phase6-add-candidate.db')}",
                "PHASE2_AUTO_CREATE_SCHEMA": "true",
                "PHASE6_ASYNC_PIPELINE_ENABLED": "true",
                "PHASE6_SERVICE_WORKER_ENABLED": "false",
                "PHASE6_MAX_STAGE_ATTEMPTS": "4",
                "MEDIA_TEMP_ROOT": os.path.join(temp_root, "temp"),
                "MEDIA_OUTPUT_ROOT": os.path.join(temp_root, "output"),
                "PHASE2_OUTBOX_DISPATCH_ENABLED": "false",
                "RABBITMQ_URL": "",
            }
            providers = Phase3Providers(
                followed_artists_lookup=lambda: [],
                channel_lookup=lambda artist: artist.yt_channel_id or "UC_PHASE6_ADD",
                candidate_lookup=lambda artist, days: [
                    CandidateDiscoveryPayload(
                        video_id="video-phase6-add",
                        title="Phase 6 Add Candidate Video",
                        source_url="https://youtube.test/watch?v=phase6-add",
                    )
                ],
            )
            with patch.dict(os.environ, env, clear=False), patch(
                "api.service.create_default_producer_backend",
                FakeProducerBackend,
            ):
                app = api_service.create_app(phase3_providers=providers)
                from domain.entities import Artist

                app.state.phase3_catalog_service.artist_repository.upsert(
                    Artist(spotify_id="artist-phase6-add", name="Phase Six Add")
                )
                app.state.phase3_catalog_service.resync_artist("artist-phase6-add", trigger="manual")
                candidate_id = app.state.phase3_catalog_service.candidate_repository.list_for_artist(
                    "artist-phase6-add"
                )[0].candidate_id

                with TestClient(app) as client:
                    response = client.post(
                        f"/v1/candidates/{candidate_id}/pipeline",
                        headers={"X-Correlation-Id": "trace-add-pipeline"},
                    )

                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["candidate_status"], "downloading")
                    self.assertIn("异步 pipeline", response.json()["message"])
                    self.assertIsNotNone(response.json()["task_id"])

                    outbox_repository = SQLAlchemyOutboxRepository(app.state.session_factory)
                    pending = [
                        event
                        for event in outbox_repository.list_pending()
                        if event.aggregate_id == response.json()["task_id"]
                        and event.topic == "pipeline.command"
                    ]
                    self.assertEqual(len(pending), 1)
                    message = PipelineStageMessage.from_payload(pending[0].payload)
                    self.assertEqual(message.stage, StageType.DOWNLOAD)
                    self.assertEqual(message.review.candidate_id, candidate_id)
                    self.assertEqual(message.trace_id, "trace-add-pipeline")

                    pipeline_item = client.get("/v1/pipeline").json()["items"][0]
                    self.assertEqual(pipeline_item["candidate_id"], candidate_id)
                    self.assertEqual(pipeline_item["pipeline_activity"]["job_id"], response.json()["task_id"])
                    self.assertTrue(
                        any(
                            "queued in outbox" in log["message"]
                            for log in pipeline_item["pipeline_activity"]["logs"]
                        )
                    )

    def test_add_candidate_to_pipeline_dispatches_outbox_when_publisher_is_configured(self):
        with TemporaryDirectory() as temp_root:
            env = {
                "DEEPSEEK_API_KEY": "test-key",
                "DEEPSEEK_BASE_URL": "https://example.local",
                "JOB_REPOSITORY_BACKEND": "sqlalchemy",
                "DATABASE_URL": f"sqlite:///{os.path.join(temp_root, 'phase6-dispatch-candidate.db')}",
                "PHASE2_AUTO_CREATE_SCHEMA": "true",
                "PHASE6_ASYNC_PIPELINE_ENABLED": "true",
                "PHASE6_SERVICE_WORKER_ENABLED": "false",
                "PHASE6_MAX_STAGE_ATTEMPTS": "4",
                "MEDIA_TEMP_ROOT": os.path.join(temp_root, "temp"),
                "MEDIA_OUTPUT_ROOT": os.path.join(temp_root, "output"),
            }
            providers = Phase3Providers(
                followed_artists_lookup=lambda: [],
                channel_lookup=lambda artist: artist.yt_channel_id or "UC_PHASE6_DISPATCH",
                candidate_lookup=lambda artist, days: [
                    CandidateDiscoveryPayload(
                        video_id="video-phase6-dispatch",
                        title="Phase 6 Dispatch Candidate Video",
                        source_url="https://youtube.test/watch?v=phase6-dispatch",
                    )
                ],
            )
            publisher = RecordingPublisher()
            with patch.dict(os.environ, env, clear=False), patch(
                "api.service.create_default_producer_backend",
                FakeProducerBackend,
            ):
                app = api_service.create_app(
                    outbox_publisher=publisher,
                    phase3_providers=providers,
                )
                from domain.entities import Artist

                app.state.phase3_catalog_service.artist_repository.upsert(
                    Artist(spotify_id="artist-phase6-dispatch", name="Phase Six Dispatch")
                )
                app.state.phase3_catalog_service.resync_artist("artist-phase6-dispatch", trigger="manual")
                candidate_id = app.state.phase3_catalog_service.candidate_repository.list_for_artist(
                    "artist-phase6-dispatch"
                )[0].candidate_id

                with TestClient(app) as client:
                    response = client.post(
                        f"/v1/candidates/{candidate_id}/pipeline",
                        headers={"X-Correlation-Id": "trace-dispatch-pipeline"},
                    )

            self.assertEqual(response.status_code, 200)
            self.assertTrue(
                any(published["topic"] == "pipeline.command" for published in publisher.published)
            )
            command_payload = next(
                published["payload"]
                for published in publisher.published
                if published["topic"] == "pipeline.command"
            )
            message = PipelineStageMessage.from_payload(command_payload)
            self.assertEqual(message.stage, StageType.DOWNLOAD)
            self.assertEqual(message.review.candidate_id, candidate_id)
            self.assertEqual(message.trace_id, "trace-dispatch-pipeline")
            outbox_repository = SQLAlchemyOutboxRepository(app.state.session_factory)
            pending = [
                event
                for event in outbox_repository.list_pending()
                if event.aggregate_id == response.json()["task_id"]
                and event.topic == "pipeline.command"
            ]
            self.assertEqual(pending, [])

    def test_service_worker_auto_runs_after_candidate_is_added_to_pipeline(self):
        with TemporaryDirectory() as temp_root:
            env = {
                "DEEPSEEK_API_KEY": "test-key",
                "DEEPSEEK_BASE_URL": "https://example.local",
                "JOB_REPOSITORY_BACKEND": "sqlalchemy",
                "DATABASE_URL": f"sqlite:///{os.path.join(temp_root, 'phase6-inline-worker.db')}",
                "PHASE2_AUTO_CREATE_SCHEMA": "true",
                "PHASE6_ASYNC_PIPELINE_ENABLED": "true",
                "PHASE6_SERVICE_WORKER_ENABLED": "true",
                "PHASE6_SERVICE_WORKER_POLL_SECONDS": "0.01",
                "MEDIA_TEMP_ROOT": os.path.join(temp_root, "temp"),
                "MEDIA_OUTPUT_ROOT": os.path.join(temp_root, "output"),
                "PHASE2_OUTBOX_DISPATCH_ENABLED": "false",
                "RABBITMQ_URL": "",
            }
            providers = Phase3Providers(
                followed_artists_lookup=lambda: [],
                channel_lookup=lambda artist: artist.yt_channel_id or "UC_PHASE6_INLINE",
                candidate_lookup=lambda artist, days: [
                    CandidateDiscoveryPayload(
                        video_id="video-phase6-inline",
                        title="Phase 6 Inline Candidate Video",
                        source_url="https://youtube.test/watch?v=phase6-inline",
                    )
                ],
            )
            with patch.dict(os.environ, env, clear=False), patch(
                "api.service.create_default_producer_backend",
                FakeProducerBackend,
            ):
                app = api_service.create_app(phase3_providers=providers)
                from domain.entities import Artist

                app.state.phase3_catalog_service.artist_repository.upsert(
                    Artist(spotify_id="artist-phase6-inline", name="Phase Six Inline")
                )
                app.state.phase3_catalog_service.resync_artist("artist-phase6-inline", trigger="manual")
                candidate_id = app.state.phase3_catalog_service.candidate_repository.list_for_artist(
                    "artist-phase6-inline"
                )[0].candidate_id

                with TestClient(app) as client:
                    response = client.post(f"/v1/candidates/{candidate_id}/pipeline")
                    self.assertEqual(response.status_code, 200)
                    self.assertIsNotNone(response.json()["task_id"])
                    self._wait_for_stage(app.state.session_factory, candidate_id, StageType.MANUAL_REVIEW)

                    pipeline_item = client.get("/v1/pipeline").json()["items"][0]
                    self.assertEqual(pipeline_item["candidate_id"], candidate_id)
                    self.assertEqual(pipeline_item["async_execution"]["current_stage"], "manual_review")
                    self.assertEqual(pipeline_item["async_execution"]["pause_reason"], "manual_review_pending")

    def test_phase7_health_readiness_reports_dependency_statuses(self):
        with TemporaryDirectory() as temp_root:
            env = {
                "DEEPSEEK_API_KEY": "test-key",
                "DEEPSEEK_BASE_URL": "https://example.local",
                "JOB_REPOSITORY_BACKEND": "sqlalchemy",
                "DATABASE_URL": f"sqlite:///{os.path.join(temp_root, 'phase7-health.db')}",
                "PHASE2_AUTO_CREATE_SCHEMA": "true",
                "PHASE2_SHADOW_WRITE_ENABLED": "false",
                "PHASE2_RECONCILE_ENABLED": "false",
                "PHASE2_OUTBOX_DISPATCH_ENABLED": "false",
                "PHASE6_ASYNC_PIPELINE_ENABLED": "false",
                "MEDIA_STORAGE_BACKEND": "local",
                "MEDIA_TEMP_ROOT": os.path.join(temp_root, "temp"),
                "MEDIA_OUTPUT_ROOT": os.path.join(temp_root, "output"),
                "RABBITMQ_URL": "",
                "QDRANT_URL": "",
            }
            with patch.dict(os.environ, env, clear=False):
                app = api_service.create_app()
                with TestClient(app) as client:
                    live_response = client.get("/healthz")
                    ready_response = client.get("/readyz")

            self.assertEqual(live_response.status_code, 200)
            self.assertEqual(live_response.json()["status"], "ok")
            self.assertEqual(ready_response.status_code, 503)
            payload = ready_response.json()
            self.assertEqual(payload["status"], "degraded")
            self.assertEqual(payload["checks"]["db"]["status"], "ok")
            self.assertEqual(payload["checks"]["oss"]["status"], "ok")
            self.assertEqual(payload["checks"]["rabbitmq"]["status"], "skipped")

    def test_phase7_health_service_reports_dependency_failures(self):
        class QueueProbe:
            def collect_depths(self):
                raise RuntimeError("rabbitmq unavailable")

        class BrokenLocalStorage:
            storage_provider = "local-oss"
            temp_root = "/not-allowed/phase7-temp"
            output_root = "/not-allowed/phase7-output"
            bucket = "test"

        result = Phase7HealthService(
            session_factory=None,
            media_storage=BrokenLocalStorage(),
            queue_probe=QueueProbe(),
        ).readiness()

        self.assertEqual(result.status, "degraded")
        self.assertEqual(result.checks["db"]["status"], "skipped")
        self.assertEqual(result.checks["rabbitmq"]["status"], "failed")
        self.assertEqual(result.checks["oss"]["status"], "failed")

    def test_phase7_legacy_status_and_list_endpoints_publish_deprecation_headers(self):
        env = {
            "DEEPSEEK_API_KEY": "test-key",
            "DEEPSEEK_BASE_URL": "https://example.local",
            "JOB_REPOSITORY_BACKEND": "memory",
            "DATABASE_URL": "",
            "PHASE2_SHADOW_WRITE_ENABLED": "false",
                "PHASE2_RECONCILE_ENABLED": "false",
                "PHASE2_OUTBOX_DISPATCH_ENABLED": "false",
                "PHASE6_ASYNC_PIPELINE_ENABLED": "false",
                "MEDIA_STORAGE_BACKEND": "local",
            }
        with patch.dict(os.environ, env, clear=False):
            app = api_service.create_app()
            job = app.state.job_service.create_job("Legacy Song")
            with TestClient(app) as client:
                status_response = client.get(f"/check_status/{job.job_id}")
                not_found_response = client.get("/check_status/missing-phase7")
                list_response = client.get("/list_tasks")

        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.headers["Deprecation"], "true")
        self.assertIn("/v1/pipeline", status_response.headers["Link"])
        self.assertEqual(not_found_response.status_code, 404)
        self.assertEqual(not_found_response.headers["Deprecation"], "true")
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.headers["Sunset"], api_service.LEGACY_SUNSET_HTTP_DATE)

    def test_phase6_candidate_render_pauses_and_resumes_review_gates(self):
        with TemporaryDirectory() as temp_root:
            env = {
                "DEEPSEEK_API_KEY": "test-key",
                "DEEPSEEK_BASE_URL": "https://example.local",
                "JOB_REPOSITORY_BACKEND": "sqlalchemy",
                "DATABASE_URL": f"sqlite:///{os.path.join(temp_root, 'phase6-gates.db')}",
                "PHASE2_AUTO_CREATE_SCHEMA": "true",
                "PHASE6_ASYNC_PIPELINE_ENABLED": "true",
                "PHASE6_SERVICE_WORKER_ENABLED": "false",
                "MEDIA_TEMP_ROOT": os.path.join(temp_root, "temp"),
                "MEDIA_OUTPUT_ROOT": os.path.join(temp_root, "output"),
                "PHASE2_OUTBOX_DISPATCH_ENABLED": "false",
                "RABBITMQ_URL": "",
            }
            providers = Phase3Providers(
                followed_artists_lookup=lambda: [],
                channel_lookup=lambda artist: artist.yt_channel_id or "UC_PHASE6",
                candidate_lookup=lambda artist, days: [
                    CandidateDiscoveryPayload(
                        video_id="video-phase6-gate",
                        title="Phase 6 Gate Video",
                        source_url="https://youtube.test/watch?v=phase6",
                    )
                ],
            )
            with patch.dict(os.environ, env, clear=False), patch(
                "api.service.create_default_producer_backend",
                FakeProducerBackend,
            ):
                app = api_service.create_app(phase3_providers=providers)
                from domain.entities import Artist

                app.state.phase3_catalog_service.artist_repository.upsert(
                    Artist(spotify_id="artist-phase6", name="Phase Six")
                )
                app.state.phase3_catalog_service.resync_artist("artist-phase6", trigger="manual")
                candidate_id = app.state.phase3_catalog_service.candidate_repository.list_for_artist(
                    "artist-phase6"
                )[0].candidate_id

                with TestClient(app) as client:
                    render_response = client.post(f"/v1/candidates/{candidate_id}/render")
                    self.assertEqual(render_response.status_code, 200)

                    outbox_repository = SQLAlchemyOutboxRepository(app.state.session_factory)
                    _command_service, worker = app.state.phase6_async_pipeline_services

                    for expected_stage in (
                        StageType.DOWNLOAD,
                        StageType.TRANSCRIBE,
                        StageType.AUDIT,
                        StageType.MANUAL_REVIEW,
                    ):
                        event = self._next_stage_event(outbox_repository, expected_stage)
                        result = worker.handle_payload(event.payload)
                        self.assertEqual(result.action, "ack")

                    pipeline_payload = client.get("/v1/pipeline").json()
                    item = next(item for item in pipeline_payload["items"] if item["candidate_id"] == candidate_id)
                    self.assertEqual(item["current_stage"], "manual_review")
                    self.assertEqual(item["async_execution"]["current_stage"], "manual_review")
                    self.assertEqual(item["async_execution"]["pause_reason"], "manual_review_pending")

                    manual_review = next(
                        review
                        for review in client.get("/v1/audit-queue").json()["items"]
                        if review["candidate_id"] == candidate_id
                        and review["review_type"] == "manual_review"
                    )
                    approve_manual = client.post(
                        f"/v1/reviews/{manual_review['review_id']}/approve",
                        json={"expected_version": manual_review["version"], "comment": "resume"},
                    )
                    self.assertEqual(approve_manual.status_code, 200)

                    translate_event = self._next_stage_event(outbox_repository, StageType.TRANSLATE)
                    translate_message = PipelineStageMessage.from_payload(translate_event.payload)
                    self.assertEqual(translate_message.review.candidate_id, candidate_id)
                    self.assertIn("segments", translate_message.payload)
                    self.assertNotIn("pause_reason", translate_message.payload)
                    self.assertEqual(worker.handle_payload(translate_event.payload).action, "ack")

                    translation_gate_event = self._next_stage_event(outbox_repository, StageType.TRANSLATION_REVIEW)
                    self.assertEqual(worker.handle_payload(translation_gate_event.payload).action, "ack")
                    translation_review = next(
                        review
                        for review in client.get("/v1/audit-queue").json()["items"]
                        if review["candidate_id"] == candidate_id
                        and review["review_type"] == "translation_review"
                    )
                    approve_translation = client.post(
                        f"/v1/reviews/{translation_review['review_id']}/approve",
                        json={"expected_version": translation_review["version"], "comment": "render"},
                    )
                    self.assertEqual(approve_translation.status_code, 200)

                    render_event = self._next_stage_event(outbox_repository, StageType.RENDER)
                    render_message = PipelineStageMessage.from_payload(render_event.payload)
                    self.assertEqual(render_message.review.candidate_id, candidate_id)
                    self.assertNotIn("pause_reason", render_message.payload)
                    self.assertEqual(worker.handle_payload(render_event.payload).action, "ack")
                    activity_logs = client.get("/v1/pipeline").json()["items"][0]["pipeline_activity"]["logs"]
                    self.assertFalse(
                        any("render: completed (translation_review_pending)" in log["message"] for log in activity_logs)
                    )

    def _next_stage_event(self, outbox_repository, stage: StageType):
        for event in reversed(outbox_repository.list_pending()):
            try:
                message = PipelineStageMessage.from_payload(event.payload)
            except Exception:
                continue
            if message.stage == stage:
                return event
        raise AssertionError(f"No outbox event found for stage {stage.value}")

    def _wait_for_stage(self, session_factory, candidate_id: str, stage: StageType):
        repository = SQLAlchemyPipelineStageExecutionRepository(session_factory)
        deadline = time.time() + 5
        while time.time() < deadline:
            if any(execution.stage == stage for execution in repository.list_for_candidate(candidate_id)):
                return
            time.sleep(0.05)
        raise AssertionError(f"Timed out waiting for stage {stage.value}")

    def test_phase6_rabbitmq_topology_declares_stage_queues_with_dlq(self):
        class Channel:
            def __init__(self):
                self.exchanges = []
                self.queues = []
                self.bindings = []

            def exchange_declare(self, **kwargs):
                self.exchanges.append(kwargs)

            def queue_declare(self, **kwargs):
                self.queues.append(kwargs)

            def queue_bind(self, **kwargs):
                self.bindings.append(kwargs)

        channel = Channel()
        topology = PipelineQueueTopology()

        RabbitMQTopologyManager(
            RabbitMQTopologyConfig(url="amqp://guest:guest@localhost:5672/", topology=topology)
        ).declare_on_channel(channel)

        self.assertEqual(channel.exchanges[0]["exchange"], "pipeline")
        declared_queues = {item["queue"]: item for item in channel.queues}
        self.assertIn("pipeline.command", declared_queues)
        self.assertIn("pipeline.stage.download", declared_queues)
        self.assertIn("pipeline.dlq", declared_queues)
        self.assertEqual(
            declared_queues["pipeline.stage.download"]["arguments"]["x-dead-letter-routing-key"],
            "pipeline.dlq",
        )
        self.assertIsNone(declared_queues["pipeline.dlq"]["arguments"])
        self.assertIn(
            {
                "exchange": "pipeline",
                "queue": "pipeline.stage.render",
                "routing_key": "pipeline.stage.render",
            },
            channel.bindings,
        )

    def test_phase6_rabbitmq_consumer_acks_and_drains_worker_outbox(self):
        class Method:
            delivery_tag = "delivery-1"

        class Channel:
            def __init__(self):
                self.acks = []
                self.nacks = []
                self.stopped = False

            def basic_ack(self, delivery_tag):
                self.acks.append(delivery_tag)

            def basic_nack(self, delivery_tag, requeue):
                self.nacks.append((delivery_tag, requeue))

            def stop_consuming(self):
                self.stopped = True

        class Worker:
            def __init__(self):
                self.payloads = []

            def handle_payload(self, payload):
                self.payloads.append(payload)

                class Result:
                    action = "ack"
                    job_id = "job-1"
                    stage = "download"

                return Result()

        class Dispatcher:
            def __init__(self):
                self.calls = 0

            def dispatch_pending(self):
                self.calls += 1
                return {"published": 1}

        channel = Channel()
        worker = Worker()
        dispatcher = Dispatcher()
        consumer = RabbitMQWorkerConsumer(
            config=RabbitMQWorkerConfig(
                url="amqp://guest:guest@localhost:5672/",
                queue_name="pipeline.command",
                max_messages=1,
            ),
            worker=worker,
            outbox_dispatcher=dispatcher,
        )

        consumer._handle_delivery(channel, Method(), None, b"{\"hello\": \"world\"}")

        self.assertEqual(worker.payloads, ['{"hello": "world"}'])
        self.assertEqual(dispatcher.calls, 1)
        self.assertEqual(channel.acks, ["delivery-1"])
        self.assertEqual(channel.nacks, [])
        self.assertTrue(channel.stopped)

    def test_phase6_rabbitmq_consumer_nacks_unpersisted_failures_without_requeue(self):
        class Method:
            delivery_tag = "delivery-2"

        class Channel:
            def __init__(self):
                self.acks = []
                self.nacks = []
                self.stopped = False

            def basic_ack(self, delivery_tag):
                self.acks.append(delivery_tag)

            def basic_nack(self, delivery_tag, requeue):
                self.nacks.append((delivery_tag, requeue))

            def stop_consuming(self):
                self.stopped = True

        class Worker:
            def handle_payload(self, payload):
                raise RuntimeError("cannot parse message")

        channel = Channel()
        consumer = RabbitMQWorkerConsumer(
            config=RabbitMQWorkerConfig(
                url="amqp://guest:guest@localhost:5672/",
                queue_name="pipeline.command",
                max_messages=1,
            ),
            worker=Worker(),
        )

        consumer._handle_delivery(channel, Method(), None, b"not-json")

        self.assertEqual(channel.acks, [])
        self.assertEqual(channel.nacks, [("delivery-2", False)])
        self.assertTrue(channel.stopped)

    def test_phase7_observability_reports_queue_depth_stage_latency_and_dlq(self):
        class QueueCollector:
            def collect_depths(self):
                return {
                    "pipeline.command": 2,
                    "pipeline.stage.download": 1,
                    "pipeline.dlq": 3,
                }

        with TemporaryDirectory() as temp_root:
            session_factory = self._session_factory(temp_root)
            job_repository = SQLAlchemyJobRepository(session_factory)
            execution_repository = SQLAlchemyPipelineStageExecutionRepository(session_factory)
            job_repository.create(Job(job_id="job-phase7-1", song_name="Observed Song"))
            now = utc_now()
            execution_repository.upsert(
                PipelineStageExecution(
                    execution_id="stage:observed-1",
                    dedupe_key="observed-1",
                    job_id="job-phase7-1",
                    stage=StageType.DOWNLOAD,
                    status=StageStatus.COMPLETED,
                    locked_at=now - timedelta(seconds=8),
                    completed_at=now,
                    created_at=now - timedelta(seconds=9),
                    updated_at=now,
                )
            )
            execution_repository.upsert(
                PipelineStageExecution(
                    execution_id="stage:observed-2",
                    dedupe_key="observed-2",
                    job_id="job-phase7-1",
                    stage=StageType.TRANSCRIBE,
                    status=StageStatus.DLQ,
                    created_at=now,
                    updated_at=now,
                )
            )

            snapshot = Phase7ObservabilityService(
                session_factory=session_factory,
                queue_depth_collector=QueueCollector(),
            ).snapshot()

            self.assertEqual(snapshot["queue_depth"]["pipeline.command"], 2)
            self.assertEqual(snapshot["dlq_count"], 3)
            self.assertEqual(snapshot["stage_latency_seconds"]["download"]["count"], 1)
            self.assertEqual(snapshot["stage_latency_seconds"]["download"]["avg"], 8.0)
            self.assertEqual(snapshot["stage_status_counts"]["transcribe"]["dlq"], 1)
            self.assertEqual(snapshot["stage_success_failure_rate"]["download"]["success"], 1)
            self.assertEqual(snapshot["stage_success_failure_rate"]["transcribe"]["failure"], 1)
            self.assertEqual(snapshot["retry_count"]["download"], 0)
            self.assertIn("discovery_freshness", snapshot)
            self.assertIn("review_aging_seconds", snapshot)

            metrics = render_prometheus_metrics(snapshot)
            self.assertIn('randy_translation_queue_depth{queue="pipeline.command"} 2', metrics)
            self.assertIn("randy_translation_dlq_count 3", metrics)
            self.assertIn('randy_translation_stage_status_count{stage="transcribe",status="dlq"} 1', metrics)

    def test_phase7_metrics_endpoint_returns_prometheus_text(self):
        with TemporaryDirectory() as temp_root:
            env = {
                "DEEPSEEK_API_KEY": "test-key",
                "DEEPSEEK_BASE_URL": "https://example.local",
                "JOB_REPOSITORY_BACKEND": "sqlalchemy",
                "DATABASE_URL": f"sqlite:///{os.path.join(temp_root, 'phase7-metrics.db')}",
                "PHASE2_AUTO_CREATE_SCHEMA": "true",
                "PHASE2_SHADOW_WRITE_ENABLED": "false",
                "PHASE2_RECONCILE_ENABLED": "false",
                "PHASE2_OUTBOX_DISPATCH_ENABLED": "false",
                "PHASE6_ASYNC_PIPELINE_ENABLED": "false",
                "MEDIA_STORAGE_BACKEND": "local",
                "MEDIA_TEMP_ROOT": os.path.join(temp_root, "temp"),
                "MEDIA_OUTPUT_ROOT": os.path.join(temp_root, "output"),
                "RABBITMQ_URL": "",
            }
            with patch.dict(os.environ, env, clear=False):
                app = api_service.create_app()
                with TestClient(app) as client:
                    response = client.get("/internal/phase7/metrics")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/plain", response.headers["content-type"])
        self.assertIn("randy_translation_queue_depth", response.text)


if __name__ == "__main__":
    unittest.main()
