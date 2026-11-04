from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import uuid

import pytest

from judicial_ai.api.jobs import _job_for, _list_jobs
from judicial_ai.config import get_settings
from judicial_ai.db import SessionLocal
from judicial_ai.errors import ApiError
from judicial_ai.ids import new_id
from judicial_ai.models import (
    App,
    AppCredential,
    Dataset,
    DatasetVersion,
    EvalTask,
    Job,
    ResourceGrant,
    Tenant,
)
from judicial_ai.security import Principal, hash_token
from judicial_ai.services.jobs import _execute_running, enqueue_job


def test_index_job_isolation_and_idempotency(client, new_tenant_app, admin_headers, monkeypatch):
    monkeypatch.setattr(get_settings(), "resource_acl_enforced", True)
    monkeypatch.setattr(get_settings(), "job_mode", "worker")
    owner = new_tenant_app["headers"]
    tenant_id = new_tenant_app["tenant_id"]
    suffix = uuid.uuid4().hex[:8]
    response = client.post(
        "/api/v1/admin/apps",
        json={"tenant_id": tenant_id, "code": f"job-other-{suffix}", "name": "other"},
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    app_id = response.json()["data"]["id"]
    response = client.post(f"/api/v1/admin/apps/{app_id}/credentials", headers=admin_headers)
    other = {"Authorization": f"Bearer {response.json()['data']['api_key']}"}
    kb_ids = []
    for number in range(2):
        response = client.post(
            "/api/v1/kbs",
            json={"code": f"job-kb-{suffix}-{number}", "name": "test"},
            headers=owner,
        )
        assert response.status_code == 201, response.text
        kb_ids.append(response.json()["data"]["id"])
    kb_id, second_kb_id = kb_ids
    assert client.post(
        f"/api/v1/kbs/{kb_id}/grants",
        json={"subject_kind": "app", "subject_id": str(app_id), "level": "write"},
        headers=owner,
    ).status_code == 201

    def create(kb, headers):
        return client.post(
            f"/api/v1/kbs/{kb}/index-jobs",
            headers={**headers, "Idempotency-Key": "same-key"},
        )

    first = create(kb_id, owner)
    same = create(kb_id, owner)
    second = create(kb_id, other)
    assert first.status_code == same.status_code == second.status_code == 202
    owner_job = first.json()["data"]["id"]
    other_job = second.json()["data"]["id"]
    assert same.json()["data"]["id"] == owner_job
    assert other_job != owner_job
    assert create(second_kb_id, owner).status_code == 409

    assert client.get(f"/api/v1/jobs/{other_job}", headers=owner).status_code == 404
    assert client.post(f"/api/v1/jobs/{owner_job}/cancel", headers=other).status_code == 404
    other_list = client.get("/api/v1/jobs", headers=other).json()["data"]["items"]
    assert other_job in {item["id"] for item in other_list}
    assert owner_job not in {item["id"] for item in other_list}
    response = client.post(
        f"/api/v1/kbs/{kb_id}/documents",
        json={"title": "restricted", "text": f"document {suffix}"},
        headers=other,
    )
    assert response.status_code == 201, response.text
    document_id = response.json()["data"]["document"]["id"]
    document_job = response.json()["data"]["index_job_id"]
    assert client.get(f"/api/v1/jobs/{document_job}", headers=other).status_code == 200
    assert client.put(
        f"/api/v1/documents/{document_id}/access-policy",
        json={"access_policy": "RESTRICTED"},
        headers=owner,
    ).status_code == 200
    assert client.get(f"/api/v1/jobs/{document_job}", headers=other).status_code == 404
    assert client.delete(
        f"/api/v1/kbs/{kb_id}/grants",
        params={"subject_kind": "app", "subject_id": str(app_id)},
        headers=owner,
    ).status_code == 200
    assert client.get(f"/api/v1/jobs/{other_job}", headers=other).status_code == 404
    assert client.post(f"/api/v1/jobs/{other_job}/cancel", headers=other).status_code == 404
    other_list = client.get("/api/v1/jobs", headers=other).json()["data"]["items"]
    assert other_job not in {item["id"] for item in other_list}
    assert client.get(f"/api/v1/jobs/{other_job}", headers=admin_headers).status_code == 200
    for job_id in (owner_job, other_job, document_job):
        response = client.post(f"/api/v1/jobs/{job_id}/cancel", headers=admin_headers)
        assert response.status_code == 200


def test_user_ownership_and_legacy_jobs_fail_closed(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "job_mode", "worker")
    suffix = uuid.uuid4().hex
    tenant_id = 987654
    user = Principal(kind="user", tenant_id=tenant_id, user_id=f"u-{suffix}")
    other = Principal(kind="user", tenant_id=tenant_id, user_id=f"v-{suffix}")
    app = Principal(kind="app", tenant_id=tenant_id, app_id=987655)
    admin = Principal(kind="admin")
    with SessionLocal() as db:
        job = enqueue_job(db, "retry", {"value": 1}, tenant_id=tenant_id, principal=user)
        legacy_app = Job(
            id=f"job-legacy-app-{suffix}", job_type="retry", tenant_id=tenant_id,
            app_id=app.app_id, payload={},
        )
        legacy_unknown = Job(
            id=f"job-legacy-user-{suffix}", job_type="retry", tenant_id=tenant_id, payload={},
        )
        db.add_all([legacy_app, legacy_unknown])
        db.commit()
        assert _job_for(db, user, job.id).id == job.id
        assert _job_for(db, app, legacy_app.id).id == legacy_app.id
        assert _job_for(db, admin, legacy_unknown.id).id == legacy_unknown.id
        for principal, job_id in ((other, job.id), (user, legacy_unknown.id), (app, job.id)):
            with pytest.raises(ApiError) as exc:
                _job_for(db, principal, job_id)
            assert exc.value.status_code == 404
        listed = _list_jobs(
            db, user, status=None, job_type=None, tenant_id=None, limit=100
        )
        assert {item.id for item in listed} == {job.id}
        for item in (job, legacy_app, legacy_unknown):
            item.status = "CANCELLED"
        db.commit()


def test_job_creator_migration_preserves_existing_jobs(tmp_path):
    database = tmp_path / "legacy.db"
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{database.as_posix()}"
    env["DATA_DIR"] = str(tmp_path)
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "0006_kb_version_snapshot"],
        env=env, check=True, capture_output=True,
    )
    with sqlite3.connect(database) as conn:
        # The baseline creates current metadata, so reconstruct the pre-migration columns.
        conn.execute("ALTER TABLE job DROP COLUMN creator_kind")
        conn.execute("ALTER TABLE job DROP COLUMN creator_id")
        conn.execute("ALTER TABLE job DROP COLUMN creator_credential_id")
        conn.execute(
            "INSERT INTO job (id, job_type, status, tenant_id, payload, attempts, "
            "max_attempts, priority, scheduled_at, created_at, updated_at) "
            "VALUES ('legacy', 'retry', 'PENDING', 1, '{}', 0, 3, 100, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=env, check=True, capture_output=True,
    )
    with sqlite3.connect(database) as conn:
        row = conn.execute(
            "SELECT id, creator_kind, creator_id, creator_credential_id FROM job"
        ).fetchone()
    assert row == ("legacy", None, None, None)


