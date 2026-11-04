from __future__ import annotations

from .conftest import register_chat_capability


def test_register_publish_grant_and_invoke(client, admin_headers, new_tenant_app):
    code = register_chat_capability(client, admin_headers, new_tenant_app["app_id"])

    response = client.post(
        f"/api/v1/capabilities/{code}/invoke",
        json={"input": {"messages": [{"role": "user", "content": "请概括本案争议焦点"}]}},
        headers=new_tenant_app["headers"],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["capability"] == code
    assert body["version"] == "1.0.0"
    assert "Mock" in body["output"]["choices"][0]["message"]["content"]
    assert body["usage"]["total_tokens"] > 0
    assert response.headers["X-Request-Id"]


def test_invoke_without_grant_is_forbidden(client, admin_headers, new_tenant_app):
    code = register_chat_capability(client, admin_headers, new_tenant_app["app_id"])

    response = client.post(
        "/api/v1/admin/tenants",
        json={"code": "other-tenant", "name": "Other"},
        headers=admin_headers,
    )
    assert response.status_code == 201
    tenant_id = response.json()["data"]["id"]
    response = client.post(
        "/api/v1/admin/apps",
        json={"tenant_id": tenant_id, "code": "other-app", "name": "Other App"},
        headers=admin_headers,
    )
    assert response.status_code == 201
    other_app_id = response.json()["data"]["id"]
    response = client.post(f"/api/v1/admin/apps/{other_app_id}/credentials", headers=admin_headers)
    assert response.status_code == 201
    other_key = response.json()["data"]["api_key"]

    response = client.post(
        f"/api/v1/capabilities/{code}/invoke",
        json={"input": {"messages": [{"role": "user", "content": "hi"}]}},
        headers={"Authorization": f"Bearer {other_key}"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_openai_chat_and_stream(client, admin_headers, new_tenant_app):
    code = register_chat_capability(client, admin_headers, new_tenant_app["app_id"])
    payload = {
        "model": code,
        "messages": [{"role": "user", "content": "测试流式"}],
        "temperature": 0.2,
    }
    response = client.post("/v1/chat/completions", json=payload, headers=new_tenant_app["headers"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["role"] == "assistant"

    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={**payload, "stream": True},
        headers=new_tenant_app["headers"],
    ) as stream_response:
        assert stream_response.status_code == 200
        text = "".join(stream_response.iter_text())
    assert "data: [DONE]" in text
    assert "chat.completion.chunk" in text


def test_capability_discovery_and_schema(client, admin_headers, new_tenant_app):
    code = register_chat_capability(client, admin_headers, new_tenant_app["app_id"])
    response = client.get("/api/v1/capabilities", headers=new_tenant_app["headers"])
    assert response.status_code == 200
    codes = [item["code"] for item in response.json()["data"]["items"]]
    assert code in codes

    response = client.get(f"/api/v1/capabilities/{code}/schema", headers=new_tenant_app["headers"])
    assert response.status_code == 200
    assert response.json()["data"]["request_schema"]["required"] == ["messages"]
