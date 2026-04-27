from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.config import (  # noqa: E402
    create_artifact_repository,
    create_media_storage,
    create_phase4_workflow_services,
    create_phase6_async_pipeline_services,
    create_sqlalchemy_session_factory,
    load_runtime_settings,
)
from application.services.outbox_dispatcher import OutboxDispatcher  # noqa: E402
from domain.queue_topology import PipelineQueueTopology  # noqa: E402
from infrastructure.messaging.rabbitmq_consumer import RabbitMQWorkerConfig, RabbitMQWorkerConsumer  # noqa: E402
from infrastructure.messaging.rabbitmq_publisher import RabbitMQPublishConfig, RabbitMQPublisher  # noqa: E402
from infrastructure.messaging.rabbitmq_topology import RabbitMQTopologyConfig, RabbitMQTopologyManager  # noqa: E402
from infrastructure.pipeline.legacy_producer_adapter import create_default_producer_backend  # noqa: E402
from infrastructure.persistence.sqlalchemy_repositories import SQLAlchemyJobRepository  # noqa: E402
from infrastructure.persistence.sqlalchemy_repositories import SQLAlchemyOutboxRepository  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Phase 6 RabbitMQ pipeline worker.")
    parser.add_argument(
        "--queue",
        default="pipeline.command",
        help="RabbitMQ queue to consume, for example pipeline.command or pipeline.stage.download.",
    )
    parser.add_argument("--max-messages", type=int, default=None)
    parser.add_argument("--declare-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = load_runtime_settings()
    rabbitmq_url = os.environ.get("RABBITMQ_URL", "").strip() or "amqp://guest:guest@localhost:5672/"

    topology = PipelineQueueTopology()
    manager = RabbitMQTopologyManager(RabbitMQTopologyConfig(url=rabbitmq_url, topology=topology))
    if args.declare_only:
        print(manager.declare())
        return 0

    session_factory = create_sqlalchemy_session_factory(runtime_settings=settings)
    if session_factory is None:
        raise RuntimeError("DATABASE_URL is required to run the Phase 6 worker.")

    job_repository = SQLAlchemyJobRepository(session_factory)
    media_storage = create_media_storage(runtime_settings=settings)
    artifact_repository = create_artifact_repository(
        runtime_settings=settings,
        session_factory=session_factory,
    )
    workflow_services = create_phase4_workflow_services(
        runtime_settings=settings,
        session_factory=session_factory,
    )
    services = create_phase6_async_pipeline_services(
        runtime_settings=settings,
        session_factory=session_factory,
        job_repository=job_repository,
        media_storage=media_storage,
        producer_backend_factory=create_default_producer_backend,
        workflow_services=workflow_services,
        artifact_repository=artifact_repository,
    )
    if services is None:
        raise RuntimeError("PHASE6_ASYNC_PIPELINE_ENABLED=true is required to run the worker.")

    _command_service, worker = services
    publisher = RabbitMQPublisher(RabbitMQPublishConfig(url=rabbitmq_url))
    outbox_dispatcher = OutboxDispatcher(SQLAlchemyOutboxRepository(session_factory), publisher)
    consumer = RabbitMQWorkerConsumer(
        config=RabbitMQWorkerConfig(
            url=rabbitmq_url,
            queue_name=args.queue,
            max_messages=args.max_messages,
            topology=topology,
        ),
        worker=worker,
        outbox_dispatcher=outbox_dispatcher,
    )
    print(consumer.consume())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
