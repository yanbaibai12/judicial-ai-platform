from __future__ import annotations

import uuid


def test_mcp_tools_list_and_call(client, admin_headers, new_tenant_app):
    suffix = uuid.uuid4().hex[:8]
    tool_name = f"echo.{suffix}"

    response = client.post(
        "/api/v1/admin/mcp-servers",
        json={
            "code": f"mock-{suffix}",
            "name": "Mock 工具服务",
            "transport": "MOCK",
            "tenant_id": 0,
        },
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    server_id = response.json()["data"]["id"]

    response = client.post(
        f"/api/v1/admin/mcp-servers/{server_id}/tools",
        json={
            "name": tool_name,
            "description": "回显参数",
            "input_schema": {
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}},
            },
            "risk_level": "LOW",
            "idempotent": True,
        },
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    tool_id = response.json()["data"]["id"]

    response = client.post(
        f"/api/v1/admin/mcp-tools/{tool_id}/grants",
        json={"app_id": new_tenant_app["app_id"]},
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text

    headers = new_tenant_app["headers"]
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["result"]["protocolVersion"] == "2024-11-05"

    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        headers=headers,
    )
    assert response.status_code == 200
    names = [tool["name"] for tool in response.json()["result"]["tools"]]
    assert tool_name in names

    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": {"query": "测试"}},
        },
        headers=headers,
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is False
    assert "Mock" in result["content"][0]["text"]


def test_mcp_tool_call_validation_and_auth(client, admin_headers, new_tenant_app):
    suffix = uuid.uuid4().hex[:8]
    tool_name = f"strict.{suffix}"
    response = client.post(
        "/api/v1/admin/mcp-servers",
        json={
            "code": f"mock-strict-{suffix}",
            "name": "Strict",
            "transport": "MOCK",
            "tenant_id": 0,
        },
        headers=admin_headers,
    )
    server_id = response.json()["data"]["id"]
    response = client.post(
        f"/api/v1/admin/mcp-servers/{server_id}/tools",
        json={
            "name": tool_name,
            "input_schema": {
                "type": "object",
                "required": ["case_no"],
                "properties": {"case_no": {"type": "string"}},
            },
        },
        headers=admin_headers,
    )
    tool_id = response.json()["data"]["id"]
    client.post(
        f"/api/v1/admin/mcp-tools/{tool_id}/grants",
        json={"app_id": new_tenant_app["app_id"]},
        headers=admin_headers,
    )

    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": {}},
        },
        headers=new_tenant_app["headers"],
    )
    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32602
