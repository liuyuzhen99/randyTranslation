from __future__ import annotations

from fastapi import Request


def get_job_service(request: Request):
    return request.app.state.job_service


def get_job_repository(request: Request):
    return request.app.state.job_repository


def get_orchestrator(request: Request):
    return request.app.state.orchestrator


def get_media_storage(request: Request):
    return request.app.state.media_storage


def get_artifact_repository(request: Request):
    return request.app.state.artifact_repository


def get_vector_repository(request: Request):
    return request.app.state.vector_repository


def get_artist_catalog_service(request: Request):
    return request.app.state.artist_catalog_service


def get_review_workflow_services(request: Request):
    return request.app.state.review_workflow_services


def get_async_pipeline_services(request: Request):
    return request.app.state.async_pipeline_services


get_phase3_catalog_service = get_artist_catalog_service
get_phase4_workflow_services = get_review_workflow_services
get_phase6_async_pipeline_services = get_async_pipeline_services


def get_reconcile_service(request: Request):
    return request.app.state.reconcile_service


def get_outbox_dispatcher(request: Request):
    return request.app.state.outbox_dispatcher


def get_artifact_lifecycle_service(request: Request):
    return request.app.state.artifact_lifecycle_service


def get_session_factory(request: Request):
    return request.app.state.session_factory


def get_runtime_settings(request: Request):
    return request.app.state.runtime_settings


def get_shadow_write_degraded(request: Request) -> bool:
    job_service = request.app.state.job_service
    return getattr(job_service, "shadow_write_degraded", False)
