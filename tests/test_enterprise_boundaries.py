from __future__ import annotations

import uuid

import pytest

import judicial_ai.api.health as health
from judicial_ai.config import get_settings
from judicial_ai.db import SessionLocal
from judicial_ai.errors import ApiError
from judicial_ai.models import (
    AuditLog,
    ChunkMeta,
    DatasetItem,
    Document,
    EvalTask,
    IndexVersion,
    KnowledgeBase,
    ResourceGrant,
)
from judicial_ai.security import Principal
from judicial_ai.services.governance import _run_retrieval_eval
from judicial_ai.services.inference import get_inference_client
from judicial_ai.services.retrieval import _validate_embeddings, index_kb


def test_kb_acl_covers_document_search_and_grant_management(
    client, new_tenant_app, admin_headers, monkeypatch
):
    monkeypatch.setattr(get_settings(), "resource_acl_enforced", True)
    owner = new_tenant_app["headers"]
    tenant_id = new_tenant_app["tenant_id"]
    response = client.post(
        "/api/v1/admin/apps",
        json={"tenant_id": tenant_id, "code": f"acl-{uuid.uuid4().hex[:8]}", "name": "other"},
        headers=admin_headers,
    )
    assert response.status_code == 201
    app_id = response.json()["data"]["id"]
    response = client.post(f"/api/v1/admin/apps/{app_id}/credentials", headers=admin_headers)
    other = {"Authorization": f"Bearer {response.json()['data']['api_key']}"}

    response = client.post(
        "/api/v1/kbs", json={"code": f"acl-{uuid.uuid4().hex[:8]}", "name": "private"},
        headers=owner,
    )
    assert response.status_code == 201
    kb_id = response.json()["data"]["id"]
    response = client.post(
        f"/api/v1/kbs/{kb_id}/documents",
        json={"title": "private", "text": "private content"},
        headers=owner,
    )
    assert response.status_code == 201
    document_id = response.json()["data"]["document"]["id"]

    assert client.get(f"/api/v1/kbs/{kb_id}", headers=other).status_code == 403
    assert client.get(f"/api/v1/documents/{document_id}", headers=other).status_code == 403
    assert kb_id not in {
        item["id"] for item in client.get("/api/v1/kbs", headers=other).json()["data"]["items"]
    }
    assert client.post(
        "/v1/retrieve", json={"kb_ids": [kb_id], "query": "private"}, headers=other
    ).status_code == 403
    assert client.post(
        "/api/v1/contract/review",
        json={"text": "contract text for review", "kb_ids": [kb_id]},
        headers=other,
    ).status_code == 403

    grant = {"subject_kind": "app", "subject_id": str(app_id), "level": "write"}
    assert client.post(f"/api/v1/kbs/{kb_id}/grants", json=grant, headers=owner).status_code == 201
    assert client.get(f"/api/v1/documents/{document_id}", headers=other).status_code == 200
    assert client.get(f"/api/v1/kbs/{kb_id}/grants", headers=other).status_code == 403
    assert client.get(f"/api/v1/kbs/{kb_id}/grants", headers=owner).status_code == 200
    assert client.post(f"/api/v1/kbs/{kb_id}/grants", json=grant, headers=other).status_code == 403
    with monkeypatch.context() as patch:
        def fail_audit(*args, **kwargs):
            raise RuntimeError("audit unavailable")

        patch.setattr("judicial_ai.api.retrieval.audit", fail_audit)
        with pytest.raises(RuntimeError, match="audit unavailable"):
            client.post(
                f"/api/v1/kbs/{kb_id}/grants",
                json={**grant, "level": "owner"},
                headers=owner,
            )
        with pytest.raises(RuntimeError, match="audit unavailable"):
            client.delete(
                f"/api/v1/kbs/{kb_id}/grants",
                params={"subject_kind": "app", "subject_id": str(app_id)},
                headers=owner,
            )
    with SessionLocal() as db:
        saved = db.query(ResourceGrant).filter_by(
            resource_type="kb", resource_id=kb_id, subject_id=str(app_id)
        ).one()
        assert saved.status == "ACTIVE"
        assert saved.level == "write"
    assert client.delete(
        f"/api/v1/kbs/{kb_id}/grants",
        params={"subject_kind": "app", "subject_id": str(app_id)},
        headers=owner,
    ).status_code == 200
    assert client.get(f"/api/v1/documents/{document_id}", headers=other).status_code == 403
    with SessionLocal() as db:
        actions = {
            row.action for row in db.query(AuditLog).filter_by(resource_id=str(kb_id)).all()
        }
        assert {"kb.grant.create", "kb.grant.revoke"} <= actions


