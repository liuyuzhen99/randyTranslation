from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from domain.queue_topology import PipelineQueueTopology


@dataclass(frozen=True)
class RabbitMQTopologyConfig:
    url: str
    topology: PipelineQueueTopology = PipelineQueueTopology()


class RabbitMQTopologyManager:
    def __init__(self, config: RabbitMQTopologyConfig) -> None:
        self.config = config

    def declare(self) -> dict[str, int | str]:
        connection = self._connect()
        try:
            channel = connection.channel()
            self.declare_on_channel(channel)
            return {
                "exchange": self.config.topology.exchange,
                "queues": len(self.config.topology.bindings()),
            }
        finally:
            connection.close()

    def declare_on_channel(self, channel: Any) -> None:
        topology = self.config.topology
        channel.exchange_declare(
            exchange=topology.exchange,
            exchange_type="direct",
            durable=True,
        )
        for binding in topology.bindings():
            arguments = None
            if binding.queue_name != topology.dead_letter_queue:
                arguments = {
                    "x-dead-letter-exchange": topology.exchange,
                    "x-dead-letter-routing-key": topology.dlq_routing_key(),
                }
            channel.queue_declare(
                queue=binding.queue_name,
                durable=True,
                arguments=arguments,
            )
            channel.queue_bind(
                exchange=topology.exchange,
                queue=binding.queue_name,
                routing_key=binding.routing_key,
            )

    def _connect(self):
        try:
            import pika
        except ImportError as exc:
            raise RuntimeError(
                "RabbitMQ topology setup requires pika. Install requirements before enabling RabbitMQ pipeline."
            ) from exc

        return pika.BlockingConnection(pika.URLParameters(self.config.url))
