from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from application.services.phase8_vectors import EmbeddingProvider
from domain.entities import VectorRecord
from domain.repositories import VectorRepository


class QdrantVectorRepository(VectorRepository):
    def __init__(
        self,
        *,
        url: str,
        api_key: str = "",
        embedding_provider: EmbeddingProvider,
        collection_prefix: str = "",
        distance: str = "Cosine",
        client: Any = None,
    ) -> None:
        if not url and client is None:
            raise RuntimeError("QDRANT_URL is required for QdrantVectorRepository.")
        self.embedding_provider = embedding_provider
        self.collection_prefix = collection_prefix.strip()
        self.distance = distance
        self.client = client or self._build_client(url=url, api_key=api_key)

    def upsert(self, record: VectorRecord) -> None:
        collection = self._collection_name(record.namespace)
        self._ensure_collection(collection)
        vector = record.embedding or self.embedding_provider.embed(record.text)
        point = self._point_struct(
            point_id=record.vector_id,
            vector=vector,
            payload={
                **record.metadata,
                "vector_id": record.vector_id,
                "namespace": record.namespace,
                "text": record.text,
            },
        )
        self.client.upsert(collection_name=collection, points=[point])

    def list_by_namespace(self, namespace: str, limit: int = 1000, offset: int = 0) -> list[VectorRecord]:
        collection = self._collection_name(namespace)
        if not self._collection_exists(collection):
            return []
        records: list[VectorRecord] = []
        next_offset = None
        seen = 0
        while len(records) < limit:
            batch_limit = min(256, limit - len(records))
            points, next_offset = self.client.scroll(
                collection_name=collection,
                limit=batch_limit,
                offset=next_offset,
                with_payload=True,
                with_vectors=True,
            )
            if seen + len(points) <= offset:
                seen += len(points)
            else:
                for point in points:
                    if seen >= offset:
                        records.append(self._record_from_point(namespace, point))
                    seen += 1
                    if len(records) >= limit:
                        break
            if next_offset is None:
                break
        return records

    def count_by_namespace(self, namespace: str) -> int:
        collection = self._collection_name(namespace)
        if not self._collection_exists(collection):
            return 0
        result = self.client.count(collection_name=collection, exact=True)
        return int(getattr(result, "count", result.get("count", 0) if isinstance(result, dict) else 0))

    def search(self, namespace: str, text: str, limit: int = 5) -> list[VectorRecord]:
        collection = self._collection_name(namespace)
        if not self._collection_exists(collection):
            return []
        vector = self.embedding_provider.embed(text)
        if hasattr(self.client, "query_points"):
            response = self.client.query_points(
                collection_name=collection,
                query=vector,
                limit=limit,
                with_payload=True,
            )
            points = getattr(response, "points", response)
        else:
            points = self.client.search(
                collection_name=collection,
                query_vector=vector,
                limit=limit,
                with_payload=True,
            )
        return [self._record_from_point(namespace, point) for point in points]

    def _ensure_collection(self, collection: str) -> None:
        if self._collection_exists(collection):
            return
        models = self._models()
        if models is None:
            self.client.create_collection(
                collection_name=collection,
                vectors_config={
                    "size": self.embedding_provider.dimension,
                    "distance": self.distance,
                },
            )
            return
        distance = getattr(models.Distance, self.distance.upper(), models.Distance.COSINE)
        self.client.create_collection(
            collection_name=collection,
            vectors_config=models.VectorParams(
                size=self.embedding_provider.dimension,
                distance=distance,
            ),
        )

    def _collection_exists(self, collection: str) -> bool:
        try:
            if hasattr(self.client, "collection_exists"):
                return bool(self.client.collection_exists(collection))
            self.client.get_collection(collection)
            return True
        except Exception:
            return False

    def _collection_name(self, namespace: str) -> str:
        if self.collection_prefix:
            return f"{self.collection_prefix}_{namespace}"
        return namespace

    @staticmethod
    def _build_client(*, url: str, api_key: str):
        try:
            from qdrant_client import QdrantClient
        except ImportError:
            return _QdrantHttpClient(url=url, api_key=api_key)
        return QdrantClient(url=url, api_key=api_key or None)

    @staticmethod
    def _models():
        try:
            from qdrant_client import models
        except ImportError:
            return None
        return models

    def _point_struct(self, *, point_id: str, vector: list[float], payload: dict):
        if isinstance(self.client, _QdrantHttpClient):
            return {"id": point_id, "vector": vector, "payload": payload}
        models = self._models()
        if models is None:
            return {"id": point_id, "vector": vector, "payload": payload}
        return models.PointStruct(id=point_id, vector=vector, payload=payload)

    @staticmethod
    def _record_from_point(namespace: str, point) -> VectorRecord:
        if isinstance(point, dict):
            payload = point.get("payload") or {}
            point_id = str(point.get("id") or payload.get("vector_id", ""))
            vector = point.get("vector")
            score = point.get("score")
        else:
            payload = getattr(point, "payload", None) or {}
            point_id = str(getattr(point, "id", payload.get("vector_id", "")))
            vector = getattr(point, "vector", None)
            score = getattr(point, "score", None)
        return VectorRecord(
            vector_id=str(payload.get("vector_id") or point_id),
            namespace=str(payload.get("namespace") or namespace),
            text=str(payload.get("text") or ""),
            metadata={key: value for key, value in payload.items() if key not in {"vector_id", "namespace", "text"}},
            embedding=vector if isinstance(vector, list) else None,
            score=score,
        )


class _QdrantHttpClient:
    def __init__(self, *, url: str, api_key: str = "") -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key

    def collection_exists(self, collection_name: str) -> bool:
        try:
            self._request("GET", f"/collections/{collection_name}")
            return True
        except HTTPError as exc:
            if exc.code == 404:
                return False
            raise

    def create_collection(self, *, collection_name: str, vectors_config) -> None:
        if isinstance(vectors_config, dict):
            vectors = vectors_config
        else:
            vectors = {
                "size": getattr(vectors_config, "size"),
                "distance": getattr(vectors_config, "distance"),
            }
        self._request("PUT", f"/collections/{collection_name}", {"vectors": vectors})

    def upsert(self, *, collection_name: str, points: list[dict]) -> None:
        self._request("PUT", f"/collections/{collection_name}/points?wait=true", {"points": points})

    def count(self, *, collection_name: str, exact: bool = True):
        payload = self._request(
            "POST",
            f"/collections/{collection_name}/points/count",
            {"exact": exact},
        )
        return {"count": int(payload.get("result", {}).get("count", 0))}

    def scroll(
        self,
        *,
        collection_name: str,
        limit: int,
        offset=None,
        with_payload: bool = True,
        with_vectors: bool = True,
    ):
        body = {
            "limit": limit,
            "with_payload": with_payload,
            "with_vector": with_vectors,
        }
        if offset is not None:
            body["offset"] = offset
        payload = self._request("POST", f"/collections/{collection_name}/points/scroll", body)
        result = payload.get("result", {})
        return result.get("points", []), result.get("next_page_offset")

    def search(
        self,
        *,
        collection_name: str,
        query_vector: list[float],
        limit: int,
        with_payload: bool = True,
    ) -> list[dict]:
        payload = self._request(
            "POST",
            f"/collections/{collection_name}/points/search",
            {
                "vector": query_vector,
                "limit": limit,
                "with_payload": with_payload,
            },
        )
        return payload.get("result", [])

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["api-key"] = self.api_key
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(f"{self.url}{path}", data=data, headers=headers, method=method)
        with urlopen(request, timeout=10) as response:
            raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else {}
