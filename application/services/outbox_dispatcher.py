from __future__ import annotations

import logging
from dataclasses import replace
from typing import Protocol

from domain.entities import OutboxEvent
from domain.enums import OutboxStatus
from domain.repositories import OutboxRepository

logger = logging.getLogger(__name__)


class OutboxPublisher(Protocol):
    def publish(self, topic: str, payload: str, correlation_id: str | None = None) -> None:
        ...


class LoggingOutboxPublisher:
    """Prototype publisher that makes outbox draining observable before RabbitMQ lands."""

    def publish(self, topic: str, payload: str, correlation_id: str | None = None) -> None:
        logger.info(
            "Dispatching outbox event topic=%s correlation_id=%s payload=%s",
            topic,
            correlation_id,
            payload,
        )


class OutboxDispatcher:
    """Minimal dispatcher prototype that drains pending outbox events."""

    def __init__(self, outbox_repository: OutboxRepository, publisher: OutboxPublisher) -> None:
        self.outbox_repository = outbox_repository
        self.publisher = publisher

    def dispatch_pending(self) -> dict[str, int]:
        pending_events = self.outbox_repository.list_pending()
        published = 0
        failed = 0
        published_event_ids: list[str] = []
        failed_event_ids: list[str] = []

        for event in pending_events:
            try:
                self.publisher.publish(
                    topic=event.topic,
                    payload=event.payload,
                    correlation_id=event.correlation_id,
                )
                self.outbox_repository.update(
                    replace(event, status=OutboxStatus.PUBLISHED)
                )
                published += 1
                published_event_ids.append(event.event_id)
            except Exception:
                logger.exception("Failed to dispatch outbox event %s", event.event_id)
                self.outbox_repository.update(
                    replace(event, status=OutboxStatus.FAILED)
                )
                failed += 1
                failed_event_ids.append(event.event_id)

        return {
            "attempted": len(pending_events),
            "published": published,
            "failed": failed,
            "pending_after": len(self.outbox_repository.list_pending()),
            "published_event_ids": published_event_ids,
            "failed_event_ids": failed_event_ids,
        }
