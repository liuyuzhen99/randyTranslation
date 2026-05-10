from __future__ import annotations

import json
import uuid

from domain.entities import AuditLogEntry, ReviewItem, Subtitle, Video, VideoCandidate
from domain.enums import CandidateStatus, ReviewStatus, ReviewType, StageStatus
from domain.repositories import (
    ArtifactRepository,
    AuditLogRepository,
    ArtistRepository,
    CandidateRepository,
    ReviewRepository,
    SubtitleRepository,
    VideoRepository,
)
from domain.time_utils import utc_now


class ReviewConflictError(RuntimeError):
    pass


class ArtistService:
    def __init__(self, artist_repository: ArtistRepository) -> None:
        self.artist_repository = artist_repository

    def list_artists(self):
        return self.artist_repository.list_all()


class TranslationService:
    def describe_translation_status(
        self,
        reviews: list[ReviewItem],
        subtitles: list[Subtitle] | None = None,
    ) -> dict:
        translation_review = next(
            (review for review in reviews if review.review_type == ReviewType.TRANSLATION_REVIEW),
            None,
        )
        translation_lines = [
            subtitle
            for subtitle in (subtitles or [])
            if subtitle.zh_text is not None and subtitle.zh_text.strip()
        ]
        if translation_review is None:
            if translation_lines:
                latest_subtitle = max(translation_lines, key=lambda subtitle: subtitle.line_index)
                return {
                    "status": "submitted",
                    "updated_at": utc_now().isoformat(),
                    "line_count": len(translation_lines),
                    "last_line_index": latest_subtitle.line_index,
                }
            return {"status": "not_started"}
        if (
            translation_review.status == ReviewStatus.PENDING
            and subtitles
            and len(translation_lines) == len(subtitles)
        ):
            return {
                "status": "submitted",
                "updated_at": translation_review.updated_at.isoformat(),
                "line_count": len(translation_lines),
            }
        return {
            "status": translation_review.status.value,
            "updated_at": translation_review.updated_at.isoformat(),
            "line_count": len(translation_lines),
        }


