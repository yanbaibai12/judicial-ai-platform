from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from judicial_ai.config import get_settings
from judicial_ai.db import SessionLocal
from judicial_ai.models import IndexVersion
from judicial_ai.services.graph_store import GraphResult, Neo4jGraphStore


def test_degraded_graph_is_excluded_from_mixed_retrieval(client, new_tenant_app, monkeypatch):
    headers = new_tenant_app["headers"]
    monkeypatch.setattr(get_settings(), "graph_enabled", True)
    ids = []
    for suffix in ("healthy", "failed"):
        response = client.post(
            "/api/v1/kbs", json={"code": f"graph-{suffix}", "name": suffix}, headers=headers
        )
        assert response.status_code == 201, response.text
        kb_id = response.json()["data"]["id"]
        response = client.post(
            f"/api/v1/kbs/{kb_id}/documents",
            json={"title": suffix, "text": f"contract graph {suffix}"},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        ids.append(kb_id)

    with SessionLocal() as db:
        healthy = db.query(IndexVersion).filter_by(kb_id=ids[0], status="READY").one()
        failed = db.query(IndexVersion).filter_by(kb_id=ids[1], status="READY").one()
        healthy_id = healthy.id
        failed.stats = {**failed.stats, "graph": {"backend": "sql", "degraded": True}}
        db.commit()

    seen = []

    def search(**kwargs):
        seen.extend(kwargs["index_version_ids"])
        return GraphResult([], [], backend="sql")

    monkeypatch.setattr("judicial_ai.services.retrieval.graph_search_or_degraded", search)
    response = client.post(
        "/v1/retrieve",
        json={"kb_ids": ids, "query": "contract", "with_graph": True},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert seen == [healthy_id]
    assert response.json()["graph"]["degraded"] is True


def test_neo4j_graph_write_uses_one_transaction():
    store = Neo4jGraphStore.__new__(Neo4jGraphStore)
    store._settings = SimpleNamespace(neo4j_database="neo4j")
    transaction = Mock()
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    session.execute_write.side_effect = lambda callback: callback(transaction)
    store._driver = Mock()
    store._driver.session.return_value = session
    chunk = SimpleNamespace(chunk_id="chunk", document_id=10, text="合同 借款 利率")
    store.index_chunks(
        kb=SimpleNamespace(id=2, tenant_id=1),
        index_version=SimpleNamespace(id=3),
        chunks=[chunk],
    )
    session.execute_write.assert_called_once()
    assert transaction.run.call_count >= 1
    session.run.assert_not_called()


def test_neo4j_transaction_failure_propagates():
    store = Neo4jGraphStore.__new__(Neo4jGraphStore)
    store._settings = SimpleNamespace(neo4j_database="neo4j")
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    session.execute_write.side_effect = RuntimeError("transaction failed")
    store._driver = Mock()
    store._driver.session.return_value = session
    with pytest.raises(RuntimeError, match="transaction failed"):
        store.index_chunks(
            kb=SimpleNamespace(id=2, tenant_id=1),
            index_version=SimpleNamespace(id=3),
            chunks=[],
        )
