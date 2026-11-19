from __future__ import annotations

import os
import uuid

import pytest

from judicial_ai.config import get_settings
from judicial_ai.services.inference import get_inference_client


@pytest.mark.skipif(
    os.environ.get("JAI_TEST_REAL_PILOT") != "1",
    reason="requires explicit real model services and isolated test data",
)
def test_real_model_pilot(client, admin_headers, new_tenant_app, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "inference_mode", "proxy")
    monkeypatch.setattr(settings, "embedding_base_url", os.environ["JAI_TEST_EMBEDDING_URL"])
    monkeypatch.setattr(settings, "embedding_api_key", os.environ["JAI_TEST_EMBEDDING_KEY"])
    monkeypatch.setattr(settings, "embedding_model", "Qwen3-Embedding-8B")
    monkeypatch.setattr(settings, "embedding_dim", 1024)
    monkeypatch.setattr(settings, "rerank_base_url", os.environ["JAI_TEST_RERANK_URL"])
    monkeypatch.setattr(settings, "rerank_api_format", "tei")
    monkeypatch.setattr(settings, "rag_rerank_model", "bge-reranker-v2-m3")
    monkeypatch.setattr(settings, "inference_timeout_seconds", 180)
    get_inference_client.cache_clear()
    try:
        test_pilot_retrieval_feedback_and_regression(
            client, admin_headers, new_tenant_app, monkeypatch
        )
    finally:
        get_inference_client.cache_clear()


