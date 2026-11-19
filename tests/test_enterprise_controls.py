from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest

import judicial_ai.api.health as health
import judicial_ai.api.retrieval as retrieval_api
from judicial_ai.config import get_settings
from judicial_ai.db import SessionLocal
from judicial_ai.models import (
    AuditLog,
    DocumentVersion,
    IndexChunkSnapshot,
    IndexVersion,
    KnowledgeBase,
)
from judicial_ai.services import retrieval
from judicial_ai.services.object_store import (
    FilesystemObjectStore,
    export_backup,
    restore_backup,
)


def _create_kb(client, headers, code: str | None = None) -> int:
    response = client.post(
        "/api/v1/kbs",
        json={"code": code or f"enterprise-{uuid.uuid4().hex[:8]}", "name": "enterprise"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


def test_original_file_download_delete_restore_and_purge(
    client, new_tenant_app, admin_headers
):
    headers = new_tenant_app["headers"]
    kb_id = _create_kb(client, headers)
    content = b"original binary evidence\nline two"
    response = client.post(
        f"/api/v1/kbs/{kb_id}/documents/upload",
        files={"file": ("evidence.txt", content, "text/plain")},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    document_id = response.json()["data"]["document"]["id"]
    version_id = response.json()["data"]["document_version_id"]

    with SessionLocal() as db:
        version = db.get(DocumentVersion, version_id)
        assert version is not None
        assert version.content_hash == hashlib.sha256(content).hexdigest()
        assert version.object_key
        assert version.object_size == len(content)
        assert version.storage_status == "AVAILABLE"

    download = client.get(
        f"/api/v1/documents/{document_id}/download",
        params={"version": version_id},
        headers=headers,
    )
    assert download.status_code == 200, download.text
    assert download.content == content
    assert download.headers["x-content-sha256"] == hashlib.sha256(content).hexdigest()

    deleted = client.delete(f"/api/v1/documents/{document_id}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert client.get(
        f"/api/v1/documents/{document_id}/download", headers=headers
    ).status_code == 404

    restored = client.post(
        f"/api/v1/documents/{document_id}/restore", headers=headers
    )
    assert restored.status_code == 200, restored.text
    assert client.get(
        f"/api/v1/documents/{document_id}/download", headers=headers
    ).content == content

    assert client.delete(
        f"/api/v1/documents/{document_id}", headers=headers
    ).status_code == 200
    purged = client.delete(
        f"/api/v1/documents/{document_id}",
        params={"purge": "true", "force": "true"},
        headers=admin_headers,
    )
    assert purged.status_code == 200, purged.text
    with SessionLocal() as db:
        version = db.get(DocumentVersion, version_id)
        assert version.storage_status == "PURGED"
        assert version.object_key == ""


def test_original_file_purge_failure_is_persisted(
    client, new_tenant_app, admin_headers, monkeypatch
):
    headers = new_tenant_app["headers"]
    kb_id = _create_kb(client, headers)
    response = client.post(
        f"/api/v1/kbs/{kb_id}/documents/upload",
        files={"file": ("evidence.txt", b"purge failure", "text/plain")},
        headers=headers,
    )
    version_id = response.json()["data"]["document_version_id"]
    document_id = response.json()["data"]["document"]["id"]
    assert client.delete(
        f"/api/v1/documents/{document_id}", headers=headers
    ).status_code == 200

    class FailingObjectStore:
        def delete(self, key):
            raise RuntimeError(f"cannot delete {key}")

    monkeypatch.setattr(
        retrieval_api, "get_object_store", lambda: FailingObjectStore()
    )
    failed = client.delete(
        f"/api/v1/documents/{document_id}",
        params={"purge": "true", "force": "true"},
        headers=admin_headers,
    )
    assert failed.status_code == 503, failed.text
    with SessionLocal() as db:
        assert db.get(DocumentVersion, version_id).storage_status == "PURGE_FAILED"


def test_filesystem_object_backup_and_restore(tmp_path: Path):
    source = FilesystemObjectStore(tmp_path / "source")
    source.put("tenant-1/a.txt", b"one", "text/plain")
    source.put("tenant-1/nested/b.bin", b"\x00\x01", "application/octet-stream")
    manifest = export_backup(source, tmp_path / "backup")
    assert len(manifest["objects"]) == 2

    target = FilesystemObjectStore(tmp_path / "target")
    result = restore_backup(target, tmp_path / "backup")
    assert result == {"restored": 2, "skipped": 0}
    assert target.get("tenant-1/a.txt") == b"one"
    assert target.get("tenant-1/nested/b.bin") == b"\x00\x01"


def test_application_permissions_are_explicit_and_reload_without_new_key(
    client, admin_headers
):
    suffix = uuid.uuid4().hex[:8]
    tenant = client.post(
        "/api/v1/admin/tenants",
        json={"code": f"scope-{suffix}", "name": "scope"},
        headers=admin_headers,
    ).json()["data"]
    app_response = client.post(
        "/api/v1/admin/apps",
        json={"tenant_id": tenant["id"], "code": f"scope-{suffix}", "name": "scope"},
        headers=admin_headers,
    )
    assert app_response.status_code == 201
    app = app_response.json()["data"]
    assert app["permissions"] == ["app:invoke"]
    credential = client.post(
        f"/api/v1/admin/apps/{app['id']}/credentials", headers=admin_headers
    ).json()["data"]
    headers = {"Authorization": f"Bearer {credential['api_key']}"}
    assert client.post(
        "/api/v1/kbs",
        json={"code": f"denied-{suffix}", "name": "denied"},
        headers=headers,
    ).status_code == 403

    updated = client.put(
        f"/api/v1/admin/apps/{app['id']}/permissions",
        json={"permissions": ["app:invoke", "kb:read", "kb:write"]},
        headers=admin_headers,
    )
    assert updated.status_code == 200, updated.text
    assert client.post(
        "/api/v1/kbs",
        json={"code": f"allowed-{suffix}", "name": "allowed"},
        headers=headers,
    ).status_code == 201


def test_index_approval_and_explicit_prod_alias(
    client, new_tenant_app, admin_headers, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "index_require_approval", True)
    monkeypatch.setattr(settings, "require_prod_alias", True)
    kb_id = _create_kb(client, new_tenant_app["headers"])
    response = client.post(
        f"/api/v1/kbs/{kb_id}/documents",
        json={"title": "approval", "text": "approval workflow content"},
        headers=new_tenant_app["headers"],
    )
    assert response.status_code == 201, response.text
    with SessionLocal() as db:
        index = db.query(IndexVersion).filter_by(kb_id=kb_id).one()
        assert index.status == "PENDING_APPROVAL"
        index_id = index.id
        version = db.get(retrieval.KbVersion, index.kb_version_id)
        version_name = version.version
        index_name = index.version

    denied = client.post(
        "/v1/retrieve",
        json={"kb_ids": [kb_id], "query": "approval"},
        headers=new_tenant_app["headers"],
    )
    assert denied.status_code == 404
    approved = client.post(
        f"/api/v1/kbs/{kb_id}/indexes/{index_id}/approve",
        json={"reason": "quality gate passed"},
        headers=admin_headers,
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["data"]["status"] == "READY"
    alias = client.post(
        f"/api/v1/kbs/{kb_id}/aliases",
        json={"alias": "prod", "kb_version": version_name, "index_version": index_name},
        headers=admin_headers,
    )
    assert alias.status_code == 200, alias.text
    retrieved = client.post(
        "/v1/retrieve",
        json={"kb_ids": [kb_id], "query": "approval"},
        headers=new_tenant_app["headers"],
    )
    assert retrieved.status_code == 200, retrieved.text
    assert retrieved.json()["results"]


def test_prod_alias_requires_resource_owner(
    client, new_tenant_app, admin_headers, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "resource_acl_enforced", True)
    owner_headers = new_tenant_app["headers"]
    kb_id = _create_kb(client, owner_headers)
    response = client.post(
        f"/api/v1/kbs/{kb_id}/documents",
        json={"title": "owner gate", "text": "production alias owner gate"},
        headers=owner_headers,
    )
    assert response.status_code == 201, response.text
    with SessionLocal() as db:
        index = db.query(IndexVersion).filter_by(kb_id=kb_id, status="READY").one()
        version = db.get(retrieval.KbVersion, index.kb_version_id)

    suffix = uuid.uuid4().hex[:8]
    app = client.post(
        "/api/v1/admin/apps",
        json={
            "tenant_id": new_tenant_app["tenant_id"],
            "code": f"promoter-{suffix}",
            "name": "promoter",
            "permissions": ["app:invoke", "kb:promote"],
        },
        headers=admin_headers,
    ).json()["data"]
    credential = client.post(
        f"/api/v1/admin/apps/{app['id']}/credentials", headers=admin_headers
    ).json()["data"]
    promoter_headers = {"Authorization": f"Bearer {credential['api_key']}"}
    grant = client.post(
        f"/api/v1/kbs/{kb_id}/grants",
        json={"subject_kind": "app", "subject_id": str(app["id"]), "level": "write"},
        headers=owner_headers,
    )
    assert grant.status_code == 201, grant.text

    denied = client.post(
        f"/api/v1/kbs/{kb_id}/aliases",
        json={
            "alias": "prod",
            "kb_version": version.version,
            "index_version": index.version,
        },
        headers=promoter_headers,
    )
    assert denied.status_code == 403, denied.text


def test_partial_vector_failure_triggers_cleanup(
    client, new_tenant_app, monkeypatch
):
    kb_id = _create_kb(client, new_tenant_app["headers"])
    client.post(
        f"/api/v1/kbs/{kb_id}/documents",
        json={"title": "cleanup", "text": "cleanup content"},
        headers=new_tenant_app["headers"],
    )
    deleted: list[int] = []

    class PartialVectorStore:
        def index_chunks(self, **kwargs):
            raise RuntimeError("partial write")

        def delete_index(self, index_version_id):
            deleted.append(index_version_id)

    settings = get_settings()
    monkeypatch.setattr(settings, "vector_backend", "milvus")
    monkeypatch.setattr(settings, "vector_fallback_sql", False)
    monkeypatch.setattr(retrieval, "get_vector_store", lambda: PartialVectorStore())
    with SessionLocal() as db:
        kb = db.get(KnowledgeBase, kb_id)
        with pytest.raises(RuntimeError, match="partial write"):
            retrieval.index_kb(db, kb)
        failed = (
            db.query(IndexVersion)
            .filter_by(kb_id=kb_id, status="FAILED")
            .order_by(IndexVersion.created_at.desc())
            .first()
        )
        assert failed.id in deleted
        assert failed.cleanup_status == "COMPLETED"


def test_readiness_matches_approved_degradation_policy(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(health, "_check_database", lambda: {"status": "ok"})
    monkeypatch.setattr(health, "_check_redis", lambda: {"status": "disabled"})
    monkeypatch.setattr(health, "_check_vector", lambda: {"status": "error"})
    monkeypatch.setattr(health, "_check_graph", lambda: {"status": "error"})
    monkeypatch.setattr(health, "_check_inference", lambda: {"status": "ok"})
    monkeypatch.setattr(settings, "vector_backend", "milvus")
    monkeypatch.setattr(settings, "vector_fallback_sql", True)
    monkeypatch.setattr(settings, "graph_enabled", True)
    monkeypatch.setattr(settings, "graph_fail_open", True)
    assert client.get("/readyz").status_code == 200

    monkeypatch.setattr(settings, "vector_fallback_sql", False)
    response = client.get("/readyz")
    assert response.status_code == 503
    assert "vector" in response.json()["critical_failures"]
    monkeypatch.setattr(settings, "vector_fallback_sql", True)
    monkeypatch.setattr(settings, "graph_fail_open", False)
    response = client.get("/readyz")
    assert response.status_code == 503
    assert "graph" in response.json()["critical_failures"]


def test_index_reconcile_and_recovery(client, new_tenant_app, admin_headers):
    headers = new_tenant_app["headers"]
    kb_id = _create_kb(client, headers)
    response = client.post(
        f"/api/v1/kbs/{kb_id}/documents",
        json={"title": "reconcile", "text": "reconcile frozen content"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    with SessionLocal() as db:
        index = db.query(IndexVersion).filter_by(kb_id=kb_id, status="READY").one()
        index_id = index.id
        snapshot = db.query(IndexChunkSnapshot).filter_by(
            index_version_id=index_id
        ).first()
        db.delete(snapshot)
        db.commit()

    report = client.post(
        "/api/v1/admin/indexes/reconcile",
        params={"kb_id": kb_id, "repair": "false"},
        headers=admin_headers,
    )
    assert report.status_code == 200, report.text
    assert report.json()["data"]["issues"][0]["reasons"] == [
        "snapshot_count_mismatch"
    ]
    repaired = client.post(
        "/api/v1/admin/indexes/reconcile",
        params={"kb_id": kb_id, "repair": "true"},
        headers=admin_headers,
    )
    assert repaired.status_code == 200, repaired.text
    with SessionLocal() as db:
        assert db.get(IndexVersion, index_id).status == "FAILED"

    recovered = client.post(
        f"/api/v1/kbs/{kb_id}/indexes/{index_id}/recover",
        headers=headers,
    )
    assert recovered.status_code == 201, recovered.text
    assert recovered.json()["data"]["previous"]["status"] == "SUPERSEDED"
    assert recovered.json()["data"]["replacement"]["status"] == "READY"


def test_index_reconcile_records_backend_errors(
    client, admin_headers, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "vector_backend", "milvus")

    class UnavailableVectorStore:
        def list_index_ids(self):
            raise RuntimeError("milvus unavailable")

    monkeypatch.setattr(retrieval, "get_vector_store", lambda: UnavailableVectorStore())
    response = client.post(
        "/api/v1/admin/indexes/reconcile",
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    # 依赖异常文本不外泄，只保留稳定的错误类别（见 docs/15 探测面加固）。
    assert response.json()["data"]["backend_errors"] == {"vector": "RuntimeError"}
    with SessionLocal() as db:
        record = (
            db.query(AuditLog)
            .filter_by(action="index.reconcile")
            .order_by(AuditLog.created_at.desc())
            .first()
        )
        assert record.after["backend_errors"] == {"vector": "RuntimeError"}
