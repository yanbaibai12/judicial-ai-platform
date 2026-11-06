from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import SessionLocal
from ..errors import sanitize_exception_summary
from ..ids import new_id
from ..models import ChunkMeta, GraphEntity, GraphRelation, IndexVersion, KnowledgeBase
from .graph_extract import entity_terms, extract_entities

logger = logging.getLogger("judicial_ai.graph")


class GraphStoreUnavailable(RuntimeError):
    pass


@dataclass
class GraphResult:
    entities: list[dict[str, Any]]
    paths: list[dict[str, Any]]
    degraded: bool = False
    backend: str = "sql"


class GraphStore:
    backend = "unknown"

    def index_chunks(
        self,
        *,
        kb: KnowledgeBase,
        index_version: IndexVersion,
        chunks: list[ChunkMeta],
    ) -> dict[str, Any]:
        return {"backend": self.backend, "entities": 0, "relations": 0}

    def search(
        self,
        *,
        tenant_id: int,
        kb_ids: list[int],
        index_version_ids: list[int],
        query: str,
        limit: int = 20,
        depth: int = 2,
    ) -> GraphResult:
        return GraphResult([], [], backend=self.backend)

    def delete_index(self, index_version_id: int) -> None:
        return None

    def list_index_ids(self) -> set[int]:
        return set()

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "backend": self.backend}

    def close(self) -> None:
        return None


class SqlGraphStore(GraphStore):
    backend = "sql"

    def index_chunks(
        self,
        *,
        kb: KnowledgeBase,
        index_version: IndexVersion,
        chunks: list[ChunkMeta],
    ) -> dict[str, Any]:
        db: Session = SessionLocal()
        try:
            db.query(GraphRelation).filter(
                GraphRelation.index_version_id == index_version.id
            ).delete(synchronize_session=False)
            db.query(GraphEntity).filter(
                GraphEntity.index_version_id == index_version.id
            ).delete(synchronize_session=False)
            entity_count = 0
            relation_count = 0
            for chunk in chunks:
                entities = extract_entities(chunk.text)
                if not entities:
                    continue
                records: list[GraphEntity] = []
                for entity in entities:
                    record = GraphEntity(
                        id=new_id(),
                        tenant_id=chunk.tenant_id,
                        kb_id=chunk.kb_id,
                        index_version_id=index_version.id,
                        chunk_id=chunk.chunk_id,
                        document_id=chunk.document_id,
                        name=entity.name,
                        normalized=entity.normalized,
                        entity_type=entity.entity_type,
                    )
                    db.add(record)
                    records.append(record)
                    entity_count += 1
                db.flush()
                for left, right in zip(records, records[1:], strict=False):
                    if left.id == right.id:
                        continue
                    db.add(
                        GraphRelation(
                            id=new_id(),
                            tenant_id=chunk.tenant_id,
                            index_version_id=index_version.id,
                            source_id=left.id,
                            target_id=right.id,
                            relation="CO_OCCURS",
                            chunk_id=chunk.chunk_id,
                            weight=1.0,
                        )
                    )
                    relation_count += 1
            db.commit()
            return {
                "backend": self.backend,
                "entities": entity_count,
                "relations": relation_count,
            }
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def search(
        self,
        *,
        tenant_id: int,
        kb_ids: list[int],
        index_version_ids: list[int],
        query: str,
        limit: int = 20,
        depth: int = 2,
    ) -> GraphResult:
        terms = entity_terms(query)
        if not terms:
            return GraphResult([], [], backend=self.backend)
        db: Session = SessionLocal()
        try:
            filters = [
                GraphEntity.tenant_id == tenant_id,
                GraphEntity.kb_id.in_(kb_ids),
                GraphEntity.index_version_id.in_(index_version_ids),
                or_(*[GraphEntity.normalized.contains(term) for term in terms]),
            ]
            records = db.query(GraphEntity).filter(*filters).limit(limit).all()
            if not records:
                return GraphResult([], [], backend=self.backend)
            ids = [item.id for item in records]
            relations = (
                db.query(GraphRelation)
                .filter(
                    GraphRelation.index_version_id.in_(index_version_ids),
                    or_(
                        GraphRelation.source_id.in_(ids),
                        GraphRelation.target_id.in_(ids),
                    ),
                )
                .limit(100)
                .all()
            )
            names = {item.id: item.name for item in records}
            extra_ids = {
                relation.source_id for relation in relations
            } | {relation.target_id for relation in relations}
            missing = [entity_id for entity_id in extra_ids if entity_id not in names]
            if missing:
                for item in db.query(GraphEntity).filter(GraphEntity.id.in_(missing)).all():
                    names[item.id] = item.name
            entities = [
                {
                    "id": item.id,
                    "name": item.name,
                    "type": item.entity_type,
                    "chunk_id": item.chunk_id,
                    "document_id": item.document_id,
                }
                for item in records
            ]
            paths = [
                {
                    "source": names.get(relation.source_id, ""),
                    "target": names.get(relation.target_id, ""),
                    "relation": relation.relation,
                    "chunk_id": relation.chunk_id,
                }
                for relation in relations
            ]
            return GraphResult(entities, paths, backend=self.backend)
        finally:
            db.close()

    def delete_index(self, index_version_id: int) -> None:
        db: Session = SessionLocal()
        try:
            db.query(GraphRelation).filter(
                GraphRelation.index_version_id == index_version_id
            ).delete(synchronize_session=False)
            db.query(GraphEntity).filter(
                GraphEntity.index_version_id == index_version_id
            ).delete(synchronize_session=False)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def list_index_ids(self) -> set[int]:
        db: Session = SessionLocal()
        try:
            return {
                int(row[0])
                for row in db.query(GraphEntity.index_version_id).distinct().all()
            }
        finally:
            db.close()