@pytest.mark.parametrize("embeddings", [[], [[1.0], [1.0, 2.0]], [[], []]])
def test_invalid_embeddings_are_rejected(embeddings):
    with pytest.raises(ApiError):
        _validate_embeddings(["a", "b"], embeddings)


def test_partial_embeddings_do_not_commit_document(client, new_tenant_app, monkeypatch):
    headers = new_tenant_app["headers"]
    response = client.post(
        "/api/v1/kbs",
        json={"code": f"embed-{uuid.uuid4().hex[:8]}", "name": "test"},
        headers=headers,
    )
    kb_id = response.json()["data"]["id"]
    monkeypatch.setattr(get_inference_client(), "embed", lambda *args: [])
    response = client.post(
        f"/api/v1/kbs/{kb_id}/documents",
        json={"title": "broken", "text": "document text"},
        headers=headers,
    )
    assert response.status_code == 503
    with SessionLocal() as db:
        assert db.query(Document).filter_by(kb_id=kb_id).count() == 0
        assert db.query(IndexVersion).filter_by(kb_id=kb_id).count() == 0


def test_failed_index_preserves_existing_chunk_index(client, new_tenant_app, monkeypatch):
    headers = new_tenant_app["headers"]
    response = client.post(
        "/api/v1/kbs",
        json={"code": f"index-{uuid.uuid4().hex[:8]}", "name": "test"},
        headers=headers,
    )
    kb_id = response.json()["data"]["id"]
    response = client.post(
        f"/api/v1/kbs/{kb_id}/documents",
        json={"title": "existing", "text": "document text"},
        headers=headers,
    )
    assert response.status_code == 201
    with SessionLocal() as db:
        previous_index_id = db.query(ChunkMeta).filter_by(kb_id=kb_id).first().index_version_id
        assert previous_index_id is not None
        monkeypatch.setattr(get_inference_client(), "embed", lambda *args: [])
        with pytest.raises(ApiError):
            index_kb(db, db.get(KnowledgeBase, kb_id))
        db.expire_all()
        chunk = db.query(ChunkMeta).filter_by(kb_id=kb_id).first()
        assert chunk.index_version_id == previous_index_id
        assert db.query(IndexVersion).filter_by(kb_id=kb_id, status="FAILED").count() == 1


def test_readiness_fails_closed_when_required_redis_is_unavailable(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_fail_open", False)
    monkeypatch.setattr(settings, "redis_url", "redis://unavailable")
    monkeypatch.setattr(health, "_check_redis", lambda: {"status": "error"})
    response = client.get("/readyz")
    assert response.status_code == 503
    assert "redis" in response.json()["critical_failures"]
    monkeypatch.setattr(settings, "rate_limit_fail_open", True)
    assert client.get("/readyz").status_code == 200


def test_retrieval_evaluation_checks_current_kb_grants(client, new_tenant_app, monkeypatch):
    monkeypatch.setattr(get_settings(), "resource_acl_enforced", True)
    response = client.post(
        "/api/v1/kbs",
        json={"code": f"eval-{uuid.uuid4().hex[:8]}", "name": "test"},
        headers=new_tenant_app["headers"],
    )
    kb_id = response.json()["data"]["id"]
    principal = Principal(kind="app", tenant_id=new_tenant_app["tenant_id"], app_id=999999)
    task = EvalTask(tenant_id=new_tenant_app["tenant_id"])
    item = DatasetItem(input={"query": "private", "kb_ids": [kb_id]}, label={})
    with SessionLocal() as db, pytest.raises(ApiError) as error:
        _run_retrieval_eval(db, task, [item], {}, principal)
    assert error.value.status_code == 403