class WorkflowSupport:
    REVIEW_SEQUENCE: tuple[ReviewType, ...] = (
        ReviewType.TRANSCRIPT_REVIEW,
        ReviewType.TASTE_AUDIT,
        ReviewType.MANUAL_REVIEW,
        ReviewType.TRANSLATION_REVIEW,
        ReviewType.FINAL_ASSET_APPROVAL,
    )

    def __init__(
        self,
        artist_repository: ArtistRepository,
        candidate_repository: CandidateRepository,
        review_repository: ReviewRepository,
        audit_log_repository: AuditLogRepository,
        subtitle_repository: SubtitleRepository | None = None,
        video_repository: VideoRepository | None = None,
        artifact_repository: ArtifactRepository | None = None,
    ) -> None:
        self.artist_repository = artist_repository
        self.candidate_repository = candidate_repository
        self.review_repository = review_repository
        self.audit_log_repository = audit_log_repository
        self.subtitle_repository = subtitle_repository
        self.video_repository = video_repository
        self.artifact_repository = artifact_repository

    def list_candidates_with_artist(self) -> list[tuple]:
        rows: list[tuple] = []
        for artist in self.artist_repository.list_all():
            for candidate in self.candidate_repository.list_for_artist(artist.spotify_id):
                rows.append((artist, candidate))
        rows.sort(
            key=lambda item: (
                item[1].published_at or item[1].last_seen_at,
                item[1].last_seen_at,
            ),
            reverse=True,
        )
        return rows

    def bootstrap_reviews(self) -> None:
        for artist, candidate in self.list_candidates_with_artist():
            if candidate.status not in {CandidateStatus.PENDING_REVIEW, CandidateStatus.DOWNLOADING}:
                continue
            reviews = self.review_repository.list_for_subject("candidate", candidate.candidate_id)
            if reviews:
                continue
            review = ReviewItem(
                review_id=self._new_id("review"),
                subject_kind="candidate",
                subject_id=candidate.candidate_id,
                spotify_id=artist.spotify_id,
                review_type=ReviewType.TRANSCRIPT_REVIEW,
            )
            self.review_repository.create(review)
            self._log(
                aggregate_type="candidate",
                aggregate_id=candidate.candidate_id,
                action="review_checkpoint_created",
                actor_id="system",
                details=f"{review.review_type.value}:{review.review_id}",
            )

    def get_reviews_for_candidate(self, candidate_id: str) -> list[ReviewItem]:
        reviews = self.review_repository.list_for_subject("candidate", candidate_id)
        return sorted(reviews, key=lambda review: review.created_at)

    def get_latest_review_for_candidate(
        self,
        candidate_id: str,
        review_type: ReviewType,
    ) -> ReviewItem | None:
        reviews = [
            review
            for review in self.get_reviews_for_candidate(candidate_id)
            if review.review_type == review_type
        ]
        return reviews[-1] if reviews else None

    def get_candidate_or_raise(self, candidate_id: str) -> VideoCandidate:
        candidate = self.candidate_repository.get(candidate_id)
        if candidate is None:
            raise KeyError("Candidate not found")
        return candidate

    def add_candidate_to_pipeline(self, candidate_id: str, actor_id: str) -> dict:
        candidate = self.get_candidate_or_raise(candidate_id)
        if candidate.status == CandidateStatus.ACCEPTED:
            raise ReviewConflictError("Accepted candidates cannot be re-added to the review pipeline.")

        reviews = self.get_reviews_for_candidate(candidate_id)
        pending_review = next((review for review in reviews if review.status == ReviewStatus.PENDING), None)
        created_review = False
        if pending_review is None:
            pending_review = ReviewItem(
                review_id=self._new_id("review"),
                subject_kind="candidate",
                subject_id=candidate.candidate_id,
                spotify_id=candidate.spotify_id,
                review_type=ReviewType.TRANSCRIPT_REVIEW,
            )
            self.review_repository.create(pending_review)
            created_review = True

        if candidate.status != CandidateStatus.PENDING_REVIEW:
            candidate.status = CandidateStatus.PENDING_REVIEW
            self.candidate_repository.upsert(candidate)

        self._log(
            aggregate_type="candidate",
            aggregate_id=candidate.candidate_id,
            action="pipeline_manually_added",
            actor_id=actor_id,
            details=(
                f"{pending_review.review_type.value}:{pending_review.review_id}:created={str(created_review).lower()}"
            ),
        )

        return {
            "candidate_id": candidate.candidate_id,
            "candidate_status": candidate.status.value,
            "review_id": pending_review.review_id,
            "review_type": pending_review.review_type.value,
            "review_status": pending_review.status.value,
            "version": pending_review.version,
        }

    def get_video_repository_or_raise(self) -> VideoRepository:
        if self.video_repository is None:
            raise RuntimeError("Video repository is not configured")
        return self.video_repository

    def get_subtitle_repository_or_raise(self) -> SubtitleRepository:
        if self.subtitle_repository is None:
            raise RuntimeError("Subtitle repository is not configured")
        return self.subtitle_repository

    def list_artifacts_for_candidate(self, candidate_id: str) -> list[dict]:
        if self.artifact_repository is None:
            return []
        items = []
        for artifact in self.artifact_repository.list_for_owner("candidate", candidate_id):
            status = artifact.lifecycle_status
            if artifact.expires_at is not None and artifact.expires_at <= utc_now() and status == "ready":
                status = "expired"
            items.append(
                {
                "artifact_id": artifact.artifact_id,
                "artifact_type": artifact.artifact_type,
                "object_uri": artifact.object_uri,
                "job_id": artifact.job_id,
                "content_type": artifact.content_type,
                "size_bytes": artifact.size_bytes,
                "checksum_sha256": artifact.checksum_sha256,
                "lifecycle_status": status,
                "version": artifact.version,
                "created_at": artifact.created_at.isoformat(),
                "expires_at": artifact.expires_at.isoformat() if artifact.expires_at else None,
                }
            )
        return items

    def get_candidate_artifact_status(self, candidate_id: str) -> str:
        artifacts = self.list_artifacts_for_candidate(candidate_id)
        if not artifacts:
            return "missing"
        statuses = {artifact["lifecycle_status"] for artifact in artifacts}
        if "ready" in statuses:
            return "ready"
        if "expired" in statuses:
            return "expired"
        if "delete_failed" in statuses:
            return "delete_failed"
        return "deleted" if "deleted" in statuses else "missing"

    def has_ready_final_video_artifact(self, candidate_id: str) -> bool:
        return any(
            artifact["artifact_type"] == "final_video" and artifact["lifecycle_status"] == "ready"
            for artifact in self.list_artifacts_for_candidate(candidate_id)
        )

    def ensure_video_for_candidate(self, candidate: VideoCandidate) -> Video:
        video_repository = self.get_video_repository_or_raise()
        existing = video_repository.get(candidate.video_id)
        if existing is not None:
            return existing

        video = Video(
            video_id=candidate.video_id,
            spotify_id=candidate.spotify_id,
            title=candidate.title,
            published_at=candidate.published_at,
        )
        video_repository.upsert(video)
        return video

    def get_pending_review_for_candidate(
        self,
        candidate_id: str,
        review_type: ReviewType,
    ) -> ReviewItem:
        reviews = self.get_reviews_for_candidate(candidate_id)
        review = next((item for item in reviews if item.review_type == review_type), None)
        if review is None:
            raise KeyError(f"Review checkpoint {review_type.value} not found")
        if review.status != ReviewStatus.PENDING:
            raise ReviewConflictError(
                f"Review checkpoint {review_type.value} is already {review.status.value}."
            )
        return review

    def apply_review_decision(
        self,
        review_id: str,
        actor_id: str,
        expected_version: int,
        approve: bool,
        comment: str | None = None,
    ) -> dict:
        self.bootstrap_reviews()
        review = self.review_repository.get(review_id)
        if review is None:
            raise KeyError("Review not found")
        if review.status != ReviewStatus.PENDING:
            raise ReviewConflictError("Review has already been decided.")
        if expected_version != review.version:
            raise ReviewConflictError(
                f"Stale review version. expected={expected_version} current={review.version}"
            )

        if (
            approve
            and review.review_type == ReviewType.FINAL_ASSET_APPROVAL
            and not self.has_ready_final_video_artifact(review.subject_id)
        ):
            raise ReviewConflictError("Final asset approval requires a ready final_video artifact.")

        now = utc_now()
        review.status = ReviewStatus.APPROVED if approve else ReviewStatus.REJECTED
        review.version += 1
        review.decision_comment = comment
        review.decided_by = actor_id
        review.decided_at = now
        review.updated_at = now
        self.review_repository.update(review)

        candidate = self.candidate_repository.get(review.subject_id)
        if candidate is None:
            raise KeyError("Candidate not found")

        self._log(
            aggregate_type="review",
            aggregate_id=review.review_id,
            action="review_approved" if approve else "review_rejected",
            actor_id=actor_id,
            details=comment,
        )

        next_review = None
        if approve:
            next_review_type = self._next_review_type(review.review_type)
            if next_review_type is None:
                candidate.status = CandidateStatus.ACCEPTED
            else:
                existing_next = next(
                    (
                        item
                        for item in self.get_reviews_for_candidate(candidate.candidate_id)
                        if item.review_type == next_review_type
                    ),
                    None,
                )
                if existing_next is None:
                    next_review = ReviewItem(
                        review_id=self._new_id("review"),
                        subject_kind="candidate",
                        subject_id=candidate.candidate_id,
                        spotify_id=candidate.spotify_id,
                        review_type=next_review_type,
                    )
                    self.review_repository.create(next_review)
                    self._log(
                        aggregate_type="candidate",
                        aggregate_id=candidate.candidate_id,
                        action="workflow_promoted",
                        actor_id=actor_id,
                        details=f"{review.review_type.value}->{next_review.review_type.value}",
                    )
        else:
            candidate.status = CandidateStatus.REJECTED

        self.candidate_repository.upsert(candidate)
        return {
            "review_id": review.review_id,
            "status": review.status.value,
            "version": review.version,
            "subject_id": review.subject_id,
            "candidate_status": candidate.status.value,
            "next_review_id": next_review.review_id if next_review is not None else None,
            "next_review_type": next_review.review_type.value if next_review is not None else None,
            "decided_at": review.decided_at.isoformat() if review.decided_at is not None else None,
        }

    def log_structured(
        self,
        *,
        aggregate_type: str,
        aggregate_id: str,
        action: str,
        actor_id: str,
        payload: dict,
    ) -> None:
        self._log(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            action=action,
            actor_id=actor_id,
            details=json.dumps(payload, ensure_ascii=True, sort_keys=True),
        )

    def _next_review_type(self, review_type: ReviewType) -> ReviewType | None:
        try:
            index = self.REVIEW_SEQUENCE.index(review_type)
        except ValueError:
            return None
        if index + 1 >= len(self.REVIEW_SEQUENCE):
            return None
        return self.REVIEW_SEQUENCE[index + 1]

    def _log(
        self,
        aggregate_type: str,
        aggregate_id: str,
        action: str,
        actor_id: str,
        details: str | None = None,
    ) -> None:
        self.audit_log_repository.add(
            AuditLogEntry(
                log_id=self._new_id("audit"),
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                action=action,
                actor_id=actor_id,
                details=details,
            )
        )

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:12]}"


