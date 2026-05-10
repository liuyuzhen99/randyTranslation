from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from api.dependencies import get_artifact_repository, get_media_storage
from domain.time_utils import utc_now

router = APIRouter(tags=["artifacts"])


def _resolve_artifact_status(artifact) -> str:
    if artifact.lifecycle_status in {"deleted", "delete_failed"}:
        return artifact.lifecycle_status
    if artifact.expires_at is not None and artifact.expires_at <= utc_now():
        return "expired"
    return artifact.lifecycle_status or "ready"


def _serialize_artifact_detail(artifact, *, media_storage, include_preview_url: bool = True, expires_in_seconds: int = 900) -> dict:
    status = _resolve_artifact_status(artifact)
    preview_url = None
    if include_preview_url and status == "ready":
        preview_url = media_storage.create_presigned_url(artifact.object_uri, expires_in_seconds=expires_in_seconds)
    return {
        "artifact_id": artifact.artifact_id,
        "owner_type": artifact.owner_type,
        "owner_id": artifact.owner_id,
        "artifact_type": artifact.artifact_type,
        "status": status,
        "object_uri": artifact.object_uri,
        "object_key": artifact.object_key,
        "bucket": artifact.bucket,
        "storage_provider": artifact.storage_provider,
        "content_type": artifact.content_type,
        "job_id": artifact.job_id,
        "candidate_id": artifact.candidate_id,
        "size_bytes": artifact.size_bytes,
        "checksum_sha256": artifact.checksum_sha256,
        "version": artifact.version,
        "metadata": artifact.metadata,
        "created_at": artifact.created_at.isoformat(),
        "updated_at": artifact.updated_at.isoformat(),
        "expires_at": artifact.expires_at.isoformat() if artifact.expires_at else None,
        "preview_url": preview_url,
        "preview_url_expires_in_seconds": expires_in_seconds if preview_url else None,
        "fallback_download_url": f"/v1/artifacts/{artifact.artifact_id}/download" if status == "ready" else None,
    }


async def _serve_artifact_uri(uri: str, media_storage, expires_at: str | None = None):
    if expires_at is not None:
        try:
            if utc_now() > datetime.fromisoformat(unquote(expires_at)):
                raise HTTPException(status_code=403, detail="Artifact URL has expired")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid expires_at value") from exc
    suffix = Path(unquote(uri)).suffix or ".bin"
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    temp_file.close()
    try:
        media_storage.download_artifact(unquote(uri), temp_file.name)
    except Exception as exc:
        if os.path.exists(temp_file.name):
            os.remove(temp_file.name)
        raise HTTPException(status_code=404, detail="Artifact not found") from exc
    return FileResponse(
        temp_file.name,
        filename=Path(unquote(uri)).name or "artifact",
        background=BackgroundTask(os.remove, temp_file.name),
    )


@router.get("/v1/artifacts/download")
async def download_artifact_uri(
    uri: str,
    expires_at: str | None = None,
    media_storage=Depends(get_media_storage),
):
    return await _serve_artifact_uri(uri=uri, media_storage=media_storage, expires_at=expires_at)


@router.get("/v1/artifacts/{artifact_id}")
async def get_artifact_detail(
    artifact_id: str,
    include_preview_url: bool = True,
    expires_in_seconds: int = Query(default=900, ge=60, le=86400),
    artifact_repository=Depends(get_artifact_repository),
    media_storage=Depends(get_media_storage),
):
    if artifact_repository is None:
        raise HTTPException(status_code=503, detail="Artifact repository is not enabled")
    artifact = artifact_repository.get(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return _serialize_artifact_detail(
        artifact,
        media_storage=media_storage,
        include_preview_url=include_preview_url,
        expires_in_seconds=expires_in_seconds,
    )


@router.post("/v1/artifacts/{artifact_id}/refresh-url")
async def refresh_artifact_url(
    artifact_id: str,
    expires_in_seconds: int = Query(default=900, ge=60, le=86400),
    artifact_repository=Depends(get_artifact_repository),
    media_storage=Depends(get_media_storage),
):
    if artifact_repository is None:
        raise HTTPException(status_code=503, detail="Artifact repository is not enabled")
    artifact = artifact_repository.get(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    if _resolve_artifact_status(artifact) != "ready":
        raise HTTPException(status_code=409, detail="Artifact is not ready for preview")
    return {
        "artifact_id": artifact.artifact_id,
        "artifact_type": artifact.artifact_type,
        "object_uri": artifact.object_uri,
        "url": media_storage.create_presigned_url(artifact.object_uri, expires_in_seconds=expires_in_seconds),
        "expires_in_seconds": expires_in_seconds,
    }


@router.get("/v1/artifacts/{artifact_id}/preview-url")
async def get_artifact_preview_url(
    artifact_id: str,
    expires_in_seconds: int = Query(default=900, ge=60, le=86400),
    artifact_repository=Depends(get_artifact_repository),
    media_storage=Depends(get_media_storage),
):
    if artifact_repository is None:
        raise HTTPException(status_code=503, detail="Artifact repository is not enabled")
    artifact = artifact_repository.get(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    if _resolve_artifact_status(artifact) != "ready":
        raise HTTPException(status_code=409, detail="Artifact is not ready for preview")
    return {
        "artifact_id": artifact.artifact_id,
        "artifact_type": artifact.artifact_type,
        "object_uri": artifact.object_uri,
        "url": media_storage.create_presigned_url(artifact.object_uri, expires_in_seconds=expires_in_seconds),
        "expires_in_seconds": expires_in_seconds,
    }


@router.get("/v1/artifacts/{artifact_id}/download")
async def download_artifact_by_id(
    artifact_id: str,
    artifact_repository=Depends(get_artifact_repository),
    media_storage=Depends(get_media_storage),
):
    if artifact_repository is None:
        raise HTTPException(status_code=503, detail="Artifact repository is not enabled")
    artifact = artifact_repository.get(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    if _resolve_artifact_status(artifact) != "ready":
        raise HTTPException(status_code=409, detail="Artifact is not ready for download")
    return await _serve_artifact_uri(uri=artifact.object_uri, media_storage=media_storage)
