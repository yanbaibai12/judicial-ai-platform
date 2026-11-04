from __future__ import annotations

import httpx

from judicial_ai import Client, JudicialAIError


def test_sync_sdk_chat_and_retrieve():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
        if request.url.path == "/v1/retrieve":
            return httpx.Response(200, json={"results": [], "graph": {"degraded": True}})
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "missing"}})

    with Client(
        base_url="http://testserver",
        api_key="ak_test",
        transport=httpx.MockTransport(handler),
    ) as client:
        chat = client.chat("model", [{"role": "user", "content": "hello"}])
        assert chat["choices"][0]["message"]["content"] == "ok"
        retrieved = client.retrieve([1], "query")
        assert retrieved["results"] == []


def test_sdk_preserves_request_id_on_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"X-Request-Id": "req-sdk"},
            json={
                "error": {
                    "code": "FORBIDDEN",
                    "message": "no grant",
                    "request_id": "req-sdk",
                }
            },
        )

    with Client(
        base_url="http://testserver",
        api_key="ak_test",
        max_retries=0,
        transport=httpx.MockTransport(handler),
    ) as client:
        try:
            client.retrieve([1], "query")
        except JudicialAIError as exc:
            assert exc.code == "FORBIDDEN"
            assert exc.request_id == "req-sdk"
            assert exc.status_code == 403
        else:
            raise AssertionError("expected JudicialAIError")