class AuditService:
    def __init__(self, support: WorkflowSupport) -> None:
        self.support = support

    def list_queue(self, status: str | None = None) -> list[dict]:
        self.support.bootstrap_reviews()
        items: list[dict] = []
        normalized_status = status.strip().lower() if status else None
        for artist, candidate in self.support.list_candidates_with_artist():
            reviews = self.support.get_reviews_for_candidate(candidate.candidate_id)
            selected_review = self._select_queue_review(reviews, normalized_status)
            if selected_review is None:
                continue
            items.append(
                {
                    "review_id": selected_review.review_id,
                    "artist_id": artist.spotify_id,
                    "artist_name": artist.name,
                    "candidate_id": candidate.candidate_id,
                    "candidate_title": candidate.title,
                    "review_type": selected_review.review_type.value,
                    "status": selected_review.status.value,
                    "version": selected_review.version,
                    "decided_by": selected_review.decided_by,
                    "decided_at": (
                        selected_review.decided_at.isoformat()
                        if selected_review.decided_at is not None
                        else None
                    ),
                    "decision_comment": selected_review.decision_comment,
                    "published_at": (
                        candidate.published_at.isoformat()
                        if candidate.published_at is not None
                        else None
                    ),
                    "source_url": candidate.source_url,
                    "queued_at": selected_review.created_at.isoformat(),
                }
            )
        return items

    @staticmethod
    def _select_queue_review(
        reviews: list[ReviewItem],
        normalized_status: str | None,
    ) -> ReviewItem | None:
        pending_review = next((review for review in reviews if review.status == ReviewStatus.PENDING), None)
        approved_reviews = [review for review in reviews if review.status == ReviewStatus.APPROVED]
        rejected_reviews = [review for review in reviews if review.status == ReviewStatus.REJECTED]
        latest_approved = approved_reviews[-1] if approved_reviews else None
        latest_rejected = rejected_reviews[-1] if rejected_reviews else None

        if normalized_status == "pending":
            return pending_review
        if normalized_status == "approved":
            return latest_approved
        if normalized_status == "rejected":
            return latest_rejected

        return pending_review or latest_rejected or latest_approved

    def list_audit_logs(self, aggregate_type: str, aggregate_id: str) -> list[dict]:
        return [
            {
                "log_id": entry.log_id,
                "aggregate_type": entry.aggregate_type,
                "aggregate_id": entry.aggregate_id,
                "action": entry.action,
                "actor_id": entry.actor_id,
                "details": entry.details,
                "created_at": entry.created_at.isoformat(),
            }
            for entry in self.support.audit_log_repository.list_for_aggregate(
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
            )
        ]

    def approve_review(
        self,
        review_id: str,
        actor_id: str,
        expected_version: int,
        comment: str | None = None,
    ) -> dict:
        return self.support.apply_review_decision(
            review_id=review_id,
            actor_id=actor_id,
            expected_version=expected_version,
            approve=True,
            comment=comment,
        )

    def reject_review(
        self,
        review_id: str,
        actor_id: str,
        expected_version: int,
        comment: str | None = None,
    ) -> dict:
        return self.support.apply_review_decision(
            review_id=review_id,
            actor_id=actor_id,
            expected_version=expected_version,
            approve=False,
            comment=comment,
        )


