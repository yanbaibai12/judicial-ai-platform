from __future__ import annotations


def test_seed_demo_chat_and_retrieval(client, seeded, demo_headers):
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "llm.qwen3.chat",
            "messages": [{"role": "user", "content": "请介绍民间借贷利率规则"}],
        },
        headers=demo_headers,
    )
    assert response.status_code == 200, response.text
    assert "Mock" in response.json()["choices"][0]["message"]["content"]

    response = client.post(
        "/v1/retrieve",
        json={
            "kb_ids": [seeded["kb_id"]],
            "query": "民间借贷利率超过多少不受保护",
            "top_k": 3,
        },
        headers=demo_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["results"]
    assert body["results"][0]["citation"]["title"]
    assert body["graph"]["degraded"] is True


def test_seed_demo_mcp_tool(client, seeded, demo_headers):
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": seeded["mcp_tool"],
                "arguments": {"case_no": "（2026）京0101民初123号"},
            },
        },
        headers=demo_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["result"]["isError"] is False
