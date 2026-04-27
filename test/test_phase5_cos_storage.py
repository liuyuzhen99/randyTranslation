import os
import tempfile
import unittest
from unittest.mock import patch

import api.service as api_service
from api.config import create_media_storage, load_runtime_settings
from domain.entities import ArtifactRecord
from domain.time_utils import utc_now
from fastapi.testclient import TestClient
from infrastructure.persistence.sqlalchemy_repositories import (
    SQLAlchemyArtifactRepository,
    SQLAlchemySessionFactory,
)
from infrastructure.storage.local_media_storage import LocalFilesystemMediaStorage
from infrastructure.storage.cos_media_storage import TencentCOSMediaStorage


class FakeCOSClient:
    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}
        self.deleted: list[tuple[str, str]] = []

    def upload_file(self, *, Bucket, LocalFilePath, Key, PartSize, MAXThread, EnableMD5):
        with open(LocalFilePath, "rb") as file_obj:
            self.objects[(Bucket, Key)] = file_obj.read()
        self.upload_options = {
            "PartSize": PartSize,
            "MAXThread": MAXThread,
            "EnableMD5": EnableMD5,
        }

    def download_file(self, *, Bucket, Key, DestFilePath):
        with open(DestFilePath, "wb") as file_obj:
            file_obj.write(self.objects[(Bucket, Key)])

    def delete_object(self, *, Bucket, Key):
        self.deleted.append((Bucket, Key))
        self.objects.pop((Bucket, Key), None)

    def get_presigned_url(self, *, Method, Bucket, Key, Expired):
        return f"https://cos.example/{Bucket}/{Key}?method={Method}&expires={Expired}"


