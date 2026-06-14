import unittest
from tempfile import TemporaryDirectory

from scripts.vector_retrieval_quality import _load_cases
from application.services.vector_migration import (
    AUDIT_STYLE_MEMORY,
    TRANSLATION_MEMORY,
    HashingEmbeddingProvider,
    RetrievalQualityEvaluator,
    VectorMigrationService,
    RetrievalQualityCase,
    deterministic_vector_id,
)
from domain.entities import VectorRecord
from domain.repositories import VectorRepository
from infrastructure.persistence.sqlite_repositories import SQLiteVectorRepository
from infrastructure.vector.qdrant_repository import QdrantVectorRepository


class InMemoryVectorRepository(VectorRepository):
    def __init__(self) -> None:
        self.records: dict[str, VectorRecord] = {}

    def upsert(self, record: VectorRecord) -> None:
        self.records[record.vector_id] = record

    def list_by_namespace(self, namespace: str, limit: int = 1000, offset: int = 0) -> list[VectorRecord]:
        records = [
            record
            for record in sorted(self.records.values(), key=lambda item: item.vector_id)
            if record.namespace == namespace
        ]
        return records[offset : offset + limit]

    def count_by_namespace(self, namespace: str) -> int:
        return len([record for record in self.records.values() if record.namespace == namespace])

    def search(self, namespace: str, text: str, limit: int = 5) -> list[VectorRecord]:
        tokens = {token for token in text.lower().split() if token}
        scored = []
        for record in self.records.values():
            if record.namespace != namespace:
                continue
            record_tokens = {token for token in record.text.lower().split() if token}
            score = len(tokens & record_tokens)
            if score:
                scored.append((score, record))
        return [record for _, record in sorted(scored, key=lambda item: (-item[0], item[1].vector_id))[:limit]]


class QdrantVectorMigrationTests(unittest.TestCase):
    def test_deterministic_vector_id_uses_qdrant_uuid_shape(self):
        vector_id = deterministic_vector_id(TRANSLATION_MEMORY, "legacy-source-1")

        self.assertRegex(
            vector_id,
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        )
        self.assertEqual(vector_id, deterministic_vector_id(TRANSLATION_MEMORY, "legacy-source-1"))

    def test_hashing_embedding_is_deterministic_and_normalized(self):
        provider = HashingEmbeddingProvider(dimension=16)

        first = provider.embed("sample lyrics with cadence")
        second = provider.embed("sample lyrics with cadence")

        self.assertEqual(first, second)
        self.assertEqual(len(first), 16)
        self.assertAlmostEqual(sum(value * value for value in first), 1.0)

    def test_sqlite_vector_repository_lists_namespaces_with_json_metadata(self):
        with TemporaryDirectory() as temp_root:
            repository = SQLiteVectorRepository(f"{temp_root}/vectors.db")
            repository.upsert(
                VectorRecord(
                    vector_id="legacy-1",
                    namespace=TRANSLATION_MEMORY,
                    text="cold verse warm translation",
                    metadata={"artist": "A.M.", "start_line": 3},
                )
            )
            repository.upsert(
                VectorRecord(
                    vector_id="legacy-2",
                    namespace=AUDIT_STYLE_MEMORY,
                    text="dense flow bright energy",
                    metadata={"artist": "B"},
                )
            )

            records = repository.list_by_namespace(TRANSLATION_MEMORY)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].metadata["artist"], "A.M.")
        self.assertEqual(records[0].metadata["start_line"], 3)

    def test_backfill_uses_deterministic_ids_and_is_idempotent(self):
        source = InMemoryVectorRepository()
        target = InMemoryVectorRepository()
        source.upsert(
            VectorRecord(
                vector_id="legacy-chunk-1",
                namespace=TRANSLATION_MEMORY,
                text="punchline cadence translation memory",
                metadata={"artist": "Test Artist"},
            )
        )
        service = VectorMigrationService(
            source_repository=source,
            target_repository=target,
            embedding_provider=HashingEmbeddingProvider(dimension=32),
        )

        first_report = service.backfill_namespace(TRANSLATION_MEMORY)
        second_report = service.backfill_namespace(TRANSLATION_MEMORY)
        expected_id = deterministic_vector_id(TRANSLATION_MEMORY, "legacy-chunk-1")

        self.assertTrue(first_report.parity_ok)
        self.assertTrue(second_report.parity_ok)
        self.assertEqual(first_report.upserted, 1)
        self.assertEqual(target.count_by_namespace(TRANSLATION_MEMORY), 1)
        self.assertIn(expected_id, target.records)
        self.assertEqual(target.records[expected_id].metadata["vector_source_namespace"], TRANSLATION_MEMORY)

    def test_retrieval_quality_evaluator_reports_missing_expected_ids(self):
        repository = InMemoryVectorRepository()
        repository.upsert(
            VectorRecord(
                vector_id="hit-1",
                namespace=AUDIT_STYLE_MEMORY,
                text="gritty bass high energy",
            )
        )
        evaluator = RetrievalQualityEvaluator(repository)

        report = evaluator.evaluate(
            [
                RetrievalQualityCase(
                    namespace=AUDIT_STYLE_MEMORY,
                    query="gritty energy",
                    expected_ids=("hit-1",),
                ),
                RetrievalQualityCase(
                    namespace=AUDIT_STYLE_MEMORY,
                    query="smooth chorus",
                    expected_ids=("missing-1",),
                ),
            ]
        )

        self.assertFalse(report.passed)
        self.assertEqual(report.total_cases, 2)
        self.assertEqual(report.passed_cases, 1)
        self.assertEqual(report.failures[0]["missing"], ["missing-1"])

    def test_qdrant_repository_creates_collection_upserts_and_searches_with_client_contract(self):
        client = FakeQdrantClient()
        repository = QdrantVectorRepository(
            url="",
            client=client,
            collection_prefix="vector_test",
            embedding_provider=HashingEmbeddingProvider(dimension=16),
        )
        source_id = deterministic_vector_id(TRANSLATION_MEMORY, "legacy-qdrant-1")

        repository.upsert(
            VectorRecord(
                vector_id=source_id,
                namespace=TRANSLATION_MEMORY,
                text="gritty cadence translation memory",
                metadata={"artist": "Contract"},
            )
        )
        results = repository.search(TRANSLATION_MEMORY, "cadence translation", limit=3)
        listed = repository.list_by_namespace(TRANSLATION_MEMORY)

        self.assertEqual(client.created_collections["vector_test_translation_memory"]["size"], 16)
        self.assertEqual(repository.count_by_namespace(TRANSLATION_MEMORY), 1)
        self.assertEqual(results[0].vector_id, source_id)
        self.assertEqual(listed[0].metadata["artist"], "Contract")

    def test_qdrant_repository_rejects_existing_collection_dimension_mismatch(self):
        client = FakeQdrantClient()
        client.created_collections["vector_test_translation_memory"] = {"size": 384, "distance": "Cosine"}
        repository = QdrantVectorRepository(
            url="",
            client=client,
            collection_prefix="vector_test",
            embedding_provider=HashingEmbeddingProvider(dimension=1024),
        )

        with self.assertRaisesRegex(RuntimeError, "vector dimension 384"):
            repository.upsert(
                VectorRecord(
                    vector_id="dimension-mismatch",
                    namespace=TRANSLATION_MEMORY,
                    text="new bge vector",
                )
            )

    def test_retrieval_quality_case_loader_reads_json_contract(self):
        with TemporaryDirectory() as temp_root:
            cases_path = f"{temp_root}/cases.json"
            with open(cases_path, "w", encoding="utf-8") as file_obj:
                file_obj.write(
                    '[{"namespace":"translation_memory","query":"cadence",'
                    '"expected_ids":["id-1"],"limit":2}]'
                )

            cases = _load_cases(cases_path)

        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].namespace, TRANSLATION_MEMORY)
        self.assertEqual(cases[0].expected_ids, ("id-1",))
        self.assertEqual(cases[0].limit, 2)