class PipelineService:
    def __init__(self, support: WorkflowSupport, translation_service: TranslationService) -> None:
        self.support = support
        self.translation_service = translation_service

    def add_candidate(self, candidate_id: str, actor_id: str) -> dict:
        return self.support.add_candidate_to_pipeline(candidate_id, actor_id)

    def get_candidate_detail(self, candidate_id: str) -> dict:
        self.support.bootstrap_reviews()
        candidate = self.support.get_candidate_or_raise(candidate_id)
        artist = self.support.artist_repository.get(candidate.spotify_id)
        reviews = self.support.get_reviews_for_candidate(candidate_id)
        subtitles = self.support.get_subtitle_repository_or_raise().list_for_video(candidate.video_id)
        audit_logs = self.support.audit_log_repository.list_for_aggregate("candidate", candidate_id)

        return {
            "candidate_id": candidate.candidate_id,
            "artist_id": candidate.spotify_id,
            "artist_name": artist.name if artist is not None else candidate.spotify_id,
            "candidate_title": candidate.title,
            "source_url": candidate.source_url,
            "workflow_status": candidate.status.value,
            "current_stage": self._current_stage(candidate, reviews),
            "reviews": [
                {
                    "review_id": review.review_id,
                    "review_type": review.review_type.value,
                    "status": review.status.value,
                    "version": review.version,
                    "decision_comment": review.decision_comment,
                    "decided_by": review.decided_by,
                    "decided_at": review.decided_at.isoformat() if review.decided_at else None,
                    "created_at": review.created_at.isoformat(),
                    "updated_at": review.updated_at.isoformat(),
                }
                for review in reviews
            ],
            "transcript": {
                "video_id": candidate.video_id,
                "segment_count": len(subtitles),
                "segments": [
                    {
                        "line_index": subtitle.line_index,
                        "start_time": subtitle.start_time,
                        "end_time": subtitle.end_time,
                        "text": subtitle.en_text,
                        "status": subtitle.status.value,
                    }
                    for subtitle in subtitles
                ],
            },
            "taste_audit": self._latest_structured_audit_payload(audit_logs, "taste_audit_recorded"),
            "translation": {
                "line_count": len([subtitle for subtitle in subtitles if subtitle.zh_text]),
                "lines": [
                    {
                        "line_index": subtitle.line_index,
                        "start_time": subtitle.start_time,
                        "end_time": subtitle.end_time,
                        "source_text": subtitle.en_text,
                        "translated_text": subtitle.zh_text,
                        "status": subtitle.status.value,
                    }
                    for subtitle in subtitles
                ],
            },
        }

    def list_pipeline(self) -> list[dict]:
        self.support.bootstrap_reviews()
        items: list[dict] = []
        for artist, candidate in self.support.list_candidates_with_artist():
            if candidate.status not in {CandidateStatus.PENDING_REVIEW, CandidateStatus.DOWNLOADING}:
                continue
            reviews = self.support.get_reviews_for_candidate(candidate.candidate_id)
            subtitles = self.support.get_subtitle_repository_or_raise().list_for_video(candidate.video_id)
            current_review = next((review for review in reviews if review.status == ReviewStatus.PENDING), None)
            stage_items = []
            for review_type in self.support.REVIEW_SEQUENCE:
                review = self.support.get_latest_review_for_candidate(candidate.candidate_id, review_type)
                stage_items.append(
                    {
                        "stage": review_type.value,
                        "status": review.status.value if review is not None else "not_started",
                    }
                )
            items.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "artist_id": artist.spotify_id,
                    "artist_name": artist.name,
                    "candidate_title": candidate.title,
                    "workflow_status": candidate.status.value,
                    "artifact_status": self.support.get_candidate_artifact_status(candidate.candidate_id),
                    "current_stage": (
                        "downloading"
                        if candidate.status == CandidateStatus.DOWNLOADING
                        else current_review.review_type.value
                        if current_review is not None
                        else "completed"
                        if candidate.status == CandidateStatus.ACCEPTED
                        else "rejected"
                    ),
                    "stages": stage_items,
                    "translation": self.translation_service.describe_translation_status(
                        reviews,
                        subtitles=subtitles,
                    ),
                    "last_updated_at": (
                        reviews[-1].updated_at.isoformat() if reviews else candidate.last_seen_at.isoformat()
                    ),
                }
            )
        return items

    @staticmethod
    def _latest_structured_audit_payload(audit_logs: list[AuditLogEntry], action: str) -> dict | None:
        matching_logs = [entry for entry in audit_logs if entry.action == action and entry.details]
        if not matching_logs:
            return None
        latest = matching_logs[-1]
        try:
            payload = json.loads(latest.details or "{}")
        except json.JSONDecodeError:
            payload = {"raw_details": latest.details}
        payload["recorded_at"] = latest.created_at.isoformat()
        payload["recorded_by"] = latest.actor_id
        return payload

    @staticmethod
    def _current_stage(candidate: VideoCandidate, reviews: list[ReviewItem]) -> str:
        current_review = next((review for review in reviews if review.status == ReviewStatus.PENDING), None)
        if current_review is not None:
            return current_review.review_type.value
        if candidate.status == CandidateStatus.ACCEPTED:
            return "completed"
        if candidate.status == CandidateStatus.REJECTED:
            return "rejected"
        return "not_started"


