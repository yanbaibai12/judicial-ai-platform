from __future__ import annotations

import sys
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

import judicial_ai.security as security
from judicial_ai.app import delivery_configuration_errors
from judicial_ai.config import get_settings
from judicial_ai.services.jobs import run_worker_loop


def _create_kb_with_document(client, headers) -> int:
    code = f"hardening-{uuid.uuid4().hex[:8]}"
    response = client.post(
        "/api/v1/kbs",
        json={"code": code, "name": "生产加固测试库"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    kb_id = response.json()["data"]["id"]
    response = client.post(
        f"/api/v1/kbs/{kb_id}/documents",
        json={
            "title": "民间借贷利率规则",
            "text": "民间借贷利率不得超过一年期贷款市场报价利率四倍。",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return kb_id


def test_security_headers_request_context_and_metrics(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.headers["X-Request-Id"]
    assert response.headers["X-Trace-Id"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["traceparent"].startswith("00-")
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "judicial_http_requests_total" in metrics.text


def test_auth_me_and_invalid_non_ascii_token(client, admin_headers):
    response = client.get("/api/v1/auth/me", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["data"]["kind"] == "admin"
    assert security._constant_time_equal("非ASCII令牌", "dev-admin-token") is False


def test_inline_index_job_and_job_api(client, new_tenant_app):
    kb_id = _create_kb_with_document(client, new_tenant_app["headers"])
    response = client.post(f"/api/v1/kbs/{kb_id}/index-jobs", headers=new_tenant_app["headers"])
    assert response.status_code == 202, response.text
    body = response.json()["data"]
    assert body["status"] == "SUCCEEDED"
    assert body["index"]["status"] == "READY"
    job = client.get(f"/api/v1/jobs/{body['id']}", headers=new_tenant_app["headers"])
    assert job.status_code == 200
    assert job.json()["data"]["status"] == "SUCCEEDED"
    jobs = client.get("/api/v1/jobs", headers=new_tenant_app["headers"])
    assert jobs.status_code == 200
    assert jobs.json()["data"]["total"] >= 1


def test_worker_mode_processes_index_job(client, new_tenant_app, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "job_mode", "worker")
    kb_id = _create_kb_with_document(client, new_tenant_app["headers"])
    response = client.post(f"/api/v1/kbs/{kb_id}/index-jobs", headers=new_tenant_app["headers"])
    assert response.status_code == 202, response.text
    body = response.json()["data"]
    assert body["status"] == "PENDING"
    run_worker_loop(once=True)
    job = client.get(f"/api/v1/jobs/{body['id']}", headers=new_tenant_app["headers"])
    assert job.status_code == 200
    assert job.json()["data"]["status"] == "SUCCEEDED"


@pytest.mark.parametrize("fail", [False, True])
def test_worker_closes_graph_on_exit(monkeypatch, fail):
    from judicial_ai import worker

    closed = []
    monkeypatch.setattr(sys, "argv", ["worker", "--once"])
    monkeypatch.setattr(worker, "close_graph_store", lambda: closed.append(True))

    def run(**kwargs):
        if fail:
            raise RuntimeError("worker failure")

    monkeypatch.setattr(worker, "run_worker_loop", run)
    if fail:
        with pytest.raises(RuntimeError, match="worker failure"):
            worker.main()
    else:
        worker.main()
    assert closed == [True]


def test_graph_index_and_retrieval(client, new_tenant_app, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "graph_enabled", True)
    monkeypatch.setattr(settings, "graph_backend", "sql")
    kb_id = _create_kb_with_document(client, new_tenant_app["headers"])
    index = client.post(f"/api/v1/kbs/{kb_id}/index-jobs", headers=new_tenant_app["headers"])
    assert index.status_code == 202, index.text
    response = client.post(
        "/v1/retrieve",
        json={
            "kb_ids": [kb_id],
            "query": "民间借贷利率上限",
            "top_k": 3,
            "with_graph": True,
        },
        headers=new_tenant_app["headers"],
    )
    assert response.status_code == 200, response.text
    graph = response.json()["graph"]
    assert graph["degraded"] is False
    assert graph["entities"]


def test_oidc_user_principal(client, monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "oidc")
    monkeypatch.setattr(settings, "oidc_issuer", "https://issuer.example")
    monkeypatch.setattr(settings, "oidc_audience", "judicial")
    monkeypatch.setattr(settings, "oidc_admin_roles", "platform-admin")

    class FakeJwkClient:
        def get_signing_key_from_jwt(self, token):
            class Key:
                pass

            key = Key()
            key.key = private_key.public_key()
            return key

    monkeypatch.setattr(security, "_jwk_client", lambda url: FakeJwkClient())
    token = jwt.encode(
        {
            "sub": "user-1",
            "iss": "https://issuer.example",
            "aud": "judicial",
            "exp": int(time.time()) + 300,
            "roles": ["platform-reader"],
            "tenant_id": 7,
        },
        private_key,
        algorithm="RS256",
    )
    response = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["kind"] == "user"
    assert data["tenant_id"] == 7
    assert "kb:read" in data["permissions"]
    kbs = client.get(
        "/api/v1/kbs?tenant_id=1",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert kbs.status_code == 200
    assert all(item["tenant_id"] == 7 for item in kbs.json()["data"]["items"])



def test_staging_rejects_local_degradation(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "staging")
    monkeypatch.setattr(settings, "allow_static_admin", True)
    monkeypatch.setattr(settings, "database_url", "sqlite:///./data/test.db")
    monkeypatch.setattr(settings, "auto_create_schema", True)
    monkeypatch.setattr(settings, "resource_acl_enforced", False)
    monkeypatch.setattr(settings, "inference_mode", "mock")
    monkeypatch.setattr(settings, "index_require_approval", False)
    monkeypatch.setattr(settings, "require_prod_alias", False)
    monkeypatch.setattr(settings, "vector_backend", "sql")
    monkeypatch.setattr(settings, "vector_fallback_sql", True)
    monkeypatch.setattr(settings, "graph_fail_open", True)
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_fail_open", True)
    monkeypatch.setattr(settings, "job_mode", "inline")
    monkeypatch.setattr(settings, "contract_review_enabled", True)
    monkeypatch.setattr(settings, "console_demo_mode", True)

    errors = delivery_configuration_errors(settings)

    expected = {
        "DATABASE_URL",
        "AUTO_CREATE_SCHEMA",
        "RESOURCE_ACL_ENFORCED",
        "INFERENCE_MODE",
        "INDEX_REQUIRE_APPROVAL",
        "REQUIRE_PROD_ALIAS",
        "VECTOR_BACKEND",
        "VECTOR_FALLBACK_SQL",
        "GRAPH_FAIL_OPEN",
        "REDIS_RATE_LIMIT",
        "JOB_MODE",
        "CONTRACT_REVIEW_ENABLED",
        "CONSOLE_DEMO_MODE",
    }
    assert expected <= set(errors)
    # staging 仅允许自动化验收保留静态管理员；prod 时下列测试会单独要求关闭。
    assert "ALLOW_STATIC_ADMIN" not in errors


def test_production_rejects_static_admin(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "allow_static_admin", True)
    errors = delivery_configuration_errors(settings)
    assert "ALLOW_STATIC_ADMIN" in errors
