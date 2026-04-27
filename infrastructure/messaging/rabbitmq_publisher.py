from __future__ import annotations

from dataclasses import dataclass

from domain.queue_topology import PipelineQueueTopology
from infrastructure.messaging.rabbitmq_topology import RabbitMQTopologyConfig, RabbitMQTopologyManager


@dataclass(frozen=True)
class RabbitMQPublishConfig:
    url: str
    exchange: str = "pipeline"
    declare_topology: bool = True


class RabbitMQPublisher:
    """Thin RabbitMQ publisher kept behind the outbox dispatcher boundary."""

    def __init__(self, config: RabbitMQPublishConfig) -> None:
        self.config = config

    def publish(self, topic: str, payload: str, correlation_id: str | None = None) -> None:
        try:
            import pika
        except ImportError as exc:
            raise RuntimeError(
                "RabbitMQ publishing requires pika. Install requirements before enabling Phase 6."
            ) from exc

        parameters = pika.URLParameters(self.config.url)
        connection = pika.BlockingConnection(parameters)
        try:
            channel = connection.channel()
            if self.config.declare_topology:
                RabbitMQTopologyManager(
                    RabbitMQTopologyConfig(
                        url=self.config.url,
                        topology=PipelineQueueTopology(exchange=self.config.exchange),
                    )
                ).declare_on_channel(channel)
            else:
                channel.exchange_declare(
                    exchange=self.config.exchange,
                    exchange_type="direct",
                    durable=True,
                )
            channel.basic_publish(
                exchange=self.config.exchange,
                routing_key=topic,
                body=payload.encode("utf-8"),
                properties=pika.BasicProperties(
                    content_type="application/json",
                    delivery_mode=2,
                    correlation_id=correlation_id,
                ),
                mandatory=False,
            )
        finally:
            connection.close()
