from __future__ import annotations

from dataclasses import dataclass

from domain.enums import StageType


@dataclass(frozen=True)
class QueueBinding:
    queue_name: str
    routing_key: str
    stage: StageType | None = None


@dataclass(frozen=True)
class PipelineQueueTopology:
    command_queue: str = "pipeline.command"
    dead_letter_queue: str = "pipeline.dlq"
    exchange: str = "pipeline"

    def stage_queue(self, stage: StageType) -> str:
        return f"pipeline.stage.{stage.value}"

    def stage_routing_key(self, stage: StageType) -> str:
        return self.stage_queue(stage)

    def command_routing_key(self) -> str:
        return self.command_queue

    def dlq_routing_key(self) -> str:
        return self.dead_letter_queue

    def bindings(self) -> list[QueueBinding]:
        stage_bindings = [
            QueueBinding(
                queue_name=self.stage_queue(stage),
                routing_key=self.stage_routing_key(stage),
                stage=stage,
            )
            for stage in STAGE_ORDER
        ]
        return [
            QueueBinding(queue_name=self.command_queue, routing_key=self.command_routing_key()),
            *stage_bindings,
            QueueBinding(queue_name=self.dead_letter_queue, routing_key=self.dlq_routing_key()),
        ]


STAGE_ORDER: tuple[StageType, ...] = (
    StageType.DOWNLOAD,
    StageType.TRANSCRIBE,
    StageType.AUDIT,
    StageType.MANUAL_REVIEW,
    StageType.TRANSLATE,
    StageType.TRANSLATION_REVIEW,
    StageType.RENDER,
)


def next_stage(stage: StageType) -> StageType | None:
    try:
        index = STAGE_ORDER.index(stage)
    except ValueError:
        return None
    next_index = index + 1
    if next_index >= len(STAGE_ORDER):
        return None
    return STAGE_ORDER[next_index]
