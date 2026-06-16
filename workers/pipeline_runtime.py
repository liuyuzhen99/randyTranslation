from __future__ import annotations

from api.config import AppRuntimeSettings
from application.services.async_pipeline import AsyncPipelineCommandService, PipelineStageWorker
from application.services.pipeline_stage_handlers import PipelineStageHandlers
from application.services.review_workflow_service import ReviewWorkflowServices
from domain.repositories import JobRepository
from infrastructure.persistence.sqlalchemy_repositories import (
    SQLAlchemyJobRepository,
    SQLAlchemyPipelineStageExecutionRepository,
    SQLAlchemySessionFactory,
)


def create_pipeline_stage_worker(
    *,
    command_service: AsyncPipelineCommandService,
    runtime_settings: AppRuntimeSettings,
    session_factory: SQLAlchemySessionFactory,
    job_repository: JobRepository | None = None,
    media_storage=None,
    producer_backend_factory=None,
    workflow_services: ReviewWorkflowServices | None = None,
    artifact_repository=None,
    vector_repository=None,
) -> PipelineStageWorker:
    active_job_repository = job_repository or SQLAlchemyJobRepository(session_factory)
    handlers = None
    if media_storage is not None and producer_backend_factory is not None:
        handlers = PipelineStageHandlers(
            media_storage=media_storage,
            producer_backend_factory=producer_backend_factory,
            workflow_services=workflow_services,
            artifact_repository=artifact_repository,
            vector_repository=vector_repository,
            final_artifact_retention_days=runtime_settings.artifact_final_retention_days,
        ).as_mapping()
    return PipelineStageWorker(
        job_repository=active_job_repository,
        execution_repository=SQLAlchemyPipelineStageExecutionRepository(session_factory),
        command_service=command_service,
        handlers=handlers,
        backoff_base_seconds=runtime_settings.pipeline_retry_backoff_base_seconds,
        session_factory=session_factory,
    )