class Neo4jGraphStore(GraphStore):
    backend = "neo4j"

    def __init__(self) -> None:
        settings = get_settings()
        if not settings.neo4j_uri:
            raise GraphStoreUnavailable("GRAPH_BACKEND=neo4j 但未配置 NEO4J_URI")
        try:
            from neo4j import GraphDatabase
        except ImportError as exc:
            raise GraphStoreUnavailable("缺少 neo4j 依赖") from exc
        self._settings = settings
        self._driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password or ""),
        )

    def index_chunks(
        self,
        *,
        kb: KnowledgeBase,
        index_version: IndexVersion,
        chunks: list[ChunkMeta],
    ) -> dict[str, Any]:
        entity_rows: list[dict[str, Any]] = []
        relation_rows: list[dict[str, Any]] = []
        for chunk in chunks:
            entities = extract_entities(chunk.text)
            for entity in entities:
                entity_rows.append(
                    {
                        "name": entity.name,
                        "normalized": entity.normalized,
                        "type": entity.entity_type,
                        "chunk_id": chunk.chunk_id,
                        "document_id": chunk.document_id,
                    }
                )
            for left, right in zip(entities, entities[1:], strict=False):
                relation_rows.append(
                    {
                        "source": left.normalized,
                        "target": right.normalized,
                        "chunk_id": chunk.chunk_id,
                        "weight": 1.0,
                    }
                )
        def write_graph(tx: Any) -> None:
            tx.run(
                "MATCH (e:Entity {index_version_id: $index_version_id}) DETACH DELETE e",
                index_version_id=index_version.id,
            ).consume()
            if entity_rows:
                tx.run(
                    """
                    UNWIND $rows AS row
                    MERGE (e:Entity {
                        tenant_id: $tenant_id,
                        kb_id: $kb_id,
                        index_version_id: $index_version_id,
                        normalized: row.normalized
                    })
                    SET e.name = row.name,
                        e.entity_type = row.type,
                        e.chunk_id = row.chunk_id,
                        e.document_id = row.document_id
                    """,
                    rows=entity_rows,
                    tenant_id=kb.tenant_id,
                    kb_id=kb.id,
                    index_version_id=index_version.id,
                ).consume()
            if relation_rows:
                tx.run(
                    """
                    UNWIND $rows AS row
                    MATCH (a:Entity {
                        tenant_id: $tenant_id,
                        kb_id: $kb_id,
                        index_version_id: $index_version_id,
                        normalized: row.source
                    })
                    MATCH (b:Entity {
                        tenant_id: $tenant_id,
                        kb_id: $kb_id,
                        index_version_id: $index_version_id,
                        normalized: row.target
                    })
                    MERGE (a)-[r:CO_OCCURS {chunk_id: row.chunk_id}]->(b)
                    SET r.relation = 'CO_OCCURS', r.weight = row.weight
                    """,
                    rows=relation_rows,
                    tenant_id=kb.tenant_id,
                    kb_id=kb.id,
                    index_version_id=index_version.id,
                ).consume()

        with self._driver.session(database=self._settings.neo4j_database) as session:
            session.execute_write(write_graph)
        return {
            "backend": self.backend,
            "entities": len(entity_rows),
            "relations": len(relation_rows),
        }

    def search(
        self,
        *,
        tenant_id: int,
        kb_ids: list[int],
        index_version_ids: list[int],
        query: str,
        limit: int = 20,
        depth: int = 2,
    ) -> GraphResult:
        terms = entity_terms(query)
        if not terms:
            return GraphResult([], [], backend=self.backend)
        with self._driver.session(database=self._settings.neo4j_database) as session:
            result = session.run(
                """
                MATCH (e:Entity)
                WHERE e.tenant_id = $tenant_id
                  AND e.kb_id IN $kb_ids
                  AND e.index_version_id IN $index_version_ids
                  AND any(term IN $terms WHERE e.normalized CONTAINS term)
                RETURN elementId(e) AS id, e.name AS name, e.entity_type AS type,
                       e.chunk_id AS chunk_id, e.document_id AS document_id
                LIMIT $limit
                """,
                tenant_id=tenant_id,
                kb_ids=kb_ids,
                index_version_ids=index_version_ids,
                terms=terms,
                limit=limit,
            )
            entities = [dict(record) for record in result]
            ids = [record["id"] for record in entities]
            if not ids:
                return GraphResult([], [], backend=self.backend)
            path_result = session.run(
                """
                MATCH (a:Entity)-[r:CO_OCCURS]-(b:Entity)
                WHERE elementId(a) IN $ids AND elementId(b) IN $ids
                RETURN a.name AS source, b.name AS target,
                       r.relation AS relation, r.chunk_id AS chunk_id
                LIMIT 100
                """,
                ids=ids,
            )
            paths = [dict(record) for record in path_result]
        return GraphResult(entities, paths, backend=self.backend)

    def delete_index(self, index_version_id: int) -> None:
        with self._driver.session(database=self._settings.neo4j_database) as session:
            session.run(
                "MATCH (e:Entity {index_version_id: $index_version_id}) DETACH DELETE e",
                index_version_id=index_version_id,
            ).consume()

    def list_index_ids(self) -> set[int]:
        with self._driver.session(database=self._settings.neo4j_database) as session:
            result = session.run(
                "MATCH (e:Entity) RETURN DISTINCT e.index_version_id AS index_version_id"
            )
            return {
                int(record["index_version_id"])
                for record in result
                if record["index_version_id"] is not None
            }

    def health(self) -> dict[str, Any]:
        try:
            self._driver.verify_connectivity()
            return {"status": "ok", "backend": "neo4j"}
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "error",
                "backend": "neo4j",
                "error": sanitize_exception_summary(exc),
            }

    def close(self) -> None:
        self._driver.close()


@lru_cache
def get_graph_store() -> GraphStore:
    settings = get_settings()
    if settings.graph_backend == "neo4j":
        return Neo4jGraphStore()
    return SqlGraphStore()


def close_graph_store() -> None:
    if get_graph_store.cache_info().currsize == 0:
        return
    try:
        get_graph_store().close()
    finally:
        get_graph_store.cache_clear()


def graph_search_or_degraded(
    *,
    tenant_id: int,
    kb_ids: list[int],
    index_version_ids: list[int],
    query: str,
    limit: int = 20,
) -> GraphResult:
    settings = get_settings()
    if not settings.graph_enabled or not index_version_ids:
        return GraphResult([], [], degraded=True, backend=settings.graph_backend)
    try:
        return get_graph_store().search(
            tenant_id=tenant_id,
            kb_ids=kb_ids,
            index_version_ids=index_version_ids,
            query=query,
            limit=limit,
            depth=settings.graph_max_depth,
        )
    except Exception as exc:  # noqa: BLE001 - graph is optional and must not break retrieval
        logger.warning("graph_search_degraded", extra={"error": str(exc)[:240]})
        return GraphResult([], [], degraded=True, backend=settings.graph_backend)