class Phase5COSStorageTests(unittest.TestCase):
    def test_tencent_cos_storage_lifecycle_with_injected_client(self):
        with tempfile.TemporaryDirectory() as temp_root:
            client = FakeCOSClient()
            storage = TencentCOSMediaStorage(
                temp_root=temp_root,
                bucket="randy-translation-1250000000",
                region="ap-shanghai",
                client=client,
            )

            workspace = storage.prepare_task_workspace("job cos/01")
            local_file = storage.resolve_temp_file("job cos/01", "final video.mp4")
            with open(local_file, "wb") as file_obj:
                file_obj.write(b"cos artifact")

            artifact = storage.upload_artifact(
                "job cos/01",
                local_file,
                artifact_type="final_video",
                content_type="video/mp4",
            )

            self.assertEqual(artifact.storage_provider, "tencent-cos")
            self.assertEqual(
                artifact.object_uri,
                "cos://randy-translation-1250000000/pipeline/job-cos-01/final_video/v1/final-video.mp4",
            )
            self.assertEqual(artifact.size_bytes, len(b"cos artifact"))
            self.assertIn((artifact.bucket, artifact.object_key), client.objects)
            self.assertEqual(client.upload_options["PartSize"], 8)

            downloaded = os.path.join(temp_root, "downloaded.mp4")
            storage.download_artifact(artifact.object_uri, downloaded)
            with open(downloaded, "rb") as file_obj:
                self.assertEqual(file_obj.read(), b"cos artifact")

            self.assertIn("expires=120", storage.create_presigned_url(artifact.object_uri, 120))
            storage.delete_artifact(artifact.object_uri)
            self.assertEqual(client.deleted, [(artifact.bucket, artifact.object_key)])

            storage.cleanup_task_workspace("job cos/01")
            self.assertFalse(os.path.exists(workspace))

    def test_create_media_storage_uses_local_by_default(self):
        settings = load_runtime_settings({"JOB_REPOSITORY_BACKEND": "memory"})

        storage = create_media_storage(
            environ={"JOB_REPOSITORY_BACKEND": "memory", "MEDIA_OUTPUT_ROOT": "/tmp/media-output"},
            runtime_settings=settings,
        )

        self.assertEqual(storage.storage_provider, "local-oss")

    def test_local_artifact_download_endpoint_serves_signed_style_url(self):
        with tempfile.TemporaryDirectory() as temp_root, tempfile.TemporaryDirectory() as output_root:
            env = {
                "DEEPSEEK_API_KEY": "test-key",
                "DEEPSEEK_BASE_URL": "https://example.local",
                "JOB_REPOSITORY_BACKEND": "memory",
                "PHASE2_SHADOW_WRITE_ENABLED": "false",
                "PHASE2_RECONCILE_ENABLED": "false",
                "MEDIA_STORAGE_BACKEND": "local",
                "MEDIA_TEMP_ROOT": temp_root,
                "MEDIA_OUTPUT_ROOT": output_root,
            }
            with patch.dict(os.environ, env, clear=False):
                app = api_service.create_app()
                storage = LocalFilesystemMediaStorage(temp_root=temp_root, output_root=output_root)
                workspace = storage.prepare_task_workspace("download01")
                local_file = storage.resolve_temp_file("download01", "final_video.mp4")
                with open(local_file, "wb") as file_obj:
                    file_obj.write(b"download me")
                artifact = storage.upload_artifact("download01", local_file, "final_video", "video/mp4")
                app.state.media_storage = storage

                with TestClient(app) as client:
                    response = client.get(storage.create_presigned_url(artifact.object_uri, 60))

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, b"download me")
                storage.cleanup_task_workspace("download01")
                self.assertFalse(os.path.exists(workspace))

    def test_artifact_detail_refresh_and_fallback_download(self):
        with tempfile.TemporaryDirectory() as temp_root, tempfile.TemporaryDirectory() as output_root:
            env = {
                "DEEPSEEK_API_KEY": "test-key",
                "DEEPSEEK_BASE_URL": "https://example.local",
                "JOB_REPOSITORY_BACKEND": "memory",
                "MEDIA_STORAGE_BACKEND": "local",
                "MEDIA_TEMP_ROOT": temp_root,
                "MEDIA_OUTPUT_ROOT": output_root,
            }
            with patch.dict(os.environ, env, clear=False):
                app = api_service.create_app()
                session_factory = SQLAlchemySessionFactory(f"sqlite:///{os.path.join(temp_root, 'artifacts.db')}")
                session_factory.create_schema()
                artifact_repo = SQLAlchemyArtifactRepository(session_factory)
                storage = LocalFilesystemMediaStorage(temp_root=temp_root, output_root=output_root)
                workspace = storage.prepare_task_workspace("detail01")
                local_file = storage.resolve_temp_file("detail01", "final_video.mp4")
                with open(local_file, "wb") as file_obj:
                    file_obj.write(b"detail video")
                stored = storage.upload_artifact("detail01", local_file, "final_video", "video/mp4")
                artifact = ArtifactRecord(
                    artifact_id="job:detail01:final_video:v1",
                    owner_type="job",
                    owner_id="detail01",
                    artifact_type="final_video",
                    object_uri=stored.object_uri,
                    object_key=stored.object_key,
                    bucket=stored.bucket,
                    storage_provider=stored.storage_provider,
                    content_type=stored.content_type,
                    size_bytes=stored.size_bytes,
                    checksum_sha256=stored.checksum_sha256,
                    created_at=stored.created_at,
                    updated_at=utc_now(),
                )
                artifact_repo.upsert(artifact)
                app.state.artifact_repository = artifact_repo
                app.state.media_storage = storage

                with TestClient(app) as client:
                    detail = client.get("/v1/artifacts/job:detail01:final_video:v1")
                    refreshed = client.post("/v1/artifacts/job:detail01:final_video:v1/refresh-url")
                    downloaded = client.get("/v1/artifacts/job:detail01:final_video:v1/download")

                self.assertEqual(detail.status_code, 200)
                self.assertEqual(detail.json()["status"], "ready")
                self.assertEqual(detail.json()["fallback_download_url"], "/v1/artifacts/job:detail01:final_video:v1/download")
                self.assertEqual(refreshed.status_code, 200)
                self.assertIn("/v1/artifacts/download?uri=", refreshed.json()["url"])
                self.assertEqual(downloaded.status_code, 200)
                self.assertEqual(downloaded.content, b"detail video")
                storage.cleanup_task_workspace("detail01")
                self.assertFalse(os.path.exists(workspace))

    def test_artifact_lifecycle_deletes_expired_final_artifacts_and_stale_temp(self):
        with tempfile.TemporaryDirectory() as temp_root, tempfile.TemporaryDirectory() as output_root:
            env = {
                "DEEPSEEK_API_KEY": "test-key",
                "DEEPSEEK_BASE_URL": "https://example.local",
                "JOB_REPOSITORY_BACKEND": "memory",
                "MEDIA_STORAGE_BACKEND": "local",
                "MEDIA_TEMP_ROOT": temp_root,
                "MEDIA_OUTPUT_ROOT": output_root,
                "ARTIFACT_TEMP_RETENTION_DAYS": "0",
            }
            with patch.dict(os.environ, env, clear=False):
                app = api_service.create_app()
                session_factory = SQLAlchemySessionFactory(f"sqlite:///{os.path.join(temp_root, 'lifecycle.db')}")
                session_factory.create_schema()
                artifact_repo = SQLAlchemyArtifactRepository(session_factory)
                storage = LocalFilesystemMediaStorage(temp_root=temp_root, output_root=output_root)
                stale_workspace = storage.prepare_task_workspace("stale01")
                old_timestamp = 1
                os.utime(stale_workspace, (old_timestamp, old_timestamp))
                local_file = os.path.join(temp_root, "final_video.mp4")
                with open(local_file, "wb") as file_obj:
                    file_obj.write(b"expired")
                stored = storage.upload_artifact("expired01", local_file, "final_video", "video/mp4")
                artifact_repo.upsert(
                    ArtifactRecord(
                        artifact_id="job:expired01:final_video:v1",
                        owner_type="job",
                        owner_id="expired01",
                        artifact_type="final_video",
                        object_uri=stored.object_uri,
                        object_key=stored.object_key,
                        bucket=stored.bucket,
                        storage_provider=stored.storage_provider,
                        content_type=stored.content_type,
                        size_bytes=stored.size_bytes,
                        checksum_sha256=stored.checksum_sha256,
                        created_at=stored.created_at,
                        updated_at=utc_now(),
                        expires_at=utc_now(),
                    )
                )
                app.state.artifact_repository = artifact_repo
                app.state.media_storage = storage
                app.state.artifact_lifecycle_service.artifact_repository = artifact_repo
                app.state.artifact_lifecycle_service.media_storage = storage

                with TestClient(app) as client:
                    response = client.post("/internal/phase5/artifacts/lifecycle")

                self.assertEqual(response.status_code, 200)
                self.assertIn("stale01", response.json()["deleted_temp_workspaces"])
                self.assertIn("job:expired01:final_video:v1", response.json()["deleted_artifacts"])
                self.assertEqual(artifact_repo.get("job:expired01:final_video:v1").lifecycle_status, "deleted")


if __name__ == "__main__":
    unittest.main()
