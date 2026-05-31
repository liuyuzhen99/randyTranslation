from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol
from urllib.error import URLError
from urllib.request import Request, urlopen

from sqlalchemy import text

from infrastructure.persistence.sqlalchemy_repositories import SQLAlchemySessionFactory


class QueueProbe(Protocol):
    def collect_depths(self) -> dict[str, int]:
        ...


@dataclass(frozen=True)
class HealthCheckResult:
    status: str
    checks: dict[str, dict]


class Phase7HealthService:
    def __init__(
        self,
        *,
        session_factory: SQLAlchemySessionFactory | None,
        media_storage,
        queue_probe: QueueProbe | None = None,
        qdrant_url: str = "",
        qdrant_api_key: str = "",
    ) -> None:
        self.session_factory = session_factory
        self.media_storage = media_storage
        self.queue_probe = queue_probe
        self.qdrant_url = qdrant_url
        self.qdrant_api_key = qdrant_api_key

    def liveness(self) -> HealthCheckResult:
        return HealthCheckResult(status="ok", checks={"api": {"status": "ok"}})

    def readiness(self) -> HealthCheckResult:
        checks = {
            "db": self._check_db(),
            "rabbitmq": self._check_rabbitmq(),
            "oss": self._check_oss(),
            "qdrant": self._check_qdrant(),
        }
        required_statuses = [
            checks["db"]["status"],
            checks["rabbitmq"]["status"],
            checks["oss"]["status"],
        ]
        status = "ok" if all(item == "ok" for item in required_statuses) else "degraded"
        return HealthCheckResult(status=status, checks=checks)

    def _check_db(self) -> dict:
        if self.session_factory is None:
            return {"status": "skipped", "reason": "DATABASE_URL is not configured"}
        try:
            with self.session_factory.session_scope() as session:
                session.execute(text("select 1"))
            return {"status": "ok"}
        except Exception as exc:
            return {"status": "failed", "reason": str(exc)}

    def _check_rabbitmq(self) -> dict:
        if self.queue_probe is None:
            return {"status": "skipped", "reason": "RABBITMQ_URL is not configured"}
        try:
            return {
                "status": "ok",
                "queue_depth": self.queue_probe.collect_depths(),
            }
        except Exception as exc:
            return {"status": "failed", "reason": str(exc)}

    def _check_oss(self) -> dict:
        provider = getattr(self.media_storage, "storage_provider", "unknown")
        if provider == "local-oss":
            output_root = getattr(self.media_storage, "output_root", "")
            temp_root = getattr(self.media_storage, "temp_root", "")
            try:
                if output_root:
                    os.makedirs(output_root, exist_ok=True)
                if temp_root:
                    os.makedirs(temp_root, exist_ok=True)
                return {
                    "status": "ok",
                    "provider": provider,
                    "bucket": getattr(self.media_storage, "bucket", ""),
                }
            except Exception as exc:
                return {"status": "failed", "provider": provider, "reason": str(exc)}
        return {
            "status": self._check_remote_oss_status(),
            "provider": provider,
            "bucket": getattr(self.media_storage, "bucket", ""),
            "mode": "configuration",
        }

    def _check_remote_oss_status(self) -> str:
        client = getattr(self.media_storage, "client", None)
        bucket = getattr(self.media_storage, "bucket", "")
        if client is None or not bucket:
            return "ok"
        try:
            if hasattr(client, "head_bucket"):
                client.head_bucket(Bucket=bucket)
            elif hasattr(client, "bucket_exists"):
                client.bucket_exists(bucket)
            return "ok"
        except Exception:
            return "failed"

    def _check_qdrant(self) -> dict:
        if not self.qdrant_url:
            return {"status": "skipped", "reason": "QDRANT_URL is not configured"}
        request = Request(self.qdrant_url.rstrip("/") + "/readyz")
        if self.qdrant_api_key:
            request.add_header("api-key", self.qdrant_api_key)
        try:
            with urlopen(request, timeout=2) as response:
                return {"status": "ok" if response.status < 500 else "failed"}
        except (OSError, URLError) as exc:
            return {"status": "failed", "reason": str(exc)}
