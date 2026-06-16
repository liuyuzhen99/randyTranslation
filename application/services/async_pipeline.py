from __future__ import annotations

import logging
import json
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from threading import Event, Thread
import time
from typing import TYPE_CHECKING, Callable, Protocol

from application.services.request_tracing import request_span
from domain.entities import Job, OutboxEvent, PipelineStageExecution
from domain.enums import JobStatus, OutboxStatus, StageStatus, StageType
from domain.message_contracts import PipelineStageMessage, ReviewContext
from domain.queue_topology import PipelineQueueTopology, next_stage
from domain.repositories import JobRepository, OutboxRepository, PipelineStageExecutionRepository
from domain.time_utils import utc_now

if TYPE_CHECKING:
    from infrastructure.persistence.sqlalchemy_repositories import SQLAlchemySessionFactory, SQLAlchemyOutboxRepository, SQLAlchemyPipelineStageExecutionRepository

logger = logging.getLogger(__name__)


class StageHandler(Protocol):
    def __call__(self, message: PipelineStageMessage) -> dict:
        ...


@dataclass(frozen=True)
class WorkerResult:
    action: str
    job_id: str
    stage: str
    dedupe_key: str
    attempt: int
    next_retry_seconds: int = 0
    error: str | None = None


class _PipelineStageLogHandler(logging.Handler):
    def __init__(self, stage: str, captured_logs: list[dict]) -> None:
        super().__init__(level=logging.INFO)
        self.stage = stage
        self.captured_logs = captured_logs

    def emit(self, record: logging.LogRecord) -> None:
        level = record.levelname.lower()
        if level == "warning":
            level = "warning"
        elif level in {"error", "critical"}:
            level = "error"
        elif level == "info":
            level = "info"
        else:
            level = "info"
        task_id = getattr(record, "task_id", "")
        prefix = f"[{task_id}] " if task_id else ""
        self.captured_logs.append(
            {
                "timestamp": utc_now().isoformat(),
                "level": level,
                "stage": self.stage,
                "message": f"{prefix}{record.getMessage()}",
            }
        )


class AsyncPipelineCommandService:
    def __init__(
        self,
        *,
        outbox_repository: OutboxRepository,
        topology: PipelineQueueTopology | None = None,
        max_attempts: int = 3,
    ) -> None:
        self.outbox_repository = outbox_repository
        self.topology = topology or PipelineQueueTopology()
        self.max_attempts = max_attempts

    def enqueue_first_stage(
        self,
        job: Job,
        *,
        candidate_id: str | None = None,
        trace_id: str | None = None,
    ) -> PipelineStageMessage:
        message = PipelineStageMessage.build(
            message_type="pipeline.stage.command",
            job_id=job.job_id,
            stage=StageType.DOWNLOAD,
            song_name=job.song_name,
            trace_id=trace_id,
            max_attempts=self.max_attempts,
            review=ReviewContext(candidate_id=candidate_id),
            payload={"candidate_id": candidate_id},
        )
        self._enqueue_message(message, topic=self.topology.command_queue)
        return message

    def enqueue_stage(self, message: PipelineStageMessage) -> None:
        self._enqueue_message(
            message,
            topic=self.topology.stage_queue(message.stage),
        )

    def enqueue_dlq(self, message: PipelineStageMessage, *, error: str) -> None:
        dlq_message = PipelineStageMessage.build(
            message_type="pipeline.stage.dlq",
            job_id=message.job_id,
            stage=message.stage,
            song_name=message.song_name,
            trace_id=message.trace_id,
            attempt=message.retry.attempt,
            max_attempts=message.retry.max_attempts,
            review=message.review,
            payload={**message.payload, "error": error},
        )
        dlq_message = replace(dlq_message, dedupe_key=f"{message.dedupe_key}:dlq")
        self._enqueue_message(dlq_message, topic=self.topology.dead_letter_queue)

    def _enqueue_message(self, message: PipelineStageMessage, *, topic: str) -> None:
        self.outbox_repository.add(
            OutboxEvent(
                event_id=f"{message.message_type}:{message.dedupe_key}",
                topic=topic,
                payload=message.to_payload(),
                status=OutboxStatus.PENDING,
                aggregate_id=message.job_id,
                dedupe_key=message.dedupe_key,
                correlation_id=message.trace_id,
            )
        )


