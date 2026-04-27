from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class StoredMediaObject:
    artifact_type: str
    object_uri: str
    object_key: str
    bucket: str
    storage_provider: str
    content_type: str | None
    size_bytes: int
    checksum_sha256: str
    created_at: datetime


class MediaStorageService(ABC):
    @abstractmethod
    def prepare_task_workspace(self, task_id: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def resolve_temp_file(self, task_id: str, filename: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def resolve_final_output(self, task_id: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def upload_artifact(
        self,
        task_id: str,
        local_path: str,
        artifact_type: str,
        content_type: str | None = None,
    ) -> StoredMediaObject:
        raise NotImplementedError

    @abstractmethod
    def download_artifact(self, object_uri: str, destination_path: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def delete_artifact(self, object_uri: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def create_presigned_url(self, object_uri: str, expires_in_seconds: int = 900) -> str:
        raise NotImplementedError

    @abstractmethod
    def cleanup_task_workspace(self, task_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def cleanup_stale_task_workspaces(self, older_than: datetime) -> list[str]:
        raise NotImplementedError
