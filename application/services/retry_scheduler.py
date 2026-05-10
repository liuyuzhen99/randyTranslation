from __future__ import annotations

from dataclasses import replace

from application.services.async_pipeline import AsyncPipelineCommandService
from domain.enums import StageStatus
from domain.message_contracts import PipelineStageMessage
from domain.repositories import PipelineStageExecutionRepository
from domain.time_utils import utc_now


class Phase6RetryScheduler:
    """DB-backed delayed delivery scheduler for Phase 6 retry attempts."""

    def __init__(
        self,
        *,
        execution_repository: PipelineStageExecutionRepository,
        command_service: AsyncPipelineCommandService,
    ) -> None:
        self.execution_repository = execution_repository
        self.command_service = command_service

    def schedule_due(self, *, limit: int = 100) -> dict[str, int | list[str]]:
        due_executions = self.execution_repository.list_due_retries(utc_now(), limit=limit)
        scheduled_dedupe_keys: list[str] = []
        skipped = 0

        for execution in due_executions:
            if not execution.result_payload:
                skipped += 1
                continue

            retry_message = PipelineStageMessage.from_payload(execution.result_payload)
            self.command_service.enqueue_stage(retry_message)
            self.execution_repository.upsert(
                replace(
                    execution,
                    status=StageStatus.RETRY_SCHEDULED,
                    next_retry_at=None,
                    updated_at=utc_now(),
                )
            )
            scheduled_dedupe_keys.append(retry_message.dedupe_key)

        return {
            "due": len(due_executions),
            "scheduled": len(scheduled_dedupe_keys),
            "skipped": skipped,
            "dedupe_keys": scheduled_dedupe_keys,
        }
