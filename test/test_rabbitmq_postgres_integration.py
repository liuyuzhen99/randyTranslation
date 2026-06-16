import os
import unittest
from dataclasses import replace
from uuid import uuid4

from application.services.async_pipeline import AsyncPipelineCommandService, PipelineStageWorker
from domain.entities import Job
from domain.enums import OutboxStatus, StageStatus, StageType
from domain.message_contracts import PipelineStageMessage
from domain.queue_topology import PipelineQueueTopology
from infrastructure.messaging.rabbitmq_consumer import RabbitMQWorkerConfig, RabbitMQWorkerConsumer
from infrastructure.messaging.rabbitmq_publisher import RabbitMQPublishConfig, RabbitMQPublisher
from infrastructure.messaging.rabbitmq_topology import RabbitMQTopologyConfig, RabbitMQTopologyManager
from infrastructure.persistence.sqlalchemy_repositories import (
    SQLAlchemyJobRepository,
    SQLAlchemyOutboxRepository,
    SQLAlchemyPipelineStageExecutionRepository,
    SQLAlchemySessionFactory,
)


RUN_INTEGRATION = (
    os.environ.get(
        "RUN_PIPELINE_RABBITMQ_POSTGRES_INTEGRATION",
        os.environ.get("RUN_PHASE6_RABBITMQ_POSTGRES_INTEGRATION", ""),
    ).lower()
    in {
        "1",
        "true",
        "yes",
        "on",
    }
)


class RabbitMQPostgresIntegrationTests(unittest.TestCase):
    def test_rabbitmq_postgres_worker_round_trip(self):
        if not RUN_INTEGRATION:
            self.skipTest(
                "Set RUN_PIPELINE_RABBITMQ_POSTGRES_INTEGRATION=true with DATABASE_URL and RABBITMQ_URL to run."
            )
        database_url = os.environ["DATABASE_URL"]
        rabbitmq_url = os.environ["RABBITMQ_URL"]
        self.assertTrue(
            database_url.startswith("postgresql"),
            "This integration test is intended for PostgreSQL DATABASE_URL.",
        )

        topology = PipelineQueueTopology()
        RabbitMQTopologyManager(
            RabbitMQTopologyConfig(url=rabbitmq_url, topology=topology)
        ).declare()
        self._purge_pipeline_queues(rabbitmq_url, topology)

        session_factory = SQLAlchemySessionFactory(database_url)
        session_factory.create_schema()
        job_repository = SQLAlchemyJobRepository(session_factory)
        outbox_repository = SQLAlchemyOutboxRepository(session_factory)
        execution_repository = SQLAlchemyPipelineStageExecutionRepository(session_factory)
        command_service = AsyncPipelineCommandService(outbox_repository=outbox_repository)

        job = Job(job_id=f"pipeline-it-{uuid4().hex[:12]}", song_name="RabbitMQ Postgres IT")
        job_repository.create(job)
        first_message = command_service.enqueue_first_stage(job)
        first_event = outbox_repository.get(f"pipeline.stage.command:{first_message.dedupe_key}")
        publisher = RabbitMQPublisher(RabbitMQPublishConfig(url=rabbitmq_url))
        publisher.publish(
            topic=topology.command_queue,
            payload=first_event.payload,
            correlation_id=first_event.correlation_id,
        )
        outbox_repository.update(replace(first_event, status=OutboxStatus.PUBLISHED))

        calls = []
        worker = PipelineStageWorker(
            job_repository=job_repository,
            execution_repository=execution_repository,
            command_service=command_service,
            handlers={StageType.DOWNLOAD: lambda message: calls.append(message.dedupe_key) or {"ok": True}},
        )
        consumer = RabbitMQWorkerConsumer(
            config=RabbitMQWorkerConfig(
                url=rabbitmq_url,
                queue_name=topology.command_queue,
                max_messages=1,
                topology=topology,
            ),
            worker=worker,
        )

        result = consumer.consume()
        execution = execution_repository.get_by_dedupe_key(first_message.dedupe_key)
        next_dedupe_key = f"pipeline:{job.job_id}:transcribe:attempt:0"
        next_event = outbox_repository.get(f"pipeline.stage.command:{next_dedupe_key}")

        self.assertEqual(result["processed"], 1)
        self.assertEqual(calls, [first_message.dedupe_key])
        self.assertEqual(execution.status, StageStatus.COMPLETED)
        self.assertIsNotNone(next_event)
        self.assertEqual(PipelineStageMessage.from_payload(next_event.payload).stage, StageType.TRANSCRIBE)

    def _purge_pipeline_queues(self, rabbitmq_url: str, topology: PipelineQueueTopology) -> None:
        import pika

        connection = pika.BlockingConnection(pika.URLParameters(rabbitmq_url))
        try:
            channel = connection.channel()
            for binding in topology.bindings():
                channel.queue_purge(queue=binding.queue_name)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
