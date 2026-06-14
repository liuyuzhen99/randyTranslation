from __future__ import annotations

import argparse
from multiprocessing import Process
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.config import (  # noqa: E402
    create_artifact_repository,
    create_media_storage,
    create_review_workflow_services,
    create_async_pipeline_services,
    create_sqlalchemy_session_factory,
    create_vector_repository,
    load_runtime_settings,
)
from application.services.outbox_dispatcher import OutboxDispatcher  # noqa: E402
from application.services.retry_scheduler import PipelineRetryScheduler  # noqa: E402
from domain.queue_topology import PipelineQueueTopology  # noqa: E402
from infrastructure.messaging.rabbitmq_consumer import RabbitMQWorkerConfig, RabbitMQWorkerConsumer  # noqa: E402
from infrastructure.messaging.rabbitmq_publisher import RabbitMQPublishConfig, RabbitMQPublisher  # noqa: E402
from infrastructure.messaging.rabbitmq_topology import RabbitMQTopologyConfig, RabbitMQTopologyManager  # noqa: E402
from infrastructure.pipeline.legacy_producer_adapter import create_default_producer_backend  # noqa: E402
from infrastructure.persistence.sqlalchemy_repositories import SQLAlchemyJobRepository  # noqa: E402
from infrastructure.persistence.sqlalchemy_repositories import SQLAlchemyOutboxRepository  # noqa: E402
from infrastructure.persistence.sqlalchemy_repositories import SQLAlchemyPipelineStageExecutionRepository  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run pipeline RabbitMQ worker.")
    parser.add_argument(
        "--queue",
        default="pipeline.command",
        help="RabbitMQ queue to consume, for example pipeline.command or pipeline.stage.download.",
    )
    parser.add_argument("--max-messages", type=int, default=None)
    parser.add_argument("--prefetch", type=int, default=1)
    parser.add_argument("--instances", type=int, default=1)
    parser.add_argument("--declare-only", action="store_true")
    parser.add_argument("--schedule-retries", action="store_true")
    parser.add_argument("--retry-limit", type=int, default=100)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.instances < 1:
        raise RuntimeError("--instances must be at least 1.")
    settings = load_runtime_settings()
    rabbitmq_url = os.environ.get("RABBITMQ_URL", "").strip() or "amqp://guest:guest@localhost:5672/"

    topology = PipelineQueueTopology()
    manager = RabbitMQTopologyManager(RabbitMQTopologyConfig(url=rabbitmq_url, topology=topology))
    if args.declare_only:
        print(manager.declare())
        return 0

    if args.schedule_retries:
        session_factory = create_sqlalchemy_session_factory(runtime_settings=settings)
        if session_factory is None:
            raise RuntimeError("DATABASE_URL is required to schedule Pipeline retries.")
        command_service = create_async_pipeline_services(
            runtime_settings=settings,
            session_factory=session_factory,
        )
        if command_service is None:
            raise RuntimeError("ASYNC_PIPELINE_ENABLED=true is required to schedule retries.")
        scheduler = PipelineRetryScheduler(
            execution_repository=SQLAlchemyPipelineStageExecutionRepository(session_factory),
            command_service=command_service[0],
        )
        print(scheduler.schedule_due(limit=args.retry_limit))
        return 0

    if args.instances > 1:
        processes = [
            Process(target=_run_consumer, args=(args.queue, args.max_messages, args.prefetch), daemon=False)
            for _ in range(args.instances)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join()
        return max((process.exitcode or 0) for process in processes)

    print(_run_consumer(args.queue, args.max_messages, args.prefetch))
    return 0


def _run_consumer(queue_name: str, max_messages: int | None, prefetch_count: int) -> dict[str, int | str]:
    settings = load_runtime_settings()
    rabbitmq_url = os.environ.get("RABBITMQ_URL", "").strip() or "amqp://guest:guest@localhost:5672/"
    topology = PipelineQueueTopology()
    session_factory = create_sqlalchemy_session_factory(runtime_settings=settings)
    if session_factory is None:
        raise RuntimeError("DATABASE_URL is required to run the pipeline worker.")

    job_repository = SQLAlchemyJobRepository(session_factory)
    media_storage = create_media_storage(runtime_settings=settings)
    artifact_repository = create_artifact_repository(
        runtime_settings=settings,
        session_factory=session_factory,
    )
    workflow_services = create_review_workflow_services(
        runtime_settings=settings,
        session_factory=session_factory,
    )
    vector_repository = create_vector_repository(runtime_settings=settings)
    services = create_async_pipeline_services(
        runtime_settings=settings,
        session_factory=session_factory,
        job_repository=job_repository,
        media_storage=media_storage,
        producer_backend_factory=create_default_producer_backend,
        workflow_services=workflow_services,
        artifact_repository=artifact_repository,
        vector_repository=vector_repository,
    )
    if services is None:
        raise RuntimeError("ASYNC_PIPELINE_ENABLED=true is required to run the worker.")

    _command_service, worker = services
    publisher = RabbitMQPublisher(RabbitMQPublishConfig(url=rabbitmq_url))
    outbox_dispatcher = OutboxDispatcher(SQLAlchemyOutboxRepository(session_factory), publisher)
    consumer = RabbitMQWorkerConsumer(
        config=RabbitMQWorkerConfig(
            url=rabbitmq_url,
            queue_name=queue_name,
            max_messages=max_messages,
            prefetch_count=prefetch_count,
            topology=topology,
        ),
        worker=worker,
        outbox_dispatcher=outbox_dispatcher,
    )
    return consumer.consume()


if __name__ == "__main__":
    raise SystemExit(main())
