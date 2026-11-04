from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from judicial_ai.config import get_settings
from judicial_ai.db import SessionLocal
from judicial_ai.models import AuditLog, ChunkMeta, ResourceGrant
from judicial_ai.services.graph_store import GraphResult


def test_document_access_across_retrieval_and_graph(
    client, new_tenant_app, admin_headers, monkeypatch
):
    monkeypatch.setattr(get_settings(), "resource_acl_enforced", True)
    monkeypatch.setattr(get_settings(), "graph_enabled", True)
    owner = new_tenant_app["headers"]
    response = client.post(
        "/api/v1/admin/apps",
        json={
            "tenant_id": new_tenant_app["tenant_id"],
            "code": f"reader-{uuid.uuid4().hex[:8]}",
            "name": "reader",
        },
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    app_id = response.json()["data"]["id"]
    response = client.post(f"/api/v1/admin/apps/{app_id}/credentials", headers=admin_headers)
    reader = {"Authorization": f"Bearer {response.json()['data']['api_key']}"}
    response = client.post(
        "/api/v1/kbs",
        json={"code": f"private-{uuid.uuid4().hex[:8]}", "name": "private"},
        headers=owner,
    )
    kb_id = response.json()["data"]["id"]
    documents = []
    for title in ("public", "secret"):
        response = client.post(
            f"/api/v1/kbs/{kb_id}/documents",
            json={"title": title, "text": f"contract public secret unique {title}"},
            headers=owner,
        )
        assert response.status_code == 201, response.text
        documents.append(response.json()["data"]["document"]["id"])
    public_id, secret_id = documents
    response = client.post(
        f"/api/v1/kbs/{kb_id}/grants",
        json={"subject_kind": "app", "subject_id": str(app_id), "level": "read"},
        headers=owner,
    )
    assert response.status_code == 201, response.text
    response = client.put(
        f"/api/v1/documents/{secret_id}/access-policy",
        json={"access_policy": "RESTRICTED"}, headers=owner,
    )
    assert response.status_code == 200, response.text
    assert client.get(f"/api/v1/documents/{secret_id}", headers=reader).status_code == 403
    assert client.get(f"/api/v1/documents/{secret_id}", headers=owner).status_code == 200
    assert client.get(f"/api/v1/kbs/{kb_id}", headers=reader).json()["data"]["document_count"] == 1
    listed = client.get(f"/api/v1/kbs/{kb_id}/documents", headers=reader).json()["data"]["items"]
    assert {item["id"] for item in listed} == {public_id}
    assert client.put(
        f"/api/v1/documents/{secret_id}/access-policy",
        json={"access_policy": "INHERIT"}, headers=reader,
    ).status_code == 403

    with SessionLocal() as db:
        chunks = db.query(ChunkMeta).filter(ChunkMeta.document_id.in_(documents)).all()
        chunk_ids = {chunk.document_id: chunk.chunk_id for chunk in chunks}
    monkeypatch.setattr(
        "judicial_ai.services.retrieval.graph_search_or_degraded",
        lambda **kwargs: GraphResult(
            entities=[
                {"name": "public", "chunk_id": chunk_ids[public_id], "document_id": public_id},
                {"name": "secret", "chunk_id": chunk_ids[secret_id], "document_id": secret_id},
            ],
            paths=[
                {"source": "public", "target": "public", "chunk_id": chunk_ids[public_id]},
                {"source": "secret", "target": "secret", "chunk_id": chunk_ids[secret_id]},
            ],
        ),
    )

    def retrieve():
        response = client.post(
            "/v1/retrieve",
            json={"kb_ids": [kb_id], "query": "contract", "top_k": 10, "with_graph": True},
            headers=reader,
        )
        assert response.status_code == 200, response.text
        return response.json()

    body = retrieve()
    assert {item["doc_id"] for item in body["results"]} == {public_id}
    source_url = body["results"][0]["citation"]["source_url"]
    assert client.get(source_url, headers=reader).status_code == 200
    assert [item["name"] for item in body["graph"]["entities"]] == ["public"]
    assert [item["source"] for item in body["graph"]["paths"]] == ["public"]
    for capability_type in ("RAG_RETRIEVE", "GRAPH_RETRIEVE"):
        code = f"acl.{capability_type.lower()}.{uuid.uuid4().hex[:8]}"
        response = client.post(
            "/api/v1/admin/capabilities",
            json={
                "code": code, "name": "ACL test", "type": capability_type, "owner": "test",
                "version": "1.0.0", "protocol": "PLATFORM",
                "request_schema": {"type": "object"}, "response_schema": {"type": "object"},
            },
            headers=admin_headers,
        )
        assert response.status_code == 201, response.text
        assert client.post(
            f"/api/v1/admin/capabilities/{code}/versions/1.0.0/publish", headers=admin_headers
        ).status_code == 200
        assert client.post(
            f"/api/v1/admin/capabilities/{code}/grants",
            json={"app_id": app_id}, headers=admin_headers,
        ).status_code == 201
        response = client.post(
            f"/api/v1/capabilities/{code}/invoke",
            json={"input": {"kb_ids": [kb_id], "query": "contract", "with_graph": True}},
            headers=reader,
        )
        assert response.status_code == 200, response.text
        output = response.json()["output"]
        assert {item["doc_id"] for item in output["results"]} == {public_id}
        assert all(item["name"] != "secret" for item in output["graph"]["entities"])
    response = client.post(
        "/api/v1/contract/review",
        json={"text": "借款合同约定年利率为20%，双方同意。", "kb_ids": [kb_id]}, headers=reader,
    )
    assert response.status_code == 200, response.text
    citations = [
        citation
        for finding in response.json()["data"]["findings"]
        for citation in finding["citations"]
    ]
    assert citations and all(item["document_id"] == public_id for item in citations)

    grant = {"subject_kind": "app", "subject_id": str(app_id), "level": "read"}
    response = client.post(
        f"/api/v1/documents/{secret_id}/grants", json=grant, headers=owner
    )
    assert response.status_code == 201, response.text
    assert {item["doc_id"] for item in retrieve()["results"]} == {public_id, secret_id}
    response = client.post(
        f"/api/v1/documents/{secret_id}/grants",
        json={**grant, "expires_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()},
        headers=owner,
    )
    assert response.status_code == 201, response.text
    assert {item["doc_id"] for item in retrieve()["results"]} == {public_id}
    assert client.delete(
        f"/api/v1/documents/{secret_id}/grants",
        params={"subject_kind": "app", "subject_id": str(app_id)}, headers=owner,
    ).status_code == 200
    assert client.get(f"/api/v1/documents/{secret_id}", headers=reader).status_code == 403
    assert client.put(
        f"/api/v1/documents/{public_id}/access-policy",
        json={"access_policy": "RESTRICTED"}, headers=owner,
    ).status_code == 200
    assert client.get(source_url, headers=reader).status_code == 403
    assert client.get(f"/api/v1/documents/{secret_id}", headers=owner).status_code == 200
    with SessionLocal() as db:
        actions = {
            row.action for row in db.query(AuditLog).filter_by(resource_id=str(secret_id)).all()
        }
        assert {
            "document.access_policy.update", "document.grant.create", "document.grant.revoke"
        } <= actions

    with monkeypatch.context() as patch:
        def fail_audit(*args, **kwargs):
            raise RuntimeError("audit unavailable")

        patch.setattr("judicial_ai.api.retrieval.audit", fail_audit)
        with pytest.raises(RuntimeError, match="audit unavailable"):
            client.post(f"/api/v1/documents/{secret_id}/grants", json=grant, headers=owner)
    with SessionLocal() as db:
        saved = db.query(ResourceGrant).filter_by(
            resource_type="document", resource_id=secret_id, subject_id=str(app_id)
        ).one()
        assert saved.status == "REVOKED"

    assert client.post(
        f"/api/v1/documents/{secret_id}/grants", json=grant, headers=owner
    ).status_code == 201
    for policy in ("INHERIT", "RESTRICTED"):
        assert client.put(
            f"/api/v1/documents/{secret_id}/access-policy",
            json={"access_policy": policy}, headers=owner,
        ).status_code == 200
    assert client.get(f"/api/v1/documents/{secret_id}", headers=reader).status_code == 403
