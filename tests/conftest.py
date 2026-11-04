from __future__ import annotations

import os
import tempfile
import uuid

_TEST_DIR = tempfile.mkdtemp(prefix="judicial-ai-tests-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DIR}/test.db"
os.environ["DATA_DIR"] = _TEST_DIR
os.environ["ADMIN_TOKEN"] = "test-admin"
os.environ["INFERENCE_MODE"] = "mock"
os.environ["RATE_LIMIT_ENABLED"] = "false"
os.environ["GRAPH_ENABLED"] = "false"
os.environ["EMBEDDING_DIM"] = "128"

import pytest
from fastapi.testclient import TestClient

from judicial_ai.app import create_app
from judicial_ai.db import init_db
from judicial_ai.seed import seed_demo


@pytest.fixture(scope="session")
def client():
    init_db()
    application = create_app()
    with TestClient(application) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def seeded(client):
    return seed_demo()


@pytest.fixture(scope="session")
def admin_headers():
    return {"Authorization": "Bearer test-admin"}


@pytest.fixture(scope="session")
def demo_headers(seeded):
    return {"Authorization": f"Bearer {seeded['api_key']}"}


@pytest.fixture
def new_tenant_app(client, admin_headers):
    suffix = uuid.uuid4().hex[:8]
    tenant_code = f"t-{suffix}"
    response = client.post(
        "/api/v1/admin/tenants",
        json={"code": tenant_code, "name": tenant_code},
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    tenant_id = response.json()["data"]["id"]

    app_code = f"app-{suffix}"
    response = client.post(
        "/api/v1/admin/apps",
        json={"tenant_id": tenant_id, "code": app_code, "name": app_code},
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    app_id = response.json()["data"]["id"]

    response = client.post(f"/api/v1/admin/apps/{app_id}/credentials", headers=admin_headers)
    assert response.status_code == 201, response.text
    api_key = response.json()["data"]["api_key"]

    return {
        "tenant_id": tenant_id,
        "app_id": app_id,
        "api_key": api_key,
        "headers": {"Authorization": f"Bearer {api_key}"},
    }


def register_chat_capability(
    client,
    admin_headers,
    app_id: int,
    code: str | None = None,
    adapter_alias: str | None = None,
) -> str:
    code = code or f"llm.test.{uuid.uuid4().hex[:8]}"
    response = client.post(
        "/api/v1/admin/capabilities",
        json={
            "code": code,
            "name": "测试对话能力",
            "type": "MODEL_CHAT",
            "owner": "test",
            "version": "1.0.0",
            "protocol": "OPENAI",
            "model_name": "Qwen3-8B",
            "adapter_alias": adapter_alias,
            "request_schema": {
                "type": "object",
                "required": ["messages"],
                "properties": {"messages": {"type": "array"}},
            },
            "response_schema": {"type": "object"},
            "tags": ["test"],
        },
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    response = client.post(
        f"/api/v1/admin/capabilities/{code}/versions/1.0.0/publish",
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    response = client.post(
        f"/api/v1/admin/capabilities/{code}/grants",
        json={"app_id": app_id},
        headers=admin_headers,
    )
    assert response.status_code in {200, 201}, response.text
    return code
