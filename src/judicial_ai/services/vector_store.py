from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from ..config import get_settings
from ..errors import sanitize_exception_summary
from ..models import ChunkMeta, IndexVersion, KnowledgeBase
from ..text_utils import sparse_vector

logger = logging.getLogger("judicial_ai.vector")


class VectorStoreUnavailable(RuntimeError):
    pass


@dataclass
class VectorHit:
    chunk_id: str
    score: float
    recall_sources: list[str] = field(default_factory=list)


class VectorStore:
    backend = "unknown"

    def index_chunks(
        self,
        *,
        kb: KnowledgeBase,
        index_version: IndexVersion,
        chunks: list[ChunkMeta],
        embeddings: list[list[float]],
    ) -> dict[str, Any]:
        return {"backend": self.backend, "indexed": 0}

    def search(
        self,
        *,
        tenant_id: int,
        kb_ids: list[int],
        index_version_ids: list[int],
        query_text: str,
        query_embedding: list[float],
        limit: int,
        security_levels: list[str] | None = None,
    ) -> list[VectorHit]:
        return []

    def delete_index(self, index_version_id: int) -> None:
        return None

    def list_index_ids(self) -> set[int]:
        return set()

    def collection_name(self, index_version_id: int) -> str:
        return ""

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "backend": self.backend}


class SqlVectorStore(VectorStore):
    backend = "sql"

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "backend": "sql", "mode": "json-fallback"}


