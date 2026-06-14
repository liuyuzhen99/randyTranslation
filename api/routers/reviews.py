from __future__ import annotations

import json
from dataclasses import replace

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from api.dependencies import (
    get_review_workflow_services,
    get_async_pipeline_services,
    get_outbox_dispatcher,
    get_session_factory,
)
from api.service import (
    ReviewDecisionRequest,
    TasteAuditRequest,
    TranscriptSubmissionRequest,
    TranslationSubmissionRequest,
)
from application.services.review_workflow_service import ReviewConflictError
from domain.enums import ReviewType, StageType
from domain.message_contracts import PipelineStageMessage, ReviewContext
from domain.time_utils import utc_now

router = APIRouter(tags=["reviews"])


def _meta():
    return {"generated_at": utc_now().isoformat(), "update_mode": "polling", "refresh_hint_seconds": 15}


def _resume_pipeline_after_review(
    candidate_id: str,
    review_type,
    song_name: str,
    *,
    pipeline_services,
    outbox_dispatcher,
    session_factory,
) -> None:
    if pipeline_services is None or session_factory is None:
        return
    next_stage = None
    if review_type == ReviewType.MANUAL_REVIEW:
        next_stage = StageType.TRANSLATE
    elif review_type == ReviewType.TRANSLATION_REVIEW:
        next_stage = StageType.RENDER
    if next_stage is None:
        return
    from infrastructure.persistence.sqlalchemy_repositories import SQLAlchemyPipelineStageExecutionRepository
    executions = SQLAlchemyPipelineStageExecutionRepository(session_factory).list_for_candidate(candidate_id)
    if not executions:
        return
    latest = executions[-1]
    try:
        resume_payload = json.loads(latest.result_payload or "{}")
    except json.JSONDecodeError:
        resume_payload = {}
    resume_payload.pop("pause", None)
    resume_payload.pop("pause_reason", None)
    command_service, _worker = pipeline_services
    command_service.enqueue_stage(
        PipelineStageMessage.build(
            message_type="pipeline.stage.command",
            job_id=latest.job_id,
            stage=next_stage,
            song_name=song_name,
            trace_id=latest.trace_id,
            max_attempts=latest.max_attempts,
            payload=resume_payload,
            review=ReviewContext(candidate_id=candidate_id),
        )
    )
    if outbox_dispatcher is not None:
        outbox_dispatcher.dispatch_pending()


@router.get("/v1/audit-queue")
async def list_audit_queue(
    status: str | None = None,
    review_services=Depends(get_review_workflow_services),
):
    if review_services is None:
        raise HTTPException(status_code=503, detail="Review workflow services are not enabled")
    items = review_services.audit_service.list_queue(status=status)
    total = len(items)
    return {
        "items": items,
        "pagination": {"page": 1, "page_size": total or 1, "total": total, "total_pages": 1},
        "meta": _meta(),
    }


@router.get("/v1/audit-log")
async def list_audit_log(
    aggregate_type: str,
    aggregate_id: str,
    review_services=Depends(get_review_workflow_services),
):
    if review_services is None:
        raise HTTPException(status_code=503, detail="Review workflow services are not enabled")
    items = review_services.audit_service.list_audit_logs(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
    )
    total = len(items)
    return {
        "items": items,
        "pagination": {"page": 1, "page_size": total or 1, "total": total, "total_pages": 1},
        "meta": _meta(),
    }


@router.post("/v1/reviews/{review_id}/approve")
async def approve_review(
    review_id: str,
    request: ReviewDecisionRequest,
    x_actor_id: str | None = Header(default=None),
    review_services=Depends(get_review_workflow_services),
    pipeline_services=Depends(get_async_pipeline_services),
    outbox_dispatcher=Depends(get_outbox_dispatcher),
    session_factory=Depends(get_session_factory),
):
    if review_services is None:
        raise HTTPException(status_code=503, detail="Review workflow services are not enabled")
    try:
        review_before = review_services.audit_service.support.review_repository.get(review_id)
        result = review_services.audit_service.approve_review(
            review_id=review_id,
            actor_id=(x_actor_id or "manual-review"),
            expected_version=request.expected_version,
            comment=request.comment,
        )
        if review_before is not None:
            candidate = review_services.audit_service.support.get_candidate_or_raise(review_before.subject_id)
            _resume_pipeline_after_review(
                review_before.subject_id,
                review_before.review_type,
                candidate.title,
                pipeline_services=pipeline_services,
                outbox_dispatcher=outbox_dispatcher,
                session_factory=session_factory,
            )
        return result
    except KeyError:
        raise HTTPException(status_code=404, detail="Review not found")
    except ReviewConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/v1/reviews/{review_id}/reject")
async def reject_review(
    review_id: str,
    request: ReviewDecisionRequest,
    x_actor_id: str | None = Header(default=None),
    review_services=Depends(get_review_workflow_services),
):
    if review_services is None:
        raise HTTPException(status_code=503, detail="Review workflow services are not enabled")
    try:
        return review_services.audit_service.reject_review(
            review_id=review_id,
            actor_id=(x_actor_id or "manual-review"),
            expected_version=request.expected_version,
            comment=request.comment,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Review not found")
    except ReviewConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/v1/candidates/{candidate_id}/transcript")
async def submit_transcript(
    candidate_id: str,
    request: TranscriptSubmissionRequest,
    x_actor_id: str | None = Header(default=None),
    review_services=Depends(get_review_workflow_services),
):
    if review_services is None:
        raise HTTPException(status_code=503, detail="Review workflow services are not enabled")
    try:
        return review_services.automation_service.submit_transcript(
            candidate_id=candidate_id,
            actor_id=(x_actor_id or "ai-transcriber"),
            segments=[segment.model_dump() for segment in request.segments],
            auto_approve_review=request.auto_approve_review,
            comment=request.comment,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ReviewConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/v1/candidates/{candidate_id}/taste-audit")
async def submit_taste_audit(
    candidate_id: str,
    request: TasteAuditRequest,
    x_actor_id: str | None = Header(default=None),
    review_services=Depends(get_review_workflow_services),
):
    if review_services is None:
        raise HTTPException(status_code=503, detail="Review workflow services are not enabled")
    decision = request.decision.strip().lower()
    if decision not in {"approved", "rejected"}:
        raise HTTPException(status_code=400, detail="Taste audit decision must be 'approved' or 'rejected'.")
    try:
        return review_services.automation_service.record_taste_audit(
            candidate_id=candidate_id,
            actor_id=(x_actor_id or "ai-auditor"),
            approve=(decision == "approved"),
            comment=request.comment,
            score=request.score,
            key_lyrics=request.key_lyrics,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ReviewConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/v1/candidates/{candidate_id}/translation")
async def submit_translation(
    candidate_id: str,
    request: TranslationSubmissionRequest,
    x_actor_id: str | None = Header(default=None),
    review_services=Depends(get_review_workflow_services),
):
    if review_services is None:
        raise HTTPException(status_code=503, detail="Review workflow services are not enabled")
    try:
        return review_services.automation_service.submit_translation(
            candidate_id=candidate_id,
            actor_id=(x_actor_id or "ai-translator"),
            translations=[line.model_dump() for line in request.translations],
            auto_approve_review=request.auto_approve_review,
            comment=request.comment,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ReviewConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
