from __future__ import annotations

import logging
from dataclasses import dataclass

from application.services.async_pipeline import PipelineStageWorker
from application.services.outbox_dispatcher import OutboxDispatcher
from domain.queue_topology import PipelineQueueTopology
from infrastructure.messaging.rabbitmq_topology import RabbitMQTopologyConfig, RabbitMQTopologyManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RabbitMQWorkerConfig:
    url: str
    queue_name: str
    prefetch_count: int = 1
    max_messages: int | None = None
    topology: PipelineQueueTopology = PipelineQueueTopology()


class RabbitMQWorkerConsumer:
    def __init__(
        self,
        *,
        config: RabbitMQWorkerConfig,
        worker: PipelineStageWorker,
        outbox_dispatcher: OutboxDispatcher | None = None,
    ) -> None:
        self.config = config
        self.worker = worker
        self.outbox_dispatcher = outbox_dispatcher
        self._processed = 0

    def consume(self) -> dict[str, int | str]:
        connection = self._connect()
        try:
            channel = connection.channel()
            RabbitMQTopologyManager(
                RabbitMQTopologyConfig(
                    url=self.config.url,
                    topology=self.config.topology,
                )
            ).declare_on_channel(channel)
            channel.basic_qos(prefetch_count=self.config.prefetch_count)
            channel.basic_consume(
                queue=self.config.queue_name,
                on_message_callback=self._handle_delivery,
                auto_ack=False,
            )
            channel.start_consuming()
            return {
                "queue_name": self.config.queue_name,
                "processed": self._processed,
            }
        finally:
            if connection.is_open:
                connection.close()

    def _handle_delivery(self, channel, method, properties, body) -> None:
        payload = body.decode("utf-8") if isinstance(body, bytes) else str(body)
        try:
            result = self.worker.handle_payload(payload)
            if self.outbox_dispatcher is not None:
                self.outbox_dispatcher.dispatch_pending()
            channel.basic_ack(delivery_tag=method.delivery_tag)
            logger.info(
                "Acked pipeline message action=%s job_id=%s stage=%s",
                result.action,
                result.job_id,
                result.stage,
            )
        except Exception:
            logger.exception("Worker failed before it could persist retry/DLQ outcome")
            channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        finally:
            self._processed += 1
            if self.config.max_messages is not None and self._processed >= self.config.max_messages:
                channel.stop_consuming()

    def _connect(self):
        try:
            import pika
        except ImportError as exc:
            raise RuntimeError(
                "RabbitMQ worker consumption requires pika. Install requirements before enabling Phase 6."
            ) from exc

        return pika.BlockingConnection(pika.URLParameters(self.config.url))
