from __future__ import annotations

from dataclasses import replace

from domain.entities import Job
from domain.enums import JobStatus, StageType
from domain.exceptions import InvalidJobTransitionError
from domain.time_utils import utc_now

TERMINAL_JOB_STATUSES = {JobStatus.COMPLETED}

ALLOWED_JOB_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.PENDING: {JobStatus.PROCESSING, JobStatus.FAILED},
    JobStatus.PROCESSING: {JobStatus.COMPLETED, JobStatus.FAILED},
    JobStatus.COMPLETED: set(),
    JobStatus.FAILED: {JobStatus.PROCESSING},
}


def can_transition_job_status(
    current_status: JobStatus,
    next_status: JobStatus,
    retry_count: int = 0,
    max_retry_attempts: int = 3,
) -> bool:
    if current_status == next_status:
        return True

    if next_status not in ALLOWED_JOB_TRANSITIONS[current_status]:
        return False

    if current_status == JobStatus.FAILED and next_status == JobStatus.PROCESSING:
        return retry_count < max_retry_attempts

    return True


def validate_job_transition(
    current_status: JobStatus,
    next_status: JobStatus,
    retry_count: int = 0,
    max_retry_attempts: int = 3,
) -> None:
    if not can_transition_job_status(
        current_status=current_status,
        next_status=next_status,
        retry_count=retry_count,
        max_retry_attempts=max_retry_attempts,
    ):
        raise InvalidJobTransitionError(
            f"Invalid job status transition: {current_status.value} -> {next_status.value}"
        )


def transition_job(
    job: Job,
    next_status: JobStatus,
    *,
    progress: str,
    stage: StageType | None = None,
    result: str | None = None,
    max_retry_attempts: int = 3,
) -> Job:
    validate_job_transition(
        current_status=job.status,
        next_status=next_status,
        retry_count=job.retry_count,
        max_retry_attempts=max_retry_attempts,
    )

    next_retry_count = job.retry_count
    if job.status == JobStatus.FAILED and next_status == JobStatus.PROCESSING:
        next_retry_count += 1

    return replace(
        job,
        status=next_status,
        progress=progress,
        current_stage=stage,
        result=result if result is not None else job.result,
        retry_count=next_retry_count,
        updated_at=utc_now(),
    )
