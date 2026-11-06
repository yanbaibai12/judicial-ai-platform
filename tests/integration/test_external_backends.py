from __future__ import annotations

import os

import pytest

from judicial_ai.config import get_settings


@pytest.mark.skipif(not os.getenv("MILVUS_URI"), reason="MILVUS_URI not configured")
def test_milvus_health(monkeypatch):
    from judicial_ai.services.vector_store import MilvusVectorStore

    settings = get_settings()
    monkeypatch.setattr(settings, "vector_backend", "milvus")
    assert MilvusVectorStore().health()["status"] == "ok"


@pytest.mark.skipif(not os.getenv("NEO4J_URI"), reason="NEO4J_URI not configured")
def test_neo4j_health(monkeypatch):
    from judicial_ai.services.graph_store import Neo4jGraphStore

    settings = get_settings()
    monkeypatch.setattr(settings, "graph_backend", "neo4j")
    store = Neo4jGraphStore()
    try:
        assert store.health()["status"] == "ok"
    finally:
        store.close()


@pytest.mark.skipif(
    not os.getenv("JAI_TEST_INFERENCE_BASE_URL"),
    reason="JAI_TEST_INFERENCE_BASE_URL not configured",
)
def test_proxy_inference_health(monkeypatch):
    from judicial_ai.services.inference import InferenceClient

    settings = get_settings()
    monkeypatch.setattr(settings, "inference_mode", "proxy")
    monkeypatch.setattr(settings, "inference_base_url", os.environ["JAI_TEST_INFERENCE_BASE_URL"])
    assert InferenceClient().health()["status"] == "ok"
