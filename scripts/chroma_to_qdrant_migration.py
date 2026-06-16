from __future__ import annotations

import argparse
import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from application.services.vector_migration import (
    build_embedding_provider,
    deterministic_vector_id,
)
from domain.entities import VectorRecord
from domain.repositories import VectorRepository
from infrastructure.vector.qdrant_repository import QdrantVectorRepository


class ChromaVectorRepository(VectorRepository):
    def __init__(self, path: str) -> None:
        try:
            import chromadb
        except ImportError as exc:
            raise RuntimeError("chromadb is required to read legacy Chroma data.") from exc
        self.client = chromadb.PersistentClient(path=path)

    def collection_names(self) -> list[str]:
        return [collection.name for collection in self.client.list_collections()]

    def upsert(self, record: VectorRecord) -> None:
        raise NotImplementedError("ChromaVectorRepository is read-only for migration.")

    def list_by_namespace(self, namespace: str, limit: int = 1000, offset: int = 0) -> list[VectorRecord]:
        collection = self.client.get_collection(namespace)
        payload = collection.get(
            include=["documents", "metadatas", "embeddings"],
            limit=limit,
            offset=offset,
        )
        ids = payload.get("ids") or []
        documents = payload.get("documents") or []
        metadatas = payload.get("metadatas") or []
        embeddings = payload.get("embeddings")
        return [
            VectorRecord(
                vector_id=str(vector_id),
                namespace=namespace,
                text=str(documents[index] or ""),
                metadata=metadatas[index] or {},
                embedding=_embedding_at(embeddings, index),
            )
            for index, vector_id in enumerate(ids)
        ]

    def count_by_namespace(self, namespace: str) -> int:
        return int(self.client.get_collection(namespace).count())

    def search(self, namespace: str, text: str, limit: int = 5) -> list[VectorRecord]:
        collection = self.client.get_collection(namespace)
        payload = collection.query(query_texts=[text], n_results=limit, include=["documents", "metadatas", "embeddings"])
        ids = (payload.get("ids") or [[]])[0]
        documents = (payload.get("documents") or [[]])[0]
        metadatas = (payload.get("metadatas") or [[]])[0]
        embeddings = (payload.get("embeddings") or [[]])[0]
        return [
            VectorRecord(
                vector_id=str(vector_id),
                namespace=namespace,
                text=str(documents[index] or ""),
                metadata=metadatas[index] or {},
                embedding=_embedding_at(embeddings, index),
            )
            for index, vector_id in enumerate(ids)
        ]


def _embedding_at(embeddings, index: int) -> list[float] | None:
    if embeddings is None:
        return None
    value = embeddings[index]
    if hasattr(value, "tolist"):
        value = value.tolist()
    return [float(item) for item in value] if value is not None else None


def _infer_dimension(source: ChromaVectorRepository, namespaces: list[str], fallback: int) -> int:
    for namespace in namespaces:
        records = source.list_by_namespace(namespace, limit=1, offset=0)
        if records and records[0].embedding:
            return len(records[0].embedding)
    return fallback


def _request_qdrant(method: str, url: str, path: str, api_key: str = "") -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["api-key"] = api_key
    request = Request(url.rstrip("/") + path, headers=headers, method=method)
    with urlopen(request, timeout=10) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def _clear_qdrant_collections(url: str, api_key: str, collection_names: list[str]) -> list[str]:
    deleted: list[str] = []
    for collection in collection_names:
        try:
            _request_qdrant("DELETE", url, f"/collections/{collection}", api_key)
            deleted.append(collection)
        except HTTPError as exc:
            if exc.code != 404:
                raise
    return deleted


def _list_qdrant_collections(url: str, api_key: str) -> list[str]:
    payload = _request_qdrant("GET", url, "/collections", api_key)
    collections = payload.get("result", {}).get("collections", [])
    return [str(collection["name"]) for collection in collections if "name" in collection]


def _target_collection_name(prefix: str, namespace: str) -> str:
    prefix = prefix.strip()
    return f"{prefix}_{namespace}" if prefix else namespace


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate legacy Chroma persistent collections into Qdrant.")
    parser.add_argument("--chroma-path", default=os.getenv("CHROMA_PATH", "data/chroma_db"))
    parser.add_argument("--namespace", action="append", dest="namespaces")
    parser.add_argument("--qdrant-url", default=os.getenv("QDRANT_URL", "http://127.0.0.1:6333"))
    parser.add_argument("--qdrant-api-key", default=os.getenv("QDRANT_API_KEY", ""))
    parser.add_argument("--collection-prefix", default=os.getenv("QDRANT_COLLECTION_PREFIX", ""))
    parser.add_argument("--embedding-provider", default=os.getenv("VECTOR_EMBEDDING_PROVIDER", "bge"))
    parser.add_argument("--embedding-dimension", type=int, default=int(os.getenv("VECTOR_EMBEDDING_DIMENSION", "1024")))
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--clear-target", action="store_true")
    parser.add_argument("--clear-all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    source = ChromaVectorRepository(args.chroma_path)
    namespaces = args.namespaces or source.collection_names()
    dimension = _infer_dimension(source, namespaces, args.embedding_dimension)
    embedding_provider = build_embedding_provider(args.embedding_provider, dimension=dimension)
    target = QdrantVectorRepository(
        url=args.qdrant_url,
        api_key=args.qdrant_api_key,
        collection_prefix=args.collection_prefix,
        embedding_provider=embedding_provider,
    )

    deleted: list[str] = []
    if args.clear_all and not args.dry_run:
        deleted = _clear_qdrant_collections(args.qdrant_url, args.qdrant_api_key, _list_qdrant_collections(args.qdrant_url, args.qdrant_api_key))
    elif args.clear_target and not args.dry_run:
        deleted = _clear_qdrant_collections(
            args.qdrant_url,
            args.qdrant_api_key,
            [_target_collection_name(args.collection_prefix, namespace) for namespace in namespaces],
        )

    reports = []
    for namespace in namespaces:
        source_count = source.count_by_namespace(namespace)
        upserted = 0
        skipped = 0
        offset = 0
        while offset < source_count:
            records = source.list_by_namespace(namespace, limit=args.batch_size, offset=offset)
            if not records:
                break
            for record in records:
                if not record.text.strip():
                    skipped += 1
                    continue
                migrated = VectorRecord(
                    vector_id=deterministic_vector_id(record.namespace, record.vector_id),
                    namespace=record.namespace,
                    text=record.text,
                    metadata={
                        **record.metadata,
                        "vector_source_namespace": record.namespace,
                        "vector_source_vector_id": record.vector_id,
                        "vector_deterministic_id": deterministic_vector_id(record.namespace, record.vector_id),
                    },
                    embedding=record.embedding or embedding_provider.embed(record.text),
                )
                if not args.dry_run:
                    target.upsert(migrated)
                upserted += 1
            offset += len(records)
        target_count = 0 if args.dry_run else target.count_by_namespace(namespace)
        reports.append(
            {
                "namespace": namespace,
                "source_count": source_count,
                "upserted": upserted,
                "skipped": skipped,
                "target_count": target_count,
                "parity_ok": args.dry_run or target_count == upserted,
            }
        )

    payload = {
        "dry_run": args.dry_run,
        "chroma_path": args.chroma_path,
        "qdrant_url": args.qdrant_url,
        "embedding_provider": args.embedding_provider,
        "embedding_dimension": dimension,
        "deleted_collections": deleted,
        "reports": reports,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if all(report["parity_ok"] for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