class FakeQdrantClient:
    def __init__(self) -> None:
        self.created_collections: dict[str, dict] = {}
        self.points: dict[str, list[dict]] = {}

    def collection_exists(self, collection_name: str) -> bool:
        return collection_name in self.created_collections

    def get_collection(self, collection_name: str) -> dict:
        return {
            "result": {
                "config": {
                    "params": {
                        "vectors": self.created_collections[collection_name],
                    }
                }
            }
        }

    def create_collection(self, *, collection_name: str, vectors_config) -> None:
        if isinstance(vectors_config, dict):
            self.created_collections[collection_name] = vectors_config
        else:
            self.created_collections[collection_name] = {
                "size": vectors_config.size,
                "distance": vectors_config.distance,
            }

    def upsert(self, *, collection_name: str, points: list[dict]) -> None:
        self.points.setdefault(collection_name, [])
        existing = {self._point_id(point): point for point in self.points[collection_name]}
        for point in points:
            existing[self._point_id(point)] = point
        self.points[collection_name] = list(existing.values())

    @staticmethod
    def _point_id(point) -> str:
        if isinstance(point, dict):
            return point["id"]
        return point.id

    def count(self, *, collection_name: str, exact: bool = True):
        return {"count": len(self.points.get(collection_name, []))}

    def scroll(
        self,
        *,
        collection_name: str,
        limit: int,
        offset=None,
        with_payload: bool = True,
        with_vectors: bool = True,
    ):
        start = int(offset or 0)
        batch = self.points.get(collection_name, [])[start : start + limit]
        next_offset = start + len(batch) if start + len(batch) < len(self.points.get(collection_name, [])) else None
        return batch, next_offset

    def search(
        self,
        *,
        collection_name: str,
        query_vector: list[float],
        limit: int,
        with_payload: bool = True,
    ) -> list[dict]:
        return self.points.get(collection_name, [])[:limit]


if __name__ == "__main__":
    unittest.main()