class PipelineStageWorker:
    def __init__(
        self,
        *,
        job_repository: JobRepository,
        execution_repository: PipelineStageExecutionRepository,
        command_service: AsyncPipelineCommandService,
        handlers: dict[StageType, StageHandler] | None = None,
        backoff_base_seconds: int = 30,
        session_factory: SQLAlchemySessionFactory | None = None,
    ) -> None:
        self.job_repository = job_repository
        self.execution_repository = execution_repository
        self.command_service = command_service
        self.handlers = handlers or {}
        self.backoff_base_seconds = backoff_base_seconds
        self._session_factory = session_factory

    def handle_payload(self, payload: str) -> WorkerResult:
        message = PipelineStageMessage.from_payload(payload)
        with request_span(
            "pipeline.stage.handle",
            {
                "pipeline.job_id": message.job_id,
                "pipeline.stage": message.stage.value,
                "pipeline.trace_id": message.trace_id,
                "pipeline.attempt": message.retry.attempt,
            },
        ):
            return self.handle(message)

    def handle(self, message: PipelineStageMessage) -> WorkerResult:
        existing = self.execution_repository.get_by_dedupe_key(message.dedupe_key)
        if existing and existing.status == StageStatus.COMPLETED:
            return WorkerResult(
                action="ack_duplicate",
                job_id=message.job_id,
                stage=message.stage.value,
                dedupe_key=message.dedupe_key,
                attempt=existing.attempt,
            )
        if existing and existing.status in {StageStatus.RETRY_SCHEDULED, StageStatus.DLQ}:
            return WorkerResult(
                action=f"ack_{existing.status.value}",
                job_id=message.job_id,
                stage=message.stage.value,
                dedupe_key=message.dedupe_key,
                attempt=existing.attempt,
                error=existing.error_message,
            )

        now = utc_now()
        execution = existing or PipelineStageExecution(
            execution_id=f"stage:{message.dedupe_key}",
            dedupe_key=message.dedupe_key,
            job_id=message.job_id,
            stage=message.stage,
            candidate_id=message.review.candidate_id,
            attempt=message.retry.attempt,
            max_attempts=message.retry.max_attempts,
            trace_id=message.trace_id,
            created_at=now,
        )
        execution = replace(
            execution,
            status=StageStatus.PROCESSING,
            locked_at=now,
            updated_at=now,
            error_message=None,
        )
        self.execution_repository.upsert(execution)
        self._mark_job_processing(message)

        captured_logs: list[dict] = []
        try:
            with self._capture_stage_logs(message, captured_logs):
                result_payload = self._run_handler(message)
        except Exception as exc:
            logger.exception("Pipeline stage failed: %s %s", message.job_id, message.stage.value)
            return self._handle_failure(message, execution, exc, captured_logs)

        result_payload = self._with_captured_logs(result_payload or {}, captured_logs)
        completed_execution = replace(
            execution,
            status=StageStatus.COMPLETED,
            completed_at=utc_now(),
            result_payload=json.dumps({**message.payload, **(result_payload or {})}, ensure_ascii=False, sort_keys=True),
            updated_at=utc_now(),
        )
        self._atomic_upsert_and_enqueue(
            completed_execution,
            lambda: self._enqueue_next_or_complete(message, result_payload or {}),
        )
        return WorkerResult(
            action="ack",
            job_id=message.job_id,
            stage=message.stage.value,
            dedupe_key=message.dedupe_key,
            attempt=message.retry.attempt,
        )

    def _atomic_upsert_and_enqueue(
        self,
        execution: PipelineStageExecution,
        enqueue_fn: Callable[[], None],
    ) -> None:
        """Upsert execution and add outbox message in one transaction when session_factory is available.

        Falls back to separate calls when session_factory is not injected (e.g. in-memory tests).
        The shared-session path requires both repositories to be SQLAlchemy-backed.
        """
        from infrastructure.persistence.sqlalchemy_repositories import (
            SQLAlchemyOutboxRepository,
            SQLAlchemyPipelineStageExecutionRepository,
        )

        if (
            self._session_factory is not None
            and isinstance(self.execution_repository, SQLAlchemyPipelineStageExecutionRepository)
            and isinstance(self.command_service.outbox_repository, SQLAlchemyOutboxRepository)
        ):
            with self._session_factory.transactional() as session:
                self.execution_repository.upsert_with_session(session, execution)
                # Collect the outbox event by temporarily redirecting add_with_session
                # We monkey-patch the outbox add to use the shared session for this call only.
                orig_add = self.command_service.outbox_repository.add
                try:
                    self.command_service.outbox_repository.add = (
                        lambda event: self.command_service.outbox_repository.add_with_session(session, event)
                    )
                    enqueue_fn()
                finally:
                    self.command_service.outbox_repository.add = orig_add
        else:
            self.execution_repository.upsert(execution)
            enqueue_fn()

    def _run_handler(self, message: PipelineStageMessage) -> dict:
        handler = self.handlers.get(message.stage)
        if handler is None:
            return {"status": "noop"}
        return handler(message)

    def _handle_failure(
        self,
        message: PipelineStageMessage,
        execution: PipelineStageExecution,
        exc: Exception,
        captured_logs: list[dict] | None = None,
    ) -> WorkerResult:
        next_attempt = message.retry.attempt + 1
        error = str(exc)
        if next_attempt >= message.retry.max_attempts:
            failed_message = replace(
                message,
                payload=self._with_captured_logs({**message.payload, "error": error}, captured_logs or []),
            )
            failed_execution = replace(
                execution,
                status=StageStatus.DLQ,
                error_message=error,
                result_payload=failed_message.to_payload(),
                updated_at=utc_now(),
            )
            self._atomic_upsert_and_enqueue(
                failed_execution,
                lambda: self.command_service.enqueue_dlq(message, error=error),
            )
            self._mark_job_failed(message, error)
            return WorkerResult(
                action="dlq",
                job_id=message.job_id,
                stage=message.stage.value,
                dedupe_key=message.dedupe_key,
                attempt=message.retry.attempt,
                error=error,
            )

        backoff_seconds = self.backoff_base_seconds * (2 ** message.retry.attempt)
        retry_message = PipelineStageMessage.build(
            message_type=message.message_type,
            job_id=message.job_id,
            stage=message.stage,
            song_name=message.song_name,
            trace_id=message.trace_id,
            attempt=next_attempt,
            max_attempts=message.retry.max_attempts,
            backoff_seconds=backoff_seconds,
            review=message.review,
            payload=self._with_captured_logs(message.payload, captured_logs or []),
        )
        retry_execution = replace(
            execution,
            status=StageStatus.RETRY_SCHEDULED,
            error_message=error,
            next_retry_at=utc_now() + timedelta(seconds=backoff_seconds),
            result_payload=retry_message.to_payload(),
            updated_at=utc_now(),
        )
        self.execution_repository.upsert(retry_execution)
        return WorkerResult(
            action="nack_retry",
            job_id=message.job_id,
            stage=message.stage.value,
            dedupe_key=message.dedupe_key,
            attempt=message.retry.attempt,
            next_retry_seconds=backoff_seconds,
            error=error,
        )

    @contextmanager
    def _capture_stage_logs(self, message: PipelineStageMessage, captured_logs: list[dict]):
        handler = _PipelineStageLogHandler(message.stage.value, captured_logs)
        hiphop_logger = logging.getLogger("hiphop_app")
        hiphop_logger.addHandler(handler)
        captured_logs.append(
            {
                "timestamp": utc_now().isoformat(),
                "level": "info",
                "stage": message.stage.value,
                "message": f"{message.stage.value}: started",
            }
        )
        try:
            yield
            captured_logs.append(
                {
                    "timestamp": utc_now().isoformat(),
                    "level": "success",
                    "stage": message.stage.value,
                    "message": f"{message.stage.value}: completed",
                }
            )
        except Exception as exc:
            captured_logs.append(
                {
                    "timestamp": utc_now().isoformat(),
                    "level": "error",
                    "stage": message.stage.value,
                    "message": f"{message.stage.value}: failed: {exc}",
                }
            )
            raise
        finally:
            hiphop_logger.removeHandler(handler)

    @staticmethod
    def _with_captured_logs(payload: dict, captured_logs: list[dict]) -> dict:
        if not captured_logs:
            return payload
        existing_logs = payload.get("_logs") if isinstance(payload.get("_logs"), list) else []
        return {**payload, "_logs": [*existing_logs, *captured_logs][-100:]}

    def _enqueue_next_or_complete(self, message: PipelineStageMessage, result_payload: dict) -> None:
        if result_payload.get("pause"):
            return
        stage = result_payload.get("next_stage") or next_stage(message.stage)
        if isinstance(stage, str):
            stage = StageType(stage)
        if stage is None:
            job = self.job_repository.get(message.job_id)
            if job is not None:
                self.job_repository.update(
                    replace(
                        job,
                        status=JobStatus.COMPLETED,
                        current_stage=message.stage,
                        progress="异步 pipeline 已完成",
                        updated_at=utc_now(),
                    )
                )
            return
        next_message = PipelineStageMessage.build(
            message_type="pipeline.stage.command",
            job_id=message.job_id,
            stage=stage,
            song_name=message.song_name,
            trace_id=message.trace_id,
            max_attempts=message.retry.max_attempts,
            review=message.review,
            payload={**message.payload, **result_payload},
        )
        self.command_service.enqueue_stage(next_message)

    def _mark_job_processing(self, message: PipelineStageMessage) -> None:
        job = self.job_repository.get(message.job_id)
        if job is None:
            self.job_repository.create(
                Job(
                    job_id=message.job_id,
                    song_name=message.song_name,
                    status=JobStatus.PROCESSING,
                    current_stage=message.stage,
                    progress=f"异步执行阶段: {message.stage.value}",
                )
            )
            return
        self.job_repository.update(
            replace(
                job,
                status=JobStatus.PROCESSING,
                current_stage=message.stage,
                progress=f"异步执行阶段: {message.stage.value}",
                updated_at=utc_now(),
            )
        )

    def _mark_job_failed(self, message: PipelineStageMessage, error: str) -> None:
        job = self.job_repository.get(message.job_id)
        if job is None:
            return
        self.job_repository.update(
            replace(
                job,
                status=JobStatus.FAILED,
                current_stage=message.stage,
                progress=f"异步阶段进入 DLQ: {error}",
                updated_at=utc_now(),
            )
        )


