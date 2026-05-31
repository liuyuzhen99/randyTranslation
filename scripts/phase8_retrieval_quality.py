from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict

from application.services.phase8_vectors import (
    Phase8RetrievalQualityEvaluator,
    RetrievalQualityCase,
    build_embedding_provider,
)
from infrastructure.persistence.sqlite_repositories import SQLiteVectorRepository
from infrastructure.vector.qdrant_repository import QdrantVectorRepository


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 8 retrieval quality baseline runner.")
    parser.add_argument("--cases", required=True, help="JSON file containing retrieval cases.")
    parser.add_argument("--backend", default=os.getenv("VECTOR_REPOSITORY_BACKEND", "qdrant"))
    parser.add_argument("--source-sqlite", default=os.getenv("PHASE8_SOURCE_SQLITE", "data/jobs.db"))
    parser.add_argument("--qdrant-url", default=os.getenv("QDRANT_URL", ""))
    parser.add_argument("--qdrant-api-key", default=os.getenv("QDRANT_API_KEY", ""))
    parser.add_argument("--collection-prefix", default=os.getenv("QDRANT_COLLECTION_PREFIX", ""))
    parser.add_argument("--embedding-provider", default=os.getenv("VECTOR_EMBEDDING_PROVIDER", "bge"))
    parser.add_argument("--embedding-dimension", type=int, default=int(os.getenv("VECTOR_EMBEDDING_DIMENSION", "1024")))
    args = parser.parse_args()

    repository = _build_repository(args)
    evaluator = Phase8RetrievalQualityEvaluator(repository)
    cases = _load_cases(args.cases)
    report = evaluator.evaluate(cases)
    payload = asdict(report)
    payload["passed"] = report.passed
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if report.passed else 1


def _build_repository(args):
    if args.backend == "sqlite":
        return SQLiteVectorRepository(args.source_sqlite)
    if args.backend != "qdrant":
        raise RuntimeError("Invalid backend. Expected one of: sqlite, qdrant.")
    return QdrantVectorRepository(
        url=args.qdrant_url,
        api_key=args.qdrant_api_key,
        collection_prefix=args.collection_prefix,
        embedding_provider=build_embedding_provider(
            args.embedding_provider,
            dimension=args.embedding_dimension,
        ),
    )


def _load_cases(path: str) -> list[RetrievalQualityCase]:
    with open(path, "r", encoding="utf-8") as file_obj:
        raw_cases = json.load(file_obj)
    return [
        RetrievalQualityCase(
            namespace=item["namespace"],
            query=item["query"],
            expected_ids=tuple(item["expected_ids"]),
            limit=int(item.get("limit", 5)),
        )
        for item in raw_cases
    ]


if __name__ == "__main__":
    raise SystemExit(main())
