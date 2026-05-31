from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from api.config import create_media_storage, create_sqlalchemy_session_factory, load_runtime_settings
from application.services.async_pipeline import AsyncPipelineCommandService, PipelineStageWorker
from application.services.outbox_dispatcher import OutboxDispatcher
from domain.entities import Job
from domain.enums import OutboxStatus, StageStatus, StageType
from domain.message_contracts import PipelineStageMessage, ReviewContext
from domain.queue_topology import PipelineQueueTopology
from infrastructure.messaging.rabbitmq_consumer import RabbitMQWorkerConfig, RabbitMQWorkerConsumer
from infrastructure.messaging.rabbitmq_observability import (
    RabbitMQQueueMetricsCollector,
    RabbitMQQueueMetricsConfig,
)
from infrastructure.messaging.rabbitmq_publisher import RabbitMQPublishConfig, RabbitMQPublisher
from infrastructure.messaging.rabbitmq_topology import RabbitMQTopologyConfig, RabbitMQTopologyManager
from infrastructure.persistence.sqlalchemy_repositories import (
    SQLAlchemyJobRepository,
    SQLAlchemyOutboxRepository,
    SQLAlchemyPipelineStageExecutionRepository,
)


def main() -> int:
    settings = load_runtime_settings()
    rabbitmq_url = os.environ.get("RABBITMQ_URL", "").strip() or "amqp://guest:guest@localhost:5672/"
    topology = PipelineQueueTopology()

    session_factory = create_sqlalchemy_session_factory(runtime_settings=settings)
    if session_factory is None:
        raise RuntimeError("DATABASE_URL is required for Phase 7 backend drill.")

    RabbitMQTopologyManager(RabbitMQTopologyConfig(url=rabbitmq_url, topology=topology)).declare()
    collector = RabbitMQQueueMetricsCollector(RabbitMQQueueMetricsConfig(url=rabbitmq_url, topology=topology))
    initial_depths = collector.collect_depths()
    non_empty = {queue: depth for queue, depth in initial_depths.items() if depth}
    if non_empty:
        raise RuntimeError(f"Phase 7 backend drill requires empty queues. Non-empty queues: {non_empty}")

    job_repository = SQLAlchemyJobRepository(session_factory)
    outbox_repository = SQLAlchemyOutboxRepository(session_factory)
    execution_repository = SQLAlchemyPipelineStageExecutionRepository(session_factory)
    command_service = AsyncPipelineCommandService(outbox_repository=outbox_repository)
    publisher = RabbitMQPublisher(RabbitMQPublishConfig(url=rabbitmq_url))
    dispatcher = OutboxDispatcher(outbox_repository, publisher)

    try:
        cos_result = _run_cos_drill(settings)
        backlog_result = _run_backlog_drill(
            job_repository=job_repository,
            outbox_repository=outbox_repository,
            execution_repository=execution_repository,
            command_service=command_service,
            publisher=publisher,
            dispatcher=dispatcher,
            collector=collector,
            topology=topology,
            rabbitmq_url=rabbitmq_url,
        )
        dlq_result = _run_dlq_replay_drill(
            job_repository=job_repository,
            outbox_repository=outbox_repository,
            execution_repository=execution_repository,
            command_service=command_service,
            dispatcher=dispatcher,
            collector=collector,
        )
        final_depths_before_cleanup = collector.collect_depths()
    finally:
        _purge_phase7_queues(rabbitmq_url, topology)
    result = {
        "initial_depths": initial_depths,
        "cos": cos_result,
        "backlog": backlog_result,
        "dlq_replay": dlq_result,
        "final_depths_before_cleanup": final_depths_before_cleanup,
        "final_depths_after_cleanup": collector.collect_depths(),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _run_cos_drill(settings) -> dict:
    storage = create_media_storage(runtime_settings=settings)
    drill_id = f"phase7-cos-{uuid4().hex[:12]}"
    workspace = storage.prepare_task_workspace(drill_id)
    source_path = Path(storage.resolve_temp_file(drill_id, "phase7-drill.txt"))
    source_path.write_text("phase7 cos drill", encoding="utf-8")

    artifact = storage.upload_artifact(
        task_id=drill_id,
        local_path=str(source_path),
        artifact_type="phase7_drill",
        content_type="text/plain",
    )
    downloaded_path = Path(storage.resolve_temp_file(drill_id, "phase7-drill-downloaded.txt"))
    storage.download_artifact(artifact.object_uri, str(downloaded_path))
    downloaded = downloaded_path.read_text(encoding="utf-8")
    storage.delete_artifact(artifact.object_uri)
    storage.cleanup_task_workspace(drill_id)
    return {
        "provider": artifact.storage_provider,
        "bucket": artifact.bucket,
        "object_key": artifact.object_key,
        "uploaded": True,
        "downloaded_matches": downloaded == "phase7 cos drill",
        "deleted": True,
    }


def _purge_phase7_queues(rabbitmq_url: str, topology: PipelineQueueTopology) -> None:
    import pika

    connection = pika.BlockingConnection(pika.URLParameters(rabbitmq_url))
    try:
        channel = connection.channel()
        for binding in topology.bindings():
            channel.queue_purge(queue=binding.queue_name)
    finally:
        connection.close()


def _run_backlog_drill(
    *,
    job_repository,
    outbox_repository,
    execution_repository,
    command_service,
    publisher,
    dispatcher,
    collector,
    topology,
    rabbitmq_url: str,
) -> dict:
    drill_id = uuid4().hex[:12]
    jobs = [
        Job(job_id=f"phase7-backlog-{drill_id}-{index}", song_name=f"Phase 7 Backlog {index}")
        for index in range(3)
    ]
    for job in jobs:
        job_repository.create(job)
        message = command_service.enqueue_first_stage(job, trace_id=f"phase7-backlog-{drill_id}")
        event = outbox_repository.get(f"pipeline.stage.command:{message.dedupe_key}")
        publisher.publish(topology.command_queue, event.payload, event.correlation_id)
        outbox_repository.update(replace(event, status=OutboxStatus.PUBLISHED))

    after_publish = collector.collect_depths()
    if after_publish[topology.command_queue] < 3:
        raise RuntimeError(f"Expected command backlog >= 3, got {after_publish[topology.command_queue]}")

    handled = []
    worker = PipelineStageWorker(
        job_repository=job_repository,
        execution_repository=execution_repository,
        command_service=command_service,
        handlers={
            StageType.DOWNLOAD: lambda message: handled.append(message.job_id) or {"drill": "backlog"}
        },
    )
    consumer = RabbitMQWorkerConsumer(
        config=RabbitMQWorkerConfig(
            url=rabbitmq_url,
            queue_name=topology.command_queue,
            max_messages=3,
            topology=topology,
        ),
        worker=worker,
        outbox_dispatcher=dispatcher,
    )
    consume_result = consumer.consume()
    after_consume = collector.collect_depths()
    return {
        "published": len(jobs),
        "command_depth_after_publish": after_publish[topology.command_queue],
        "consume_result": consume_result,
        "handled": handled,
        "command_depth_after_consume": after_consume[topology.command_queue],
        "next_stage_depth": after_consume[topology.stage_queue(StageType.TRANSCRIBE)],
    }


def _run_dlq_replay_drill(
    *,
    job_repository,
    outbox_repository,
    execution_repository,
    command_service,
    dispatcher,
    collector,
) -> dict:
    drill_id = uuid4().hex[:12]
    job = Job(job_id=f"phase7-dlq-{drill_id}", song_name="Phase 7 DLQ Replay")
    job_repository.create(job)
    first_message = PipelineStageMessage.build(
        message_type="pipeline.stage.command",
        job_id=job.job_id,
        stage=StageType.AUDIT,
        song_name=job.song_name,
        trace_id=f"phase7-dlq-{drill_id}",
        max_attempts=1,
        review=ReviewContext(),
    )

    failing_worker = PipelineStageWorker(
        job_repository=job_repository,
        execution_repository=execution_repository,
        command_service=command_service,
        handlers={StageType.AUDIT: lambda _message: (_ for _ in ()).throw(RuntimeError("phase7 drill failure"))},
    )
    dlq_result = failing_worker.handle(first_message)
    dispatcher.dispatch_pending()
    after_dlq = collector.collect_depths()

    replay_message = PipelineStageMessage.build(
        message_type="pipeline.stage.command",
        job_id=job.job_id,
        stage=first_message.stage,
        song_name=first_message.song_name,
        trace_id=first_message.trace_id,
        attempt=first_message.retry.attempt + 1,
        max_attempts=2,
        review=first_message.review,
        payload={"replay_of": first_message.dedupe_key},
    )
    replay_worker = PipelineStageWorker(
        job_repository=job_repository,
        execution_repository=execution_repository,
        command_service=command_service,
        handlers={StageType.AUDIT: lambda _message: {"replayed": True, "next_stage": StageType.MANUAL_REVIEW.value}},
    )
    replay_result = replay_worker.handle(replay_message)
    replay_execution = execution_repository.get_by_dedupe_key(replay_message.dedupe_key)

    return {
        "dlq_action": dlq_result.action,
        "dlq_depth_after_failure": after_dlq[PipelineQueueTopology().dead_letter_queue],
        "replay_action": replay_result.action,
        "replay_status": replay_execution.status.value if replay_execution else None,
    }


if __name__ == "__main__":
    raise SystemExit(main())