class LibraryService:
    def __init__(self, support: WorkflowSupport) -> None:
        self.support = support

    def list_library(self) -> list[dict]:
        self.support.bootstrap_reviews()
        items: list[dict] = []
        for artist, candidate in self.support.list_candidates_with_artist():
            if candidate.status != CandidateStatus.ACCEPTED:
                continue
            reviews = self.support.get_reviews_for_candidate(candidate.candidate_id)
            final_review = next(
                (
                    review
                    for review in reviews
                    if review.review_type == ReviewType.FINAL_ASSET_APPROVAL
                    and review.status == ReviewStatus.APPROVED
                ),
                None,
            )
            if final_review is None:
                continue
            items.append(
                {
                    "asset_id": candidate.candidate_id,
                    "artist_id": artist.spotify_id,
                    "artist_name": artist.name,
                    "title": candidate.title,
                    "source_url": candidate.source_url,
                    "approved_at": final_review.decided_at.isoformat() if final_review.decided_at else None,
                    "approved_by": final_review.decided_by,
                    "curation_status": candidate.status.value,
                    "artifact_status": self.support.get_candidate_artifact_status(candidate.candidate_id),
                    "artifacts": self.support.list_artifacts_for_candidate(candidate.candidate_id),
                }
            )
        return items


class AutomationService:
    def __init__(self, support: WorkflowSupport) -> None:
        self.support = support

    def submit_transcript(
        self,
        candidate_id: str,
        actor_id: str,
        segments: list[dict],
        auto_approve_review: bool = False,
        comment: str | None = None,
    ) -> dict:
        self.support.bootstrap_reviews()
        candidate = self.support.get_candidate_or_raise(candidate_id)
        video = self.support.ensure_video_for_candidate(candidate)
        subtitle_repository = self.support.get_subtitle_repository_or_raise()
        subtitles = []
        for index, segment in enumerate(segments):
            subtitles.append(
                Subtitle(
                    video_id=video.video_id,
                    line_index=index,
                    start_time=segment["start_time"],
                    end_time=segment["end_time"],
                    en_text=segment["text"],
                    status=StageStatus.COMPLETED,
                )
            )
        subtitle_repository.replace_for_video(video.video_id, subtitles)

        video.processed_status = StageStatus.COMPLETED
        self.support.get_video_repository_or_raise().upsert(video)
        self.support.log_structured(
            aggregate_type="candidate",
            aggregate_id=candidate_id,
            action="transcript_updated",
            actor_id=actor_id,
            payload={
                "video_id": video.video_id,
                "segment_count": len(subtitles),
                "auto_approve_review": auto_approve_review,
            },
        )

        response = {
            "candidate_id": candidate_id,
            "video_id": video.video_id,
            "segment_count": len(subtitles),
            "auto_approve_review": auto_approve_review,
            "review_id": None,
            "review_status": None,
            "candidate_status": candidate.status.value,
            "next_review_id": None,
            "next_review_type": None,
        }
        if auto_approve_review:
            review = self.support.get_pending_review_for_candidate(
                candidate_id,
                ReviewType.TRANSCRIPT_REVIEW,
            )
            decision = self.support.apply_review_decision(
                review_id=review.review_id,
                actor_id=actor_id,
                expected_version=review.version,
                approve=True,
                comment=comment,
            )
            response.update(
                {
                    "review_id": decision["review_id"],
                    "review_status": decision["status"],
                    "candidate_status": decision["candidate_status"],
                    "next_review_id": decision["next_review_id"],
                    "next_review_type": decision["next_review_type"],
                }
            )
        return response

    def record_taste_audit(
        self,
        candidate_id: str,
        actor_id: str,
        approve: bool,
        comment: str | None = None,
        score: float | None = None,
        key_lyrics: list[str] | None = None,
    ) -> dict:
        self.support.bootstrap_reviews()
        self.support.get_candidate_or_raise(candidate_id)
        review = self.support.get_pending_review_for_candidate(
            candidate_id,
            ReviewType.TASTE_AUDIT,
        )
        self.support.log_structured(
            aggregate_type="candidate",
            aggregate_id=candidate_id,
            action="taste_audit_recorded",
            actor_id=actor_id,
            payload={
                "decision": "approved" if approve else "rejected",
                "score": score,
                "key_lyrics": key_lyrics or [],
                "comment": comment,
            },
        )
        decision = self.support.apply_review_decision(
            review_id=review.review_id,
            actor_id=actor_id,
            expected_version=review.version,
            approve=approve,
            comment=comment,
        )
        decision["score"] = score
        decision["key_lyrics"] = key_lyrics or []
        return decision

    def submit_translation(
        self,
        candidate_id: str,
        actor_id: str,
        translations: list[dict],
        auto_approve_review: bool = False,
        comment: str | None = None,
    ) -> dict:
        self.support.bootstrap_reviews()
        candidate = self.support.get_candidate_or_raise(candidate_id)
        subtitle_repository = self.support.get_subtitle_repository_or_raise()
        subtitles = subtitle_repository.list_for_video(candidate.video_id)
        if not subtitles:
            raise ValueError("Transcript must exist before translation can be submitted.")

        updates = {item["line_index"]: item["zh_text"] for item in translations}
        unknown_indexes = sorted(set(updates) - {subtitle.line_index for subtitle in subtitles})
        if unknown_indexes:
            raise ValueError(
                "Translation line_index does not exist: "
                + ", ".join(str(index) for index in unknown_indexes)
            )

        translated_count = 0
        for subtitle in subtitles:
            translated_text = updates.get(subtitle.line_index)
            if translated_text is not None:
                subtitle.zh_text = translated_text
                subtitle.status = StageStatus.COMPLETED
            if subtitle.zh_text is not None and subtitle.zh_text.strip():
                translated_count += 1
        subtitle_repository.replace_for_video(candidate.video_id, subtitles)

        self.support.log_structured(
            aggregate_type="candidate",
            aggregate_id=candidate_id,
            action="translation_updated",
            actor_id=actor_id,
            payload={
                "video_id": candidate.video_id,
                "line_count": translated_count,
                "auto_approve_review": auto_approve_review,
            },
        )

        response = {
            "candidate_id": candidate_id,
            "video_id": candidate.video_id,
            "line_count": translated_count,
            "auto_approve_review": auto_approve_review,
            "review_id": None,
            "review_status": None,
            "candidate_status": candidate.status.value,
            "next_review_id": None,
            "next_review_type": None,
        }
        if auto_approve_review:
            review = self.support.get_pending_review_for_candidate(
                candidate_id,
                ReviewType.TRANSLATION_REVIEW,
            )
            decision = self.support.apply_review_decision(
                review_id=review.review_id,
                actor_id=actor_id,
                expected_version=review.version,
                approve=True,
                comment=comment,
            )
            response.update(
                {
                    "review_id": decision["review_id"],
                    "review_status": decision["status"],
                    "candidate_status": decision["candidate_status"],
                    "next_review_id": decision["next_review_id"],
                    "next_review_type": decision["next_review_type"],
                }
            )
        return response


