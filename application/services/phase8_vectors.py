from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Protocol

from domain.entities import VectorRecord
from domain.repositories import VectorRepository

TRANSLATION_MEMORY = "translation_memory"
AUDIT_STYLE_MEMORY = "user_taste_v1"
PHASE8_COLLECTIONS = (TRANSLATION_MEMORY, AUDIT_STYLE_MEMORY)


class EmbeddingProvider(Protocol):
    dimension: int

    def embed(self, text: str) -> list[float]:
        ...


class HashingEmbeddingProvider:
    """Deterministic local embedding for repeatable migration/parity tests.

    NOT suitable for production semantic retrieval — use OpenAIEmbeddingProvider instead.
    """

    def __init__(self, dimension: int = 384) -> None:
        if dimension < 8:
            raise ValueError("Embedding dimension must be at least 8.")
        self.dimension = dimension

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        tokens = [token for token in _tokenize(text) if token]
        if not tokens:
            tokens = [text.strip() or "empty"]
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


@dataclass(frozen=True)
class VectorBackfillReport:
    namespace: str
    source_count: int
    upserted: int
    skipped: int
    mismatches: list[dict] = field(default_factory=list)

    @property
    def parity_ok(self) -> bool:
        return not self.mismatches and self.source_count == self.upserted + self.skipped


class Phase8VectorMigrationService:
    def __init__(
        self,
        *,
        source_repository: VectorRepository,
        target_repository: VectorRepository,
        embedding_provider: EmbeddingProvider,
        batch_size: int = 100,
    ) -> None:
        self.source_repository = source_repository
        self.target_repository = target_repository
        self.embedding_provider = embedding_provider
        self.batch_size = batch_size

    def backfill_namespace(self, namespace: str, dry_run: bool = False) -> VectorBackfillReport:
        source_count = self.source_repository.count_by_namespace(namespace)
        upserted = 0
        skipped = 0
        mismatches: list[dict] = []
        offset = 0

        while offset < source_count:
            batch = self.source_repository.list_by_namespace(
                namespace,
                limit=self.batch_size,
                offset=offset,
            )
            if not batch:
                break
            for record in batch:
                migrated = self._prepare_record(record)
                if not migrated.text.strip():
                    skipped += 1
                    continue
                if not dry_run:
                    self.target_repository.upsert(migrated)
                    target_matches = self.target_repository.search(
                        namespace=migrated.namespace,
                        text=migrated.text,
                        limit=5,
                    )
                    if migrated.vector_id not in {item.vector_id for item in target_matches}:
                        mismatches.append(
                            {
                                "vector_id": migrated.vector_id,
                                "reason": "target_search_miss",
                            }
                        )
                upserted += 1
            offset += len(batch)

        return VectorBackfillReport(
            namespace=namespace,
            source_count=source_count,
            upserted=upserted,
            skipped=skipped,
            mismatches=mismatches,
        )

    def _prepare_record(self, record: VectorRecord) -> VectorRecord:
        metadata = {
            **record.metadata,
            "phase8_source_namespace": record.namespace,
            "phase8_deterministic_id": deterministic_vector_id(record.namespace, record.vector_id),
        }
        return VectorRecord(
            vector_id=deterministic_vector_id(record.namespace, record.vector_id),
            namespace=record.namespace,
            text=record.text,
            metadata=metadata,
            embedding=record.embedding or self.embedding_provider.embed(record.text),
        )


@dataclass(frozen=True)
class RetrievalQualityCase:
    namespace: str
    query: str
    expected_ids: tuple[str, ...]
    limit: int = 5


@dataclass(frozen=True)
class RetrievalQualityReport:
    total_cases: int
    passed_cases: int
    failures: list[dict]

    @property
    def passed(self) -> bool:
        return self.total_cases == self.passed_cases


class Phase8RetrievalQualityEvaluator:
    def __init__(self, repository: VectorRepository) -> None:
        self.repository = repository

    def evaluate(self, cases: list[RetrievalQualityCase]) -> RetrievalQualityReport:
        failures: list[dict] = []
        for case in cases:
            results = self.repository.search(case.namespace, case.query, case.limit)
            result_ids = [item.vector_id for item in results]
            missing = [item for item in case.expected_ids if item not in result_ids]
            if missing:
                failures.append(
                    {
                        "namespace": case.namespace,
                        "query": case.query,
                        "expected_ids": list(case.expected_ids),
                        "result_ids": result_ids,
                        "missing": missing,
                    }
                )
        return RetrievalQualityReport(
            total_cases=len(cases),
            passed_cases=len(cases) - len(failures),
            failures=failures,
        )


def deterministic_vector_id(namespace: str, source_id: str) -> str:
    digest = hashlib.sha256(f"{namespace}:{source_id}".encode("utf-8")).hexdigest()
    raw = digest[:32]
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


class OpenAIEmbeddingProvider:
    """Production semantic embedding using OpenAI text-embedding-3-small (1536-dim)."""

    MODEL = "text-embedding-3-small"
    dimension = 1536

    def __init__(self, api_key: str, model: str = MODEL) -> None:
        import openai  # deferred to avoid import cost when using HashingEmbeddingProvider
        self._client = openai.OpenAI(api_key=api_key)
        self._model = model
        self.dimension = 1536

    def embed(self, text: str) -> list[float]:
        response = self._client.embeddings.create(
            model=self._model,
            input=text.strip() or "empty",
        )
        return response.data[0].embedding


class BGEEmbeddingProvider:
    """Local open-source embedding using BAAI/bge-m3 (1024-dim, MIT license).

    Bilingual (ZH+EN) SOTA — suited for translation memory and music taste retrieval.
    Imports are deferred to avoid startup cost when vector retrieval is disabled.
    """

    MODEL = "BAAI/bge-m3"
    dimension = 1024

    def __init__(self, model: str = MODEL) -> None:
        import torch
        from transformers import AutoModel, AutoTokenizer

        self._torch = torch
        self._tokenizer = AutoTokenizer.from_pretrained(model)
        self._model = AutoModel.from_pretrained(model)
        self._model.eval()

    def embed(self, text: str) -> list[float]:
        inputs = self._tokenizer(
            text.strip() or "empty",
            padding=True,
            truncation=True,
            max_length=8192,
            return_tensors="pt",
        )
        with self._torch.no_grad():
            outputs = self._model(**inputs)
        token_embeddings = outputs.last_hidden_state
        attention_mask = inputs["attention_mask"].unsqueeze(-1).expand(token_embeddings.size()).float()
        summed = (token_embeddings * attention_mask).sum(dim=1)
        counts = attention_mask.sum(dim=1).clamp(min=1e-9)
        embedding = summed / counts
        embedding = self._torch.nn.functional.normalize(embedding, p=2, dim=1)
        return embedding[0].tolist()


def build_embedding_provider(
    provider: str = "bge",
    *,
    dimension: int = 1024,
) -> EmbeddingProvider:
    name = (provider or "bge").strip().lower()
    if name in {"bge", "bge-m3", "baai/bge-m3"}:
        return BGEEmbeddingProvider()
    if name in {"hash", "hashing", "fallback"}:
        return HashingEmbeddingProvider(dimension)
    raise ValueError("Invalid embedding provider. Expected one of: bge, hashing.")


def _tokenize(text: str) -> list[str]:
    normalized = []
    current = []
    for char in text.lower():
        if char.isalnum():
            current.append(char)
        elif current:
            normalized.append("".join(current))
            current = []
    if current:
        normalized.append("".join(current))
    return normalized
