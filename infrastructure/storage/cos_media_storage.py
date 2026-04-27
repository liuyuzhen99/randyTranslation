from __future__ import annotations

import hashlib
import os
import shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from domain.storage import MediaStorageService, StoredMediaObject
from domain.time_utils import utc_now


class TencentCOSMediaStorage(MediaStorageService):
    """Tencent COS adapter with local temp workspaces for render-heavy pipeline stages."""

    def __init__(
        self,
        *,
        temp_root: str | None = None,
        bucket: str | None = None,
        region: str | None = None,
        secret_id: str | None = None,
        secret_key: str | None = None,
        scheme: str = "https",
        endpoint: str | None = None,
        client=None,
    ) -> None:
        project_root = Path.cwd()
        default_temp_root = project_root / "data" / "media" / "temp"
        self.temp_root = temp_root or os.getenv("MEDIA_TEMP_ROOT", str(default_temp_root))
        self.bucket = bucket or os.getenv("COS_BUCKET", "").strip()
        self.region = region or os.getenv("COS_REGION", "").strip()
        self.storage_provider = "tencent-cos"
        if not self.bucket:
            raise RuntimeError("COS_BUCKET is required when MEDIA_STORAGE_BACKEND=cos.")
        if not self.region:
            raise RuntimeError("COS_REGION is required when MEDIA_STORAGE_BACKEND=cos.")
        self.client = client or self._build_client(
            secret_id=secret_id or os.getenv("COS_SECRET_ID", "").strip(),
            secret_key=secret_key or os.getenv("COS_SECRET_KEY", "").strip(),
            region=self.region,
            scheme=scheme or os.getenv("COS_SCHEME", "https").strip() or "https",
            endpoint=endpoint or os.getenv("COS_ENDPOINT", "").strip() or None,
        )

    def prepare_task_workspace(self, task_id: str) -> str:
        task_dir = os.path.join(self.temp_root, task_id)
        os.makedirs(task_dir, exist_ok=True)
        return task_dir

    def resolve_temp_file(self, task_id: str, filename: str) -> str:
        return os.path.join(self.temp_root, task_id, filename)

    def resolve_final_output(self, task_id: str) -> str:
        return self.resolve_temp_file(task_id, "final_video.mp4")

    def upload_artifact(
        self,
        task_id: str,
        local_path: str,
        artifact_type: str,
        content_type: str | None = None,
    ) -> StoredMediaObject:
        object_key = self.build_object_key(task_id, artifact_type, Path(local_path).name)
        self.client.upload_file(
            Bucket=self.bucket,
            LocalFilePath=local_path,
            Key=object_key,
            PartSize=8,
            MAXThread=8,
            EnableMD5=False,
        )
        stat = os.stat(local_path)
        return StoredMediaObject(
            artifact_type=artifact_type,
            object_uri=self._object_uri(object_key),
            object_key=object_key,
            bucket=self.bucket,
            storage_provider=self.storage_provider,
            content_type=content_type,
            size_bytes=stat.st_size,
            checksum_sha256=self._sha256(local_path),
            created_at=utc_now(),
        )

    def download_artifact(self, object_uri: str, destination_path: str) -> str:
        object_key = self._key_for_uri(object_uri)
        destination_dir = os.path.dirname(destination_path)
        if destination_dir:
            os.makedirs(destination_dir, exist_ok=True)
        self.client.download_file(Bucket=self.bucket, Key=object_key, DestFilePath=destination_path)
        return destination_path

    def delete_artifact(self, object_uri: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._key_for_uri(object_uri))

    def create_presigned_url(self, object_uri: str, expires_in_seconds: int = 900) -> str:
        return self.client.get_presigned_url(
            Method="GET",
            Bucket=self.bucket,
            Key=self._key_for_uri(object_uri),
            Expired=expires_in_seconds,
        )

    def cleanup_task_workspace(self, task_id: str) -> None:
        task_dir = os.path.join(self.temp_root, task_id)
        if os.path.exists(task_dir):
            shutil.rmtree(task_dir)

    def cleanup_stale_task_workspaces(self, older_than) -> list[str]:
        if not os.path.isdir(self.temp_root):
            return []
        deleted: list[str] = []
        for entry in os.scandir(self.temp_root):
            if not entry.is_dir():
                continue
            modified_at = datetime.fromtimestamp(entry.stat().st_mtime)
            if modified_at >= older_than:
                continue
            shutil.rmtree(entry.path)
            deleted.append(entry.name)
        return deleted

    def build_object_key(self, task_id: str, artifact_type: str, filename: str) -> str:
        safe_task_id = self._safe_key_part(task_id)
        safe_artifact_type = self._safe_key_part(artifact_type)
        safe_filename = self._safe_filename(filename)
        return f"pipeline/{safe_task_id}/{safe_artifact_type}/v1/{safe_filename}"

    def _object_uri(self, object_key: str) -> str:
        return f"cos://{self.bucket}/{object_key}"

    def _key_for_uri(self, object_uri: str) -> str:
        parsed = urlparse(object_uri)
        if parsed.scheme != "cos" or parsed.netloc != self.bucket:
            raise ValueError(f"Unsupported COS object URI for this storage adapter: {object_uri}")
        return unquote(parsed.path.lstrip("/"))

    @staticmethod
    def _build_client(
        *,
        secret_id: str,
        secret_key: str,
        region: str,
        scheme: str,
        endpoint: str | None,
    ):
        if not secret_id:
            raise RuntimeError("COS_SECRET_ID is required when MEDIA_STORAGE_BACKEND=cos.")
        if not secret_key:
            raise RuntimeError("COS_SECRET_KEY is required when MEDIA_STORAGE_BACKEND=cos.")
        try:
            from qcloud_cos import CosConfig, CosS3Client
        except ImportError as exc:
            raise RuntimeError(
                "Tencent COS storage requires cos-python-sdk-v5. "
                "Install requirements.txt before using MEDIA_STORAGE_BACKEND=cos."
            ) from exc

        kwargs = {
            "Region": region,
            "SecretId": secret_id,
            "SecretKey": secret_key,
            "Scheme": scheme,
        }
        if endpoint:
            kwargs["Endpoint"] = endpoint
        return CosS3Client(CosConfig(**kwargs))

    @staticmethod
    def _sha256(path: str) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as file_obj:
            for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _safe_key_part(value: str) -> str:
        safe_value = "".join(
            char if char.isalnum() or char in {"-", "_"} else "-"
            for char in value
        )
        return safe_value.strip("-")

    @staticmethod
    def _safe_filename(filename: str) -> str:
        name = os.path.basename(filename)
        return "".join(char if char.isalnum() or char in {"-", "_", "."} else "-" for char in name)
