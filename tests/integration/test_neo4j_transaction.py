from __future__ import annotations

import os
import secrets
from types import SimpleNamespace

import pytest

from judicial_ai.config import get_settings
from judicial_ai.services.graph_store import Neo4jGraphStore


@pytest.mark.skipif(
    not os.getenv("JAI_ISOLATED_NEO4J_TEST_URI"),
    reason="JAI_ISOLATED_NEO4J_TEST_URI not configured",
)
def test_graph_write_rollback_on_real_neo4j(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "neo4j_uri", os.environ["JAI_ISOLATED_NEO4J_TEST_URI"])
    monkeypatch.setattr(settings, "neo4j_password", "")
    store = Neo4jGraphStore()
    driver = store._driver
    index_id = secrets.randbits(52)
    kb = SimpleNamespace(id=index_id, tenant_id=index_id)
    index = SimpleNamespace(id=index_id)
    chunk = SimpleNamespace(
        chunk_id=f"isolated-{index_id}", document_id=index_id, text="民间借贷 涉及 100万元"
    )

    class FailingTransaction:
        def __init__(self, transaction):
            self.transaction = transaction

        def run(self, query, **parameters):
            if "UNWIND" in query:
                raise RuntimeError("injected failure after delete")
            return self.transaction.run(query, **parameters)

    class FailingSession:
        def __init__(self, session):
            self.session = session

        def __enter__(self):
            self.session.__enter__()
            return self

        def __exit__(self, *args):
            return self.session.__exit__(*args)

        def execute_write(self, callback):
            return self.session.execute_write(
                lambda transaction: callback(FailingTransaction(transaction))
            )

    class FailingDriver:
        def session(self, **kwargs):
            return FailingSession(driver.session(**kwargs))

    try:
        result = store.index_chunks(kb=kb, index_version=index, chunks=[chunk])
        assert result["entities"] >= 2
        with driver.session(database=settings.neo4j_database) as session:
            before = session.run(
                "MATCH (e:Entity {index_version_id: $id}) RETURN count(e) AS count",
                id=index_id,
            ).single()["count"]
        assert before >= 2
        store._driver = FailingDriver()
        with pytest.raises(RuntimeError, match="injected failure"):
            store.index_chunks(kb=kb, index_version=index, chunks=[chunk])
        with driver.session(database=settings.neo4j_database) as session:
            after = session.run(
                "MATCH (e:Entity {index_version_id: $id}) RETURN count(e) AS count",
                id=index_id,
            ).single()["count"]
        assert after == before
    finally:
        with driver.session(database=settings.neo4j_database) as session:
            session.run(
                "MATCH (e:Entity {index_version_id: $id}) DETACH DELETE e", id=index_id
            ).consume()
        driver.close()
