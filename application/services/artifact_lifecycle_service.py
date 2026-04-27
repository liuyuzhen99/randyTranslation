from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging

from domain.repositories import ArtifactRepository
from domain.storage import MediaStorageService
from domain.time_utils import utc_now

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArtifactLifecyclePolicy:
    temp_retention_days: int = 1
    final_artifact_retention_days: int = 0


class ArtifactLifecycleService:
    """Deletes stale temp workspaces separately from durable final artifacts."""

    def __init__(
        self,
        *,
        artifact_repository: ArtifactRepository | None,
        media_storage: MediaStorageService,
        policy: ArtifactLifecyclePolicy,
    ) -> None:
        self.artifact_repository = artifact_repository
        self.media_storage = media_storage
        self.policy = policy

    def run_once(self) -> dict:
        now = utc_now()
        temp_cutoff = now - timedelta(days=max(self.policy.temp_retention_days, 0))
        deleted_temp_workspaces = self.media_storage.cleanup_stale_task_workspaces(temp_cutoff)

        deleted_artifacts: list[str] = []
        failed_artifacts: list[dict] = []
        if self.artifact_repository is not None:
            for artifact in self.artifact_repository.list_expired(now):
                try:
                    self.media_storage.delete_artifact(artifact.object_uri)
                    artifact.lifecycle_status = "deleted"
                    artifact.updated_at = now
                    self.artifact_repository.upsert(artifact)
                    deleted_artifacts.append(artifact.artifact_id)
                except Exception as exc:
                    artifact.lifecycle_status = "delete_failed"
                    artifact.updated_at = now
                    self.artifact_repository.upsert(artifact)
                    failed_artifacts.append({"artifact_id": artifact.artifact_id, "error": str(exc)})
                    logger.exception("Failed to delete expired artifact %s", artifact.artifact_id)

        return {
            "temp_retention_days": self.policy.temp_retention_days,
            "final_artifact_retention_days": self.policy.final_artifact_retention_days,
            "deleted_temp_workspaces": deleted_temp_workspaces,
            "deleted_artifacts": deleted_artifacts,
            "failed_artifacts": failed_artifacts,
            "completed_at": now.isoformat(),
        }
