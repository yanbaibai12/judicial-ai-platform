from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from judicial_ai.config import Settings, get_settings
from judicial_ai.services import retrieval as retrieval_service
from judicial_ai.text_utils import normalize_text


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_rerank_threshold_must_be_finite(monkeypatch, value: str) -> None:
    monkeypatch.setenv("RAG_MIN_RERANK_SCORE", value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def _create_kb(client, headers) -> int:
    response = client.post(
        "/api/v1/kbs",
        json={
            "code": f"kb-{uuid.uuid4().hex[:8]}",
            "name": "测试法律知识库",
            "domain": "legal",
            "embedding_model": "bge-m3",
            "chunk_config": {"max_chars": 240, "overlap": 30},
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


def test_rerank_threshold_filters_unrelated_and_missing_scores(client, new_tenant_app, monkeypatch):
    headers = new_tenant_app["headers"]
    kb_id = _create_kb(client, headers)
    response = client.post(
        f"/api/v1/kbs/{kb_id}/documents",
        json={"title": "test", "text": "民间借贷利率有相关规定"},
        headers=headers,
    )
    assert response.status_code == 201
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_min_rerank_score", 0.5)
    inference = retrieval_service.get_inference_client()
    query = {"kb_ids": [kb_id], "query": "民间借贷", "compress": True}

    for results, expected in [
        ([{"index": 0, "relevance_score": 0.8}], 1),
        ([{"index": 0, "relevance_score": 0.01}], 0),
        ([], 0),
    ]:
        monkeypatch.setattr(inference, "rerank", lambda *args, result=results: result)
        response = client.post("/v1/retrieve", json=query, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["results"]) == expected
        if not expected:
            assert body["citation"]["status"] == "NONE"
            assert body["compression"]["reason"] == "no_results"
            assert body["usage"]["filtered_low_relevance"] == 1

    monkeypatch.setattr(
        inference, "rerank", lambda *args: [{"index": 0, "relevance_score": float("nan")}]
    )
    assert client.post("/v1/retrieve", json=query, headers=headers).status_code == 503


@pytest.mark.parametrize(
    ("best_score", "worst_score", "threshold"),
    [(0.8, 0.2, 0.5), (-1.0, -5.0, -2.0), (1e308, -1e308, 0.0)],
)
def test_rerank_scores_keep_their_order(
    client, new_tenant_app, monkeypatch, best_score, worst_score, threshold
):
    headers = new_tenant_app["headers"]
    kb_id = _create_kb(client, headers)
    for title in ("best", "worst", "missing"):
        response = client.post(
            f"/api/v1/kbs/{kb_id}/documents",
            json={"title": title, "text": f"司法证据 {title}"},
            headers=headers,
        )
        assert response.status_code == 201, response.text

    def rerank(_model, _query, documents):
        return [
            {"index": index, "relevance_score": best_score if "best" in text else worst_score}
            for index, text in enumerate(documents)
            if "missing" not in text
        ]

    monkeypatch.setattr(retrieval_service.get_inference_client(), "rerank", rerank)
    response = client.post(
        "/v1/retrieve",
        json={"kb_ids": [kb_id], "query": "司法证据", "top_k": 3},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["results"]) == 3
    assert "best" in response.json()["results"][0]["text"]

    monkeypatch.setattr(get_settings(), "rag_min_rerank_score", threshold)
    response = client.post(
        "/v1/retrieve",
        json={"kb_ids": [kb_id], "query": "司法证据", "top_k": 3},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["results"]) == 1
    assert "best" in response.json()["results"][0]["text"]


def test_empty_milvus_results_do_not_use_sql_fallback(client, new_tenant_app, monkeypatch):
    headers = new_tenant_app["headers"]
    kb_id = _create_kb(client, headers)
    response = client.post(
        f"/api/v1/kbs/{kb_id}/documents",
        json={"title": "test", "text": "民间借贷利率有相关规定"},
        headers=headers,
    )
    assert response.status_code == 201

    class EmptyVectorStore:
        def search(self, **kwargs):
            return []

    monkeypatch.setattr(get_settings(), "vector_backend", "milvus")
    monkeypatch.setattr(get_settings(), "vector_fallback_sql", False)
    monkeypatch.setattr(retrieval_service, "get_vector_store", lambda: EmptyVectorStore())
    response = client.post(
        "/v1/retrieve", json={"kb_ids": [kb_id], "query": "民间借贷"}, headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["results"] == []
    assert response.json()["usage"]["recall_mode"] == "milvus-hybrid"


def test_document_index_and_retrieve(client, new_tenant_app):
    headers = new_tenant_app["headers"]
    kb_id = _create_kb(client, headers)

    response = client.post(
        f"/api/v1/kbs/{kb_id}/documents",
        json={
            "title": "民法典借款合同条款",
            "doc_type": "law",
            "text": (
                "第六百八十条 禁止高利放贷，借款的利率不得违反国家有关规定。\n\n"
                "借款合同对支付利息没有约定的，视为没有利息。\n\n"
                "出借人请求借款人按照合同约定利率支付利息的，人民法院应予支持，"
                "但是双方约定的利率超过合同成立时一年期贷款市场报价利率四倍的除外。"
            ),
            "metadata": {"doc_no": "主席令第四十五号", "effective": True},
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["data"]["chunk_count"] >= 1

    response = client.post(
        f"/api/v1/kbs/{kb_id}/versions",
        json={"version": "v1.0.0", "publish": True},
        headers=headers,
    )
    assert response.status_code == 201, response.text

    response = client.post(f"/api/v1/kbs/{kb_id}/index-jobs?version=v1.0.0", headers=headers)
    assert response.status_code == 202, response.text
    index_version = response.json()["data"]["index"]["version"]

    response = client.post(
        f"/api/v1/kbs/{kb_id}/aliases",
        json={
            "alias": "prod",
            "kb_version": "v1.0.0",
            "index_version": index_version,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text

    response = client.post(
        "/v1/retrieve",
        json={
            "kb_ids": [kb_id],
            "query": "民间借贷利率超过多少不受保护",
            "top_k": 3,
            "with_citation": True,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["results"], body
    assert body["results"][0]["citation"]["title"] == "民法典借款合同条款"
    assert body["versions"][str(kb_id)]["kb"] == "v1.0.0"
    assert body["graph"]["degraded"] is True


def test_retrieve_with_context_compression(client, new_tenant_app):
    headers = new_tenant_app["headers"]
    kb_id = _create_kb(client, headers)
    text = (
        "第六百八十条 禁止高利放贷，借款的利率不得违反国家有关规定。\n\n"
        "借款合同对支付利息没有约定的，视为没有利息。\n\n"
        "出借人请求借款人按照合同约定利率支付利息的，人民法院应予支持，"
        "但是双方约定的利率超过合同成立时一年期贷款市场报价利率四倍的除外。\n\n"
        "本条所称一年期贷款市场报价利率，是指中国人民银行授权全国银行间同业拆借中心"
        "自2019年8月20日起每月发布的一年期贷款市场报价利率。\n\n"
        "出借人与借款人既约定了逾期利率，又约定了违约金或者其他费用，"
        "出借人可以选择主张逾期利息、违约金或者其他费用，也可以一并主张，"
        "但是总计超过合同成立时一年期贷款市场报价利率四倍的部分，人民法院不予支持。\n\n"
        "借款合同应当采用书面形式，但是自然人之间借款另有约定的除外。\n\n"
        "自然人之间的借款合同，自贷款人提供借款时成立。\n\n"
        "禁止高利放贷，借款的利率不得违反国家有关规定。"
    )
    response = client.post(
        f"/api/v1/kbs/{kb_id}/documents",
        json={
            "title": "民法典借款合同条款",
            "doc_type": "law",
            "text": text,
            "metadata": {"doc_no": "主席令第四十五号"},
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text

    response = client.post(
        f"/api/v1/kbs/{kb_id}/versions",
        json={"version": "v1.0.0", "publish": True},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    response = client.post(f"/api/v1/kbs/{kb_id}/index-jobs?version=v1.0.0", headers=headers)
    assert response.status_code == 202, response.text
    index_version = response.json()["data"]["index"]["version"]
    response = client.post(
        f"/api/v1/kbs/{kb_id}/aliases",
        json={"alias": "prod", "kb_version": "v1.0.0", "index_version": index_version},
        headers=headers,
    )
    assert response.status_code == 200, response.text

    response = client.post(
        "/v1/retrieve",
        json={
            "kb_ids": [kb_id],
            "query": "民间借贷起诉需要准备哪些材料",
            "top_k": 5,
            "with_citation": True,
            "compress": True,
            "context_budget_chars": 300,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["results"]
    assert body["decision"]["provider"] == "rule"
    assert body["decision"]["intent"] == "PROCEDURE_GUIDE"
    assert body["compression"]["enabled"] is True
    assert body["compression"]["citation_integrity"] is True
    assert body["compression"]["compressed_chunks"] >= 1
    assert body["compression"]["compressed_chars"] <= body["compression"]["original_chars"]
    normalized = normalize_text(text).replace("\n\n", "\n")
    assert all(
        item["citation"]["snippet"] in normalized
        for item in body["results"]
        if isinstance(item.get("citation"), dict)
    )


def test_retrieval_is_tenant_scoped(client, admin_headers, new_tenant_app):
    headers = new_tenant_app["headers"]
    kb_id = _create_kb(client, headers)

    response = client.post(
        "/api/v1/admin/tenants",
        json={"code": "retrieval-other", "name": "Other"},
        headers=admin_headers,
    )
    assert response.status_code == 201
    other_tenant_id = response.json()["data"]["id"]
    response = client.post(
        "/api/v1/admin/apps",
        json={"tenant_id": other_tenant_id, "code": "retrieval-other-app", "name": "Other"},
        headers=admin_headers,
    )
    assert response.status_code == 201
    other_app_id = response.json()["data"]["id"]
    response = client.post(f"/api/v1/admin/apps/{other_app_id}/credentials", headers=admin_headers)
    assert response.status_code == 201
    other_key = response.json()["data"]["api_key"]

    response = client.get(f"/api/v1/kbs/{kb_id}", headers={"Authorization": f"Bearer {other_key}"})
    assert response.status_code == 404

def test_invalid_kb_does_not_call_decision_model(client, new_tenant_app, monkeypatch):
    calls = []

    def fail_if_called(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("decision model must not run before KB authorization")

    monkeypatch.setattr("judicial_ai.services.retrieval.plan_query", fail_if_called)
    response = client.post(
        "/v1/retrieve",
        json={"kb_ids": [999999], "query": "未授权知识库不应触发决策模型"},
        headers=new_tenant_app["headers"],
    )
    assert response.status_code == 404
    assert calls == []