def test_pilot_retrieval_feedback_and_regression(
    client, admin_headers, new_tenant_app, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "resource_acl_enforced", True)
    monkeypatch.setattr(settings, "index_require_approval", True)
    monkeypatch.setattr(settings, "require_prod_alias", True)
    monkeypatch.setattr(settings, "job_mode", "inline")
    monkeypatch.setattr(settings, "kb_auto_index_after_upload", True)

    owner = new_tenant_app["headers"]
    suffix = uuid.uuid4().hex[:8]
    response = client.post(
        "/api/v1/kbs", json={"code": f"pilot-{suffix}", "name": "pilot"}, headers=owner
    )
    assert response.status_code == 201, response.text
    kb_id = response.json()["data"]["id"]

    text = f"试点测试资料 {suffix}：该段文字仅用于检索链路测试，不是法律依据。"
    response = client.post(
        f"/api/v1/kbs/{kb_id}/documents",
        json={"title": "试点测试资料", "text": text},
        headers=owner,
    )
    assert response.status_code == 201, response.text
    document = response.json()["data"]
    job = client.get(f"/api/v1/jobs/{document['index_job_id']}", headers=owner)
    assert job.status_code == 200, job.text
    assert job.json()["data"]["status"] == "SUCCEEDED"
    index = job.json()["data"]["result"]["index"]
    assert index["status"] == "PENDING_APPROVAL"

    versions = client.get(f"/api/v1/kbs/{kb_id}/versions", headers=owner)
    assert versions.status_code == 200, versions.text
    kb_version = next(
        item["version"]
        for item in versions.json()["data"]["kb_versions"]
        if item["id"] == index["kb_version_id"]
    )
    alias = {"alias": "prod", "kb_version": kb_version, "index_version": index["version"]}
    assert client.post(f"/api/v1/kbs/{kb_id}/aliases", json=alias, headers=owner).status_code == 409
    approved = client.post(
        f"/api/v1/kbs/{kb_id}/indexes/{index['id']}/approve",
        json={"reason": "pilot test"}, headers=admin_headers,
    )
    assert approved.status_code == 200, approved.text
    response = client.post(f"/api/v1/kbs/{kb_id}/aliases", json=alias, headers=owner)
    assert response.status_code == 200, response.text

    response = client.post(
        "/api/v1/admin/apps",
        json={
            "tenant_id": new_tenant_app["tenant_id"], "code": f"reader-{suffix}",
            "name": "pilot reader",
            "permissions": ["app:invoke", "kb:read", "governance:read", "governance:write"],
        },
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    reader_id = response.json()["data"]["id"]
    response = client.post(
        f"/api/v1/admin/apps/{reader_id}/credentials", headers=admin_headers
    )
    assert response.status_code == 201, response.text
    reader = {"Authorization": f"Bearer {response.json()['data']['api_key']}"}
    query = {"kb_ids": [kb_id], "query": f"试点测试资料 {suffix}", "top_k": 3}
    assert client.post("/v1/retrieve", json=query, headers=reader).status_code == 403
    response = client.post(
        f"/api/v1/kbs/{kb_id}/grants",
        json={"subject_kind": "app", "subject_id": str(reader_id), "level": "read"},
        headers=owner,
    )
    assert response.status_code == 201, response.text
    response = client.post("/v1/retrieve", json=query, headers=reader)
    assert response.status_code == 200, response.text
    retrieved = response.json()
    assert retrieved["citation"]["status"] == "VERIFIED"
    result = retrieved["results"][0]
    assert result["doc_id"] == document["document"]["id"]
    assert result["citation"]["document_version_id"] == document["document_version_id"]
    source = client.get(result["citation"]["source_url"], headers=reader)
    assert source.status_code == 200, source.text
    assert any(
        chunk["chunk_id"] == result["chunk_id"] and chunk["text"] == text
        for chunk in source.json()["data"]["chunks"]
    )

    response = client.post(
        "/api/v1/feedback",
        json={"request_id": retrieved["request_id"], "rating": "NEGATIVE", "reason": "pilot"},
        headers=reader,
    )
    assert response.status_code == 201, response.text
    feedback_id = response.json()["data"]["id"]
    feedback = client.get("/api/v1/feedback", headers=reader)
    assert any(item["request_id"] == retrieved["request_id"]
               for item in feedback.json()["data"]["items"])
    response = client.post(
        "/api/v1/bad-cases",
        json={"request_id": retrieved["request_id"], "category": "CITATION", "priority": "P2"},
        headers=reader,
    )
    assert response.status_code == 201, response.text
    bad_case_id = response.json()["data"]["id"]
    response = client.patch(
        f"/api/v1/bad-cases/{bad_case_id}",
        json={"root_cause": "pilot feedback", "add_to_regression": True}, headers=reader,
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["regression_case_id"]
    datasets = client.get("/api/v1/datasets", headers=reader)
    assert any(item["code"] == "bad-case-regression" for item in datasets.json()["data"]["items"])
    response = client.post(
        "/api/v1/datasets",
        json={"code": f"pilot-eval-{suffix}", "name": "pilot", "type": "RETRIEVAL_EVAL"},
        headers=reader,
    )
    assert response.status_code == 201, response.text
    dataset_id = response.json()["data"]["id"]
    response = client.post(
        f"/api/v1/datasets/{dataset_id}/versions",
        json={
            "version": "v1",
            "source_snapshot": {"source": "retrieval_feedback", "feedback_ids": [feedback_id]},
            "filter_config": {"redact": True},
            "split": {"train": 0.0, "validation": 0.0, "test": 1.0},
        },
        headers=reader,
    )
    assert response.status_code == 201, response.text
    version = response.json()["data"]
    assert version["stats"]["total"] >= 1
    version_id = version["id"]
    samples = client.get(f"/api/v1/dataset-versions/{version_id}/items", headers=reader)
    assert samples.status_code == 200, samples.text
    sample = samples.json()["data"]["items"][0]
    assert sample["review_status"] == "NEEDS_REVIEW"
    assert sample["label"]["relevant_chunk_ids"] == []
    assert sample["metadata"]["feedback_id"] == feedback_id
    assert sample["metadata"]["retrieval_versions"] == retrieved["versions"]
    response = client.post(f"/api/v1/dataset-versions/{version_id}/publish", headers=reader)
    assert response.status_code == 409
    response = client.post(
        "/api/v1/annotation-tasks",
        json={
            "name": "pilot review", "task_type": "RETRIEVAL",
            "dataset_version_id": version_id, "required_reviewers": 1,
        },
        headers=reader,
    )
    assert response.status_code == 201, response.text
    task_id = response.json()["data"]["id"]
    response = client.post(
        f"/api/v1/annotation-tasks/{task_id}/records",
        json={
            "item_id": sample["id"], "decision": "APPROVED",
            "label": {"relevant_chunk_ids": [result["chunk_id"]]},
        },
        headers=reader,
    )
    assert response.status_code == 201, response.text
    response = client.post(f"/api/v1/dataset-versions/{version_id}/publish", headers=reader)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "PUBLISHED"
    response = client.post(
        "/api/v1/evals",
        json={
            "dataset_version_id": version_id, "target_type": "RETRIEVAL",
            "target_version": "pilot", "config": {"top_k": 3, "gates": {"recall_at_k": 1.0}},
        },
        headers=reader,
    )
    assert response.status_code == 201, response.text
    assert response.json()["data"]["status"] == "SUCCEEDED"
    assert response.json()["data"]["metrics"]["samples"] >= 1
    assert response.json()["data"]["metrics"]["gate_passed"] is True
    response = client.post(
        "/api/v1/evals",
        json={
            "dataset_version_id": version_id, "target_type": "RETRIEVAL",
            "target_version": "pilot-candidate", "config": {"top_k": 3},
        },
        headers=reader,
    )
    assert response.status_code == 201, response.text
    assert response.json()["data"]["status"] == "SUCCEEDED"
    assert response.json()["data"]["metrics"]["recall_at_k"] == 1.0
