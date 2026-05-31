from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict

from application.services.phase8_vectors import (
    AUDIT_STYLE_MEMORY,
    TRANSLATION_MEMORY,
    Phase8VectorMigrationService,
    build_embedding_provider,
)
from infrastructure.persistence.sqlite_repositories import SQLiteVectorRepository
from infrastructure.vector.qdrant_repository import QdrantVectorRepository


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 8 Qdrant vector backfill/parity drill.")
    parser.add_argument("--source-sqlite", default=os.getenv("PHASE8_SOURCE_SQLITE", "data/jobs.db"))
    parser.add_argument("--namespace", action="append", dest="namespaces")
    parser.add_argument("--qdrant-url", default=os.getenv("QDRANT_URL", ""))
    parser.add_argument("--qdrant-api-key", default=os.getenv("QDRANT_API_KEY", ""))
    parser.add_argument("--collection-prefix", default=os.getenv("QDRANT_COLLECTION_PREFIX", ""))
    parser.add_argument("--embedding-provider", default=os.getenv("VECTOR_EMBEDDING_PROVIDER", "bge"))
    parser.add_argument("--embedding-dimension", type=int, default=int(os.getenv("VECTOR_EMBEDDING_DIMENSION", "1024")))
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    namespaces = args.namespaces or [TRANSLATION_MEMORY, AUDIT_STYLE_MEMORY]
    source = SQLiteVectorRepository(args.source_sqlite)
    embedding_provider = build_embedding_provider(
        args.embedding_provider,
        dimension=args.embedding_dimension,
    )
    if args.dry_run:
        target = source
    else:
        target = QdrantVectorRepository(
            url=args.qdrant_url,
            api_key=args.qdrant_api_key,
            collection_prefix=args.collection_prefix,
            embedding_provider=embedding_provider,
        )

    service = Phase8VectorMigrationService(
        source_repository=source,
        target_repository=target,
        embedding_provider=embedding_provider,
        batch_size=args.batch_size,
    )
    reports = []
    for namespace in namespaces:
        report = service.backfill_namespace(namespace, dry_run=args.dry_run)
        report_payload = asdict(report)
        report_payload["parity_ok"] = report.parity_ok
        reports.append(report_payload)
    print(
        json.dumps(
            {
                "dry_run": args.dry_run,
                "embedding_provider": args.embedding_provider,
                "embedding_dimension": embedding_provider.dimension,
                "reports": reports,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if all(report["parity_ok"] for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