@pytest.mark.parametrize("changed", ["credential", "grant", "app", "tenant", "none"])
def test_worker_revalidates_app_authorization(client, new_tenant_app, monkeypatch, changed):
    settings = get_settings()
    monkeypatch.setattr(settings, "resource_acl_enforced", True)
    monkeypatch.setattr(settings, "job_mode", "worker")
    headers = new_tenant_app["headers"]
    response = client.post(
        "/api/v1/kbs",
        json={"code": f"worker-auth-{uuid.uuid4().hex[:8]}", "name": "test"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    kb_id = response.json()["data"]["id"]
    response = client.post(f"/api/v1/kbs/{kb_id}/index-jobs", headers=headers)
    assert response.status_code == 202, response.text
    job_id = response.json()["data"]["id"]
    calls = []
    monkeypatch.setattr(
        "judicial_ai.services.retrieval.index_kb",
        lambda *args, **kwargs: calls.append(True) or object(),
    )
    monkeypatch.setattr("judicial_ai.services.retrieval.index_to_dict", lambda index: {})
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job.creator_credential_id is not None
        if changed == "credential":
            credential = db.query(AppCredential).filter_by(
                key_hash=hash_token(new_tenant_app["api_key"])
            ).one()
            credential.status = "REVOKED"
        elif changed == "grant":
            grant = db.query(ResourceGrant).filter_by(
                resource_type="kb", resource_id=kb_id,
                subject_kind="app", subject_id=str(new_tenant_app["app_id"]),
            ).one()
            grant.status = "REVOKED"
        elif changed == "app":
            db.get(App, new_tenant_app["app_id"]).status = "DISABLED"
        elif changed == "tenant":
            db.get(Tenant, new_tenant_app["tenant_id"]).status = "DISABLED"
        job.status = "RUNNING"
        job.attempts = 1
        db.commit()
        result = _execute_running(db, job, from_worker=True)
        assert result.status == ("SUCCEEDED" if changed == "none" else "FAILED")
        assert bool(calls) == (changed == "none")
        if changed != "none":
            assert result.finished_at is not None


@pytest.mark.parametrize("kind", ["user", "admin", None])
def test_worker_fails_closed_for_unverifiable_or_legacy_identity(client, monkeypatch, kind):
    monkeypatch.setattr(get_settings(), "resource_acl_enforced", True)
    with SessionLocal() as db:
        job = Job(
            id=f"job-unverified-{uuid.uuid4().hex}", job_type="kb_index", tenant_id=987654,
            creator_kind=kind, creator_id="unknown", payload={"kb_id": 1},
            status="RUNNING", attempts=1, max_attempts=3,
        )
        db.add(job)
        db.commit()
        result = _execute_running(db, job, from_worker=True)
        assert result.status == "FAILED"
        assert "无法重新验证" in result.error


def test_worker_rejects_expired_credentials_before_eval(client, new_tenant_app, monkeypatch):
    from datetime import timedelta

    from judicial_ai.models import utcnow

    monkeypatch.setattr(get_settings(), "job_mode", "worker")
    with SessionLocal() as db:
        credential = db.query(AppCredential).filter_by(
            key_hash=hash_token(new_tenant_app["api_key"])
        ).one()
        credential.expires_at = utcnow() - timedelta(seconds=1)
        task = EvalTask(
            id=new_id(), tenant_id=new_tenant_app["tenant_id"], dataset_version_id=1,
            target_type="MODEL", target_version="test", status="PENDING",
        )
        db.add(task)
        job = Job(
            id=f"job-expired-{uuid.uuid4().hex}", job_type="eval",
            tenant_id=new_tenant_app["tenant_id"], app_id=new_tenant_app["app_id"],
            creator_kind="app", creator_id=str(new_tenant_app["app_id"]),
            creator_credential_id=credential.id, payload={"eval_task_id": task.id},
            status="RUNNING", attempts=1, max_attempts=3,
        )
        db.add(job)
        db.commit()
        result = _execute_running(db, job, from_worker=True)
        assert result.status == "FAILED"
        assert "凭证已失效" in result.error
        db.refresh(task)
        assert task.status == "FAILED"
        assert task.finished_at is not None


def test_worker_does_not_report_failed_eval_as_succeeded(client, new_tenant_app, monkeypatch):
    def fail_eval(db, task, dataset, version, principal):
        task.status = "FAILED"
        task.error = "evaluation failed"
        return []

    monkeypatch.setattr("judicial_ai.services.governance._run_eval_task", fail_eval)
    with SessionLocal() as db:
        tenant_id = new_tenant_app["tenant_id"]
        credential = db.query(AppCredential).filter_by(
            key_hash=hash_token(new_tenant_app["api_key"])
        ).one()
        dataset = Dataset(
            id=new_id(), tenant_id=tenant_id, code=f"d-{uuid.uuid4().hex[:8]}",
            name="test", type="EVAL",
        )
        version = DatasetVersion(id=new_id(), dataset_id=dataset.id, version="1")
        task = EvalTask(
            id=new_id(), tenant_id=tenant_id, dataset_version_id=version.id,
            target_type="MODEL", target_version="test", status="PENDING",
        )
        job = Job(
            id=f"job-eval-failure-{uuid.uuid4().hex}", job_type="eval",
            tenant_id=tenant_id, app_id=new_tenant_app["app_id"],
            creator_kind="app", creator_id=str(new_tenant_app["app_id"]),
            creator_credential_id=credential.id, payload={"eval_task_id": task.id},
            status="RUNNING", attempts=1, max_attempts=3,
        )
        db.add_all([dataset, version, task, job])
        db.commit()
        result = _execute_running(db, job, from_worker=True)
        assert result.status == "FAILED"
        assert result.attempts == 1
        db.refresh(task)
        assert task.status == "FAILED"
