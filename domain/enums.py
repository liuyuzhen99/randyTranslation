from enum import Enum


class JobStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class StageType(str, Enum):
    DOWNLOAD = "download"
    TRANSCRIBE = "transcribe"
    AUDIT = "audit"
    MANUAL_REVIEW = "manual_review"
    TRANSLATE = "translate"
    TRANSLATION_REVIEW = "translation_review"
    RENDER = "render"


class StageStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRY_SCHEDULED = "retry_scheduled"
    DLQ = "dlq"


class OutboxStatus(str, Enum):
    PENDING = "pending"
    PUBLISHED = "published"
    FAILED = "failed"


class SyncStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"


class CandidateStatus(str, Enum):
    DISCOVERED = "discovered"
    DOWNLOADING = "downloading"
    PENDING_REVIEW = "pending_review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class ReviewType(str, Enum):
    TRANSCRIPT_REVIEW = "transcript_review"
    TASTE_AUDIT = "taste_audit"
    MANUAL_REVIEW = "manual_review"
    TRANSLATION_REVIEW = "translation_review"
    FINAL_ASSET_APPROVAL = "final_asset_approval"


class ReviewStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
