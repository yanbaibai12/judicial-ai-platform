from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from judicial_ai.config import get_settings
from judicial_ai.db import SessionLocal
from judicial_ai.models import ChunkMeta, Document, IndexChunkSnapshot, IndexVersion, KnowledgeBase
from judicial_ai.services import retrieval
from judicial_ai.services.vector_store import MilvusVectorStore, VectorHit


def _create_kb(client, headers):
    response = client.post(
        "/api/v1/kbs",
        json={"code": f"snapshot-{uuid.uuid4().hex[:8]}", "name": "snapshot"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


def _add_document(client, headers, kb_id, title):
    response = client.post(
        f"/api/v1/kbs/{kb_id}/documents",
        json={"title": title, "text": f"unique document content {title}"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_snapshot_rollback_and_explicit_index(client, new_tenant_app):
    headers = new_tenant_app["headers"]
    kb_id = _create_kb(client, headers)
    first = _add_document(client, headers, kb_id, "first")
    with SessionLocal() as db:
        kb = db.get(KnowledgeBase, kb_id)
        first_index = db.query(IndexVersion).filter_by(kb_id=kb_id, status="READY").first()
        version = db.get(retrieval.KbVersion, first_index.kb_version_id).version
        first_index_name = first_index.version
        retrieval.set_alias(db, kb, "prod", version, first_index_name)

    second = _add_document(client, headers, kb_id, "second")
    with SessionLocal() as db:
        indexes = db.query(IndexVersion).filter_by(kb_id=kb_id, status="READY").all()
        second_index = next(index for index in indexes if index.version != first_index_name)
        second_index_name = second_index.version
        second_version = db.get(retrieval.KbVersion, second_index.kb_version_id).version
        assert db.query(IndexChunkSnapshot).filter_by(index_version_id=first_index.id).count() == 1
        assert db.query(IndexChunkSnapshot).filter_by(index_version_id=second_index.id).count() == 2

    def retrieve(index=None):
        payload = {"kb_ids": [kb_id], "query": "unique document", "top_k": 10}
        if index:
            payload["index_version"] = index
            payload["kb_version"] = second_version
        response = client.post("/v1/retrieve", json=payload, headers=headers)
        assert response.status_code == 200, response.text
        return {item["doc_id"] for item in response.json()["results"]}

    assert retrieve() == {first["document"]["id"]}
    assert retrieve(second_index_name) == {first["document"]["id"], second["document"]["id"]}
    response = client.post(
        "/v1/retrieve",
        json={"kb_ids": [kb_id], "query": "document", "index_version": "missing"},
        headers=headers,
    )
    assert response.status_code == 404


def test_failed_vector_build_preserves_previous_snapshot(client, new_tenant_app, monkeypatch):
    headers = new_tenant_app["headers"]
    kb_id = _create_kb(client, headers)
    _add_document(client, headers, kb_id, "first")

    class BrokenVectorStore:
        def index_chunks(self, **kwargs):
            raise RuntimeError("partial vector write")

    with SessionLocal() as db:
        kb = db.get(KnowledgeBase, kb_id)
        chunk = db.query(ChunkMeta).filter_by(kb_id=kb_id).first()
        previous_id = chunk.index_version_id
        previous_embedding = chunk.embedding
        monkeypatch.setattr(get_settings(), "vector_backend", "milvus")
        monkeypatch.setattr(get_settings(), "vector_fallback_sql", False)
        monkeypatch.setattr(retrieval, "get_vector_store", lambda: BrokenVectorStore())
        with pytest.raises(RuntimeError, match="partial vector write"):
            retrieval.index_kb(db, kb)
        db.expire_all()
        chunk = db.query(ChunkMeta).filter_by(kb_id=kb_id).first()
        assert chunk.index_version_id == previous_id
        assert chunk.embedding == previous_embedding
        assert db.query(IndexChunkSnapshot).filter_by(index_version_id=previous_id).count() == 1
        failed = db.query(IndexVersion).filter_by(kb_id=kb_id, status="FAILED").one()
        assert db.query(IndexChunkSnapshot).filter_by(index_version_id=failed.id).count() == 0
        monkeypatch.setattr(get_settings(), "vector_backend", "sql")
        result = retrieval.retrieve(db, kb.tenant_id, [kb.id], "first")
        assert {item["chunk_id"] for item in result["results"]} == {chunk.chunk_id}


def test_milvus_candidates_must_belong_to_selected_snapshot(client, new_tenant_app, monkeypatch):
    headers = new_tenant_app["headers"]
    kb_id = _create_kb(client, headers)
    first = _add_document(client, headers, kb_id, "first")
    with SessionLocal() as db:
        kb = db.get(KnowledgeBase, kb_id)
        index = db.query(IndexVersion).filter_by(kb_id=kb_id, status="READY").first()
        version = db.get(retrieval.KbVersion, index.kb_version_id)
        retrieval.set_alias(db, kb, "prod", version.version, index.version)

    second = _add_document(client, headers, kb_id, "second")
    with SessionLocal() as db:
        chunks = db.query(ChunkMeta).filter_by(kb_id=kb_id).all()
        hit_ids = [chunk.chunk_id for chunk in chunks]

    class WrongVectorStore:
        def search(self, **kwargs):
            return [VectorHit(chunk_id=chunk_id, score=1.0) for chunk_id in hit_ids]

    monkeypatch.setattr(get_settings(), "vector_backend", "milvus")
    monkeypatch.setattr(retrieval, "get_vector_store", lambda: WrongVectorStore())
    response = client.post(
        "/v1/retrieve", json={"kb_ids": [kb_id], "query": "document"}, headers=headers
    )
    assert response.status_code == 200, response.text
    assert {item["doc_id"] for item in response.json()["results"]} == {first["document"]["id"]}
    assert second["document"]["id"] not in {
        item["doc_id"] for item in response.json()["results"]
    }


def test_degraded_vector_index_uses_sql_snapshot(client, new_tenant_app, monkeypatch):
    headers = new_tenant_app["headers"]
    kb_id = _create_kb(client, headers)
    document = _add_document(client, headers, kb_id, "first")
    with SessionLocal() as db:
        index = db.query(IndexVersion).filter_by(kb_id=kb_id, status="READY").first()
        index.stats = {**index.stats, "vector": {"degraded": True}}
        db.commit()
    monkeypatch.setattr(get_settings(), "vector_backend", "milvus")
    monkeypatch.setattr(get_settings(), "vector_fallback_sql", True)
    response = client.post(
        "/v1/retrieve", json={"kb_ids": [kb_id], "query": "document"}, headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["usage"]["recall_mode"] == "sql-fallback"
    assert response.json()["results"][0]["doc_id"] == document["document"]["id"]
    monkeypatch.setattr(get_settings(), "vector_fallback_sql", False)
    response = client.post(
        "/v1/retrieve", json={"kb_ids": [kb_id], "query": "document"}, headers=headers
    )
    assert response.status_code == 503


def test_missing_milvus_collection_is_not_silent(client, new_tenant_app, monkeypatch):
    headers = new_tenant_app["headers"]
    kb_id = _create_kb(client, headers)
    document = _add_document(client, headers, kb_id, "first")

    class MissingCollection:
        def has_collection(self, **kwargs):
            return False

    store = MilvusVectorStore.__new__(MilvusVectorStore)
    store._settings = SimpleNamespace(milvus_collection_prefix="test")
    store._client = MissingCollection()
    monkeypatch.setattr(retrieval, "get_vector_store", lambda: store)
    monkeypatch.setattr(get_settings(), "vector_backend", "milvus")
    monkeypatch.setattr(get_settings(), "vector_fallback_sql", False)
    payload = {"kb_ids": [kb_id], "query": "document"}
    response = client.post("/v1/retrieve", json=payload, headers=headers)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"

    monkeypatch.setattr(get_settings(), "vector_fallback_sql", True)
    response = client.post("/v1/retrieve", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["usage"]["recall_mode"] == "sql-fallback"
    assert response.json()["results"][0]["doc_id"] == document["document"]["id"]


def test_empty_milvus_index_does_not_require_collection(client, new_tenant_app, monkeypatch):
    headers = new_tenant_app["headers"]
    kb_id = _create_kb(client, headers)
    with SessionLocal() as db:
        index = retrieval.index_kb(db, db.get(KnowledgeBase, kb_id))
        assert index.stats["chunk_count"] == 0

    class NoSearch:
        def search(self, **kwargs):
            raise AssertionError("empty index must not query Milvus")

    monkeypatch.setattr(retrieval, "get_vector_store", lambda: NoSearch())
    monkeypatch.setattr(get_settings(), "vector_backend", "milvus")
    monkeypatch.setattr(get_settings(), "vector_fallback_sql", False)
    response = client.post(
        "/v1/retrieve", json={"kb_ids": [kb_id], "query": "document"}, headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["results"] == []


def test_alias_rejects_invalid_index_and_version(client, new_tenant_app):
    headers = new_tenant_app["headers"]
    kb_id = _create_kb(client, headers)
    _add_document(client, headers, kb_id, "first")
    with SessionLocal() as db:
        kb = db.get(KnowledgeBase, kb_id)
        index = db.query(IndexVersion).filter_by(kb_id=kb_id, status="READY").first()
        other = retrieval.create_kb_version(db, kb, "other")
        with pytest.raises(retrieval.ApiError) as error:
            retrieval.set_alias(db, kb, "prod", other.version, index.version)
        assert error.value.status_code == 409
        index.status = "BUILDING"
        db.commit()
        version = db.get(retrieval.KbVersion, index.kb_version_id)
        with pytest.raises(retrieval.ApiError) as error:
            retrieval.set_alias(db, kb, "prod", version.version, index.version)
        assert error.value.status_code == 409
        with pytest.raises(retrieval.ApiError) as error:
            retrieval.index_kb(db, kb, "does-not-exist")
        assert error.value.status_code == 404


def test_snapshot_migration_backfills_existing_rows():
    path = Path(__file__).resolve().parents[1] / "migrations/versions/0004_index_chunk_snapshot.py"
    spec = importlib.util.spec_from_file_location("index_snapshot_migration", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text(
            "CREATE TABLE chunk_meta (chunk_id VARCHAR(64), "
            "index_version_id BIGINT, embedding JSON)"
        ))
        connection.execute(sa.text(
            """INSERT INTO chunk_meta VALUES ('one', 42, '[1.0, 2.0]'), ('two', NULL, NULL)"""
        ))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()
        rows = connection.execute(sa.text(
            "SELECT index_version_id, chunk_id, embedding FROM index_chunk_snapshot"
        )).all()
        assert len(rows) == 1
        assert rows[0][0:2] == (42, "one")
        assert rows[0][2] == "[1.0, 2.0]"


def test_kb_version_rebuild_uses_frozen_text_and_document_metadata(client, new_tenant_app):
    headers = new_tenant_app["headers"]
    kb_id = _create_kb(client, headers)
    first = _add_document(client, headers, kb_id, "first")
    document_id = first["document"]["id"]
    response = client.post(
        f"/api/v1/kbs/{kb_id}/versions", json={"version": "frozen"}, headers=headers
    )
    assert response.status_code == 201, response.text
    assert response.json()["data"]["snapshot_state"] == "PARSED"
    with SessionLocal() as db:
        chunk = db.query(ChunkMeta).filter_by(document_id=document_id).one()
        original_text = chunk.text
        chunk.text = "changed live content"
        db.get(Document, document_id).title = "changed title"
        db.commit()

    _add_document(client, headers, kb_id, "later")
    response = client.post(f"/api/v1/kbs/{kb_id}/index-jobs?version=frozen", headers=headers)
    assert response.status_code == 202, response.text
    assert response.json()["data"]["status"] == "SUCCEEDED"
    response = client.post(
        "/v1/retrieve",
        json={"kb_ids": [kb_id], "query": "first", "kb_version": "frozen"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    results = response.json()["results"]
    assert len(results) == 1
    assert results[0]["doc_id"] == document_id
    assert results[0]["text"] == original_text
    assert results[0]["citation"]["title"] == "first"
    citation = results[0]["citation"]
    assert citation["source_status"] == "SNAPSHOT_AVAILABLE"
    source = client.get(citation["source_url"], headers=headers)
    assert source.status_code == 200, source.text
    assert source.json()["data"]["document"]["title"] == "first"
    assert [item["text"] for item in source.json()["data"]["chunks"]] == [original_text]
    assert client.get(
        f"/api/v1/documents/{document_id}?version=123456789", headers=headers
    ).status_code == 404
    assert client.get(
        f"/api/v1/documents/{document_id}?kb_version_id=123456789", headers=headers
    ).status_code == 400
    assert client.get(
        f"/api/v1/documents/{document_id}?version=123456789&kb_version_id="
        f"{source.json()['data']['snapshot']['kb_version_id']}",
        headers=headers,
    ).status_code == 404


def test_legacy_kb_version_cannot_silently_rebuild(client, new_tenant_app):
    kb_id = _create_kb(client, new_tenant_app["headers"])
    _add_document(client, new_tenant_app["headers"], kb_id, "first")
    with SessionLocal() as db:
        kb = db.get(KnowledgeBase, kb_id)
        version = retrieval.create_kb_version(db, kb, "legacy")
        version.snapshot_state = "LEGACY"
        db.commit()
        with pytest.raises(retrieval.ApiError) as error:
            retrieval.index_kb(db, kb, "legacy")
        assert error.value.status_code == 409