class Phase4WorkflowServices:
    def __init__(
        self,
        artist_service: ArtistService,
        audit_service: AuditService,
        pipeline_service: PipelineService,
        library_service: LibraryService,
        translation_service: TranslationService,
        automation_service: AutomationService,
    ) -> None:
        self.artist_service = artist_service
        self.audit_service = audit_service
        self.pipeline_service = pipeline_service
        self.library_service = library_service
        self.translation_service = translation_service
        self.automation_service = automation_service


def build_phase4_workflow_services(
    artist_repository: ArtistRepository,
    candidate_repository: CandidateRepository,
    review_repository: ReviewRepository,
    audit_log_repository: AuditLogRepository,
    subtitle_repository: SubtitleRepository | None = None,
    video_repository: VideoRepository | None = None,
    artifact_repository: ArtifactRepository | None = None,
) -> Phase4WorkflowServices:
    artist_service = ArtistService(artist_repository)
    translation_service = TranslationService()
    support = WorkflowSupport(
        artist_repository=artist_repository,
        candidate_repository=candidate_repository,
        review_repository=review_repository,
        audit_log_repository=audit_log_repository,
        subtitle_repository=subtitle_repository,
        video_repository=video_repository,
        artifact_repository=artifact_repository,
    )
    return Phase4WorkflowServices(
        artist_service=artist_service,
        audit_service=AuditService(support),
        pipeline_service=PipelineService(support, translation_service),
        library_service=LibraryService(support),
        translation_service=translation_service,
        automation_service=AutomationService(support),
    )
