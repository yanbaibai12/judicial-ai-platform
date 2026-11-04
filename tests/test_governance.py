from __future__ import annotations

import uuid

from .conftest import register_chat_capability


def test_feedback_bad_case_and_dataset_version(client, admin_headers, new_tenant_app):
    headers = new_tenant_app["headers"]
    code = register_chat_capability(client, admin_headers, new_tenant_app["app_id"])

    response = client.post(
        f"/api/v1/capabilities/{code}/invoke",
        json={"input": {"messages": [{"role": "user", "content": "请总结"}]}},
        headers=headers,
    )
    assert response.status_code == 200
    request_id = response.json()["request_id"]

    response = client.post(
        "/api/v1/feedback",
        json={
            "request_id": request_id,
            "rating": "NEGATIVE",
            "reason": "CITATION_ERROR",
            "comment": "引用不准确",
            "tags": ["citation"],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    feedback_id = response.json()["data"]["id"]
    assert feedback_id

    response = client.post(
        "/api/v1/bad-cases",
        json={
            "request_id": request_id,
            "category": "CITATION",
            "sub_category": "CITATION_NOT_SUPPORT",
            "priority": "P1",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    case_id = response.json()["data"]["id"]

    response = client.patch(
        f"/api/v1/bad-cases/{case_id}",
        json={"status": "CLOSED", "root_cause": "引用片段与结论不匹配"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "CLOSED"

    suffix = uuid.uuid4().hex[:8]
    response = client.post(
        "/api/v1/datasets",
        json={
            "code": f"ds-{suffix}",
            "name": "反馈数据集",
            "type": "SFT",
            "owner": "test",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    dataset_id = response.json()["data"]["id"]

    response = client.post(
        f"/api/v1/datasets/{dataset_id}/versions",
        json={
            "version": "v1.0.0",
            "source_snapshot": {},
            "filter_config": {"rating": ["NEGATIVE"]},
            "split": {"train": 1.0, "validation": 0.0, "test": 0.0},
            "publish": True,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    version = response.json()["data"]
    assert version["stats"]["total"] == 1
    assert version["stats"]["train"] == 1
    assert version["status"] == "PUBLISHED"


def test_usage_and_audit_logs(client, admin_headers, new_tenant_app):
    code = register_chat_capability(client, admin_headers, new_tenant_app["app_id"])
    response = client.post(
        f"/api/v1/capabilities/{code}/invoke",
        json={"input": {"messages": [{"role": "user", "content": "usage"}]}},
        headers=new_tenant_app["headers"],
    )
    assert response.status_code == 200

    response = client.get("/api/v1/usage", headers=new_tenant_app["headers"])
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["total_requests"] >= 1
    assert any(item["capability_code"] == code for item in body["items"])

    response = client.get("/api/v1/audit-logs", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["data"]["total"] >= 1