class MilvusVectorStore(VectorStore):
    backend = "milvus"

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.milvus_uri:
            raise VectorStoreUnavailable("VECTOR_BACKEND=milvus 但未配置 MILVUS_URI")
        try:
            from pymilvus import DataType, MilvusClient
        except ImportError as exc:
            raise VectorStoreUnavailable("缺少 pymilvus 依赖") from exc
        self._settings = settings
        self._data_type = DataType
        self._client = MilvusClient(
            uri=settings.milvus_uri,
            token=settings.milvus_token,
            timeout=settings.milvus_timeout_seconds,
        )
        self._ensured_collections: set[str] = set()

    def collection_name(self, index_version_id: int) -> str:
        return f"{self._settings.milvus_collection_prefix}_{index_version_id}"

    def _ensure_collection(self, name: str, dimension: int) -> None:
        if name in self._ensured_collections:
            return
        if self._client.has_collection(collection_name=name):
            self._ensured_collections.add(name)
            try:
                self._client.load_collection(collection_name=name)
            except Exception:  # noqa: BLE001 - load is idempotent in Milvus
                pass
            return

        schema = self._client.create_schema(auto_id=False, enable_dynamic_field=True)
        schema.add_field(
            field_name="chunk_id",
            datatype=self._data_type.VARCHAR,
            is_primary=True,
            max_length=64,
        )
        schema.add_field(field_name="tenant_id", datatype=self._data_type.INT64)
        schema.add_field(field_name="kb_id", datatype=self._data_type.INT64)
        schema.add_field(field_name="index_version_id", datatype=self._data_type.INT64)
        schema.add_field(field_name="document_id", datatype=self._data_type.INT64)
        schema.add_field(
            field_name="security_level",
            datatype=self._data_type.VARCHAR,
            max_length=32,
        )
        schema.add_field(
            field_name="embedding",
            datatype=self._data_type.FLOAT_VECTOR,
            dim=dimension,
        )
        if self._settings.milvus_sparse_enabled:
            schema.add_field(
                field_name="sparse",
                datatype=self._data_type.SPARSE_FLOAT_VECTOR,
            )
        index_params = self._client.prepare_index_params()
        index_params.add_index(
            field_name="embedding",
            index_type="AUTOINDEX",
            metric_type="COSINE",
        )
        if self._settings.milvus_sparse_enabled:
            index_params.add_index(
                field_name="sparse",
                index_type="SPARSE_INVERTED_INDEX",
                metric_type="IP",
            )
        self._client.create_collection(
            collection_name=name,
            schema=schema,
            index_params=index_params,
            consistency_level=self._settings.milvus_consistency_level,
        )
        self._ensured_collections.add(name)

    def index_chunks(
        self,
        *,
        kb: KnowledgeBase,
        index_version: IndexVersion,
        chunks: list[ChunkMeta],
        embeddings: list[list[float]],
    ) -> dict[str, Any]:
        if len(chunks) != len(embeddings):
            raise ValueError("chunk and embedding counts differ")
        if not chunks:
            return {"backend": self.backend, "indexed": 0, "collection": ""}
        dimension = len(embeddings[0]) if embeddings else self._settings.embedding_dim
        name = self.collection_name(index_version.id)
        self._ensure_collection(name, dimension)
        rows: list[dict[str, Any]] = []
        for chunk, embedding in zip(chunks, embeddings, strict=True):
            row: dict[str, Any] = {
                "chunk_id": chunk.chunk_id,
                "tenant_id": chunk.tenant_id,
                "kb_id": chunk.kb_id,
                "index_version_id": index_version.id,
                "document_id": chunk.document_id,
                "security_level": (chunk.extra or {}).get("security_level", "INTERNAL"),
                "embedding": embedding,
            }
            if self._settings.milvus_sparse_enabled:
                row["sparse"] = sparse_vector(chunk.text)
            rows.append(row)
        batch_size = 256
        for start in range(0, len(rows), batch_size):
            self._client.upsert(collection_name=name, data=rows[start : start + batch_size])
        try:
            self._client.flush(collection_name=name)
        except Exception:  # noqa: BLE001
            pass
        return {"backend": self.backend, "indexed": len(rows), "collection": name}

    @staticmethod
    def _filter(
        tenant_id: int,
        kb_ids: list[int],
        index_version_id: int,
        security_levels: list[str] | None,
    ) -> str:
        kb_expr = ", ".join(str(int(item)) for item in kb_ids)
        parts = [
            f"tenant_id == {int(tenant_id)}",
            f"kb_id in [{kb_expr}]",
            f"index_version_id == {int(index_version_id)}",
        ]
        if security_levels:
            levels = ", ".join(f'"{level}"' for level in security_levels)
            parts.append(f"security_level in [{levels}]")
        return " and ".join(parts)

    @staticmethod
    def _parse_hits(results: Any, source: str) -> list[VectorHit]:
        hits: list[VectorHit] = []
        if not results:
            return hits
        for item in results[0]:
            if isinstance(item, dict):
                entity = item.get("entity") or {}
                chunk_id = entity.get("chunk_id") or item.get("id")
                score = item.get("distance", item.get("score", 0.0))
            else:
                entity = getattr(item, "entity", {}) or {}
                chunk_id = getattr(item, "id", None)
                score = getattr(item, "distance", 0.0)
            if chunk_id:
                hits.append(
                    VectorHit(
                        chunk_id=str(chunk_id),
                        score=float(score or 0.0),
                        recall_sources=[source],
                    )
                )
        return hits

    def search(
        self,
        *,
        tenant_id: int,
        kb_ids: list[int],
        index_version_ids: list[int],
        query_text: str,
        query_embedding: list[float],
        limit: int,
        security_levels: list[str] | None = None,
    ) -> list[VectorHit]:
        all_hits: list[VectorHit] = []
        for index_version_id in index_version_ids:
            name = self.collection_name(index_version_id)
            if not self._client.has_collection(collection_name=name):
                raise VectorStoreUnavailable(
                    f"向量索引集合不存在: index_version_id={index_version_id}"
                )
            expr = self._filter(tenant_id, kb_ids, index_version_id, security_levels)
            try:
                self._client.load_collection(collection_name=name)
            except Exception:  # noqa: BLE001
                pass
            hits: list[VectorHit] = []
            if self._settings.milvus_sparse_enabled:
                try:
                    from pymilvus import AnnSearchRequest, RRFRanker

                    requests = [
                        AnnSearchRequest(
                            data=[query_embedding],
                            anns_field="embedding",
                            param={"metric_type": "COSINE"},
                            limit=max(limit, 20),
                        ),
                        AnnSearchRequest(
                            data=[sparse_vector(query_text)],
                            anns_field="sparse",
                            param={"metric_type": "IP"},
                            limit=max(limit, 20),
                        ),
                    ]
                    results = self._client.hybrid_search(
                        collection_name=name,
                        reqs=requests,
                        ranker=RRFRanker(),
                        limit=limit,
                        filter=expr,
                        output_fields=["chunk_id"],
                    )
                    hits = self._parse_hits(results, "hybrid")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("milvus_hybrid_failed", extra={"error": str(exc)[:240]})
            if not hits:
                results = self._client.search(
                    collection_name=name,
                    data=[query_embedding],
                    anns_field="embedding",
                    search_params={"metric_type": "COSINE"},
                    filter=expr,
                    limit=limit,
                    output_fields=["chunk_id"],
                )
                hits = self._parse_hits(results, "dense")
            all_hits.extend(hits)
        all_hits.sort(key=lambda item: item.score, reverse=True)
        return all_hits[:limit]

    def delete_index(self, index_version_id: int) -> None:
        name = self.collection_name(index_version_id)
        if not self._client.has_collection(collection_name=name):
            return
        try:
            self._client.delete(
                collection_name=name,
                filter=f"index_version_id == {int(index_version_id)}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("milvus_delete_failed", extra={"error": str(exc)[:240]})
            raise

    def list_index_ids(self) -> set[int]:
        prefix = f"{self._settings.milvus_collection_prefix}_"
        result: set[int] = set()
        for name in self._client.list_collections():
            if not name.startswith(prefix):
                continue
            try:
                result.add(int(name[len(prefix) :]))
            except ValueError:
                continue
        return result

    def health(self) -> dict[str, Any]:
        try:
            collections = self._client.list_collections()
            return {
                "status": "ok",
                "backend": "milvus",
                "collections": len(collections),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "error",
                "backend": "milvus",
                "error": sanitize_exception_summary(exc),
            }


@lru_cache
def get_vector_store() -> VectorStore:
    settings = get_settings()
    if settings.vector_backend == "milvus":
        return MilvusVectorStore()
    return SqlVectorStore()
