import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

import api.service as api_service
from application.services.phase3_catalog_service import CandidateDiscoveryPayload, Phase3Providers
from application.services.async_pipeline import AsyncPipelineCommandService, PipelineStageWorker
from domain.entities import Job
from domain.enums import JobStatus, OutboxStatus, StageStatus, StageType
from domain.message_contracts import PipelineStageMessage, ReviewContext
from domain.queue_topology import PipelineQueueTopology, STAGE_ORDER, next_stage
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

    def generate_bilingual_srt(self, segments, english_texts, output_file: str):
        with open(output_file, "w", encoding="utf-8") as file_obj:
            file_obj.write("1\n00:00:00,000 --> 00:00:01,000\nhello\n你好\n\n")
        return output_file

    def burn_video(self, video_ref, srt_file: str, final_path: str):
        with open(final_path, "w", encoding="utf-8") as file_obj:
            file_obj.write("final")


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
            retry_message = next(
                PipelineStageMessage.from_payload(event.payload)
                for event in outbox_repository.list_pending()
                if event.topic == "pipeline.stage.audit"
            )
            dlq_result = worker.handle(retry_message)

            self.assertEqual(retry_result.action, "nack_retry")
            self.assertEqual(retry_result.next_retry_seconds, 5)
            self.assertEqual(replay_result.action, "ack_retry_scheduled")
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
                "PHASE6_ASYNC_PIPELINE_ENABLED": "true",
                "PHASE6_MAX_STAGE_ATTEMPTS": "4",
            }
            with patch.dict(os.environ, env, clear=False):
                app = api_service.create_app()
                with TestClient(app) as client:
                    response = client.post(
                        "/create_task",
                        json={"song_name": "Async API Song", "candidate_id": "candidate-api-1"},
                    )

            self.assertEqual(response.status_code, 200)
            self.assertIn("异步 pipeline", response.json()["message"])
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

    def test_phase6_candidate_render_pauses_and_resumes_review_gates(self):
        with TemporaryDirectory() as temp_root:
            env = {
                "DEEPSEEK_API_KEY": "test-key",
                "DEEPSEEK_BASE_URL": "https://example.local",
                "JOB_REPOSITORY_BACKEND": "sqlalchemy",
                "DATABASE_URL": f"sqlite:///{os.path.join(temp_root, 'phase6-gates.db')}",
                "PHASE2_AUTO_CREATE_SCHEMA": "true",
                "PHASE6_ASYNC_PIPELINE_ENABLED": "true",
                "MEDIA_TEMP_ROOT": os.path.join(temp_root, "temp"),
                "MEDIA_OUTPUT_ROOT": os.path.join(temp_root, "output"),
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
                    self.assertEqual(
                        PipelineStageMessage.from_payload(translate_event.payload).review.candidate_id,
                        candidate_id,
                    )
                    self.assertIn("segments", PipelineStageMessage.from_payload(translate_event.payload).payload)

    def _next_stage_event(self, outbox_repository, stage: StageType):
        for event in reversed(outbox_repository.list_pending()):
            try:
                message = PipelineStageMessage.from_payload(event.payload)
            except Exception:
                continue
            if message.stage == stage:
                return event
        raise AssertionError(f"No outbox event found for stage {stage.value}")

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


if __name__ == "__main__":
    unittest.main()
