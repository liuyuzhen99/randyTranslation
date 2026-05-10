from __future__ import annotations

from dataclasses import dataclass

from domain.queue_topology import PipelineQueueTopology


@dataclass(frozen=True)
class RabbitMQQueueMetricsConfig:
    url: str
    topology: PipelineQueueTopology = PipelineQueueTopology()


class RabbitMQQueueMetricsCollector:
    def __init__(self, config: RabbitMQQueueMetricsConfig) -> None:
        self.config = config

    def collect_depths(self) -> dict[str, int]:
        connection = self._connect()
        try:
            channel = connection.channel()
            depths: dict[str, int] = {}
            for binding in self.config.topology.bindings():
                method = channel.queue_declare(
                    queue=binding.queue_name,
                    durable=True,
                    passive=True,
                )
                depths[binding.queue_name] = int(method.method.message_count)
            return depths
        finally:
            connection.close()

    def _connect(self):
        try:
            import pika
        except ImportError as exc:
            raise RuntimeError(
                "RabbitMQ queue metrics require pika. Install requirements before enabling Phase 7 observability."
            ) from exc

        return pika.BlockingConnection(pika.URLParameters(self.config.url))