class InProcessPipelineWorker:
    """Service-local pipeline worker that consumes pipeline outbox events directly."""

    def __init__(
        self,
        *,
        outbox_repository: OutboxRepository,
        worker: PipelineStageWorker,
        poll_interval_seconds: float = 1.0,
        batch_limit: int = 20,
    ) -> None:
        self.outbox_repository = outbox_repository
        self.worker = worker
        self.poll_interval_seconds = poll_interval_seconds
        self.batch_limit = batch_limit
        self._stop_event = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = Thread(target=self._run_forever, name="pipeline-inprocess-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def drain_once(self) -> dict[str, int]:
        processed = 0
        failed = 0
        skipped = 0
        for event in self.outbox_repository.list_pending()[: self.batch_limit]:
            if not self._is_pipeline_event(event):
                skipped += 1
                continue
            try:
                self.worker.handle_payload(event.payload)
                self.outbox_repository.update(replace(event, status=OutboxStatus.PUBLISHED))
                processed += 1
            except Exception:
                logger.exception("In-process pipeline worker failed event %s", event.event_id)
                self.outbox_repository.update(replace(event, status=OutboxStatus.FAILED))
                failed += 1
        return {
            "processed": processed,
            "failed": failed,
            "skipped": skipped,
        }

    def _run_forever(self) -> None:
        while not self._stop_event.is_set():
            result = self.drain_once()
            if result["processed"] == 0 and result["failed"] == 0:
                self._stop_event.wait(self.poll_interval_seconds)
            else:
                time.sleep(0)

    @staticmethod
    def _is_pipeline_event(event: OutboxEvent) -> bool:
        return event.topic == "pipeline.command" or event.topic.startswith("pipeline.stage.")
