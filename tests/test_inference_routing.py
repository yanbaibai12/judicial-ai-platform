from types import SimpleNamespace

import httpx
import pytest

from judicial_ai.services import inference


@pytest.mark.parametrize("separate", [False, True])
def test_inference_routes_and_credentials(monkeypatch, separate):
    settings = SimpleNamespace(
        inference_mode="proxy",
        inference_timeout_seconds=10,
        inference_base_url="https://chat.example/v1",
        inference_api_key="chat-key",
        embedding_base_url="https://embedding.example/v1" if separate else None,
        embedding_api_key="embedding-key" if separate else None,
        rerank_base_url="https://rerank.example/v1" if separate else None,
        rerank_api_key=None,
        rerank_api_format="standard",
    )
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers.get("authorization")))
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]})
        if request.url.path.endswith("/rerank"):
            return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.9}]})
        return httpx.Response(200, json={"choices": []})

    client_class = httpx.Client
    monkeypatch.setattr(inference, "get_settings", lambda: settings)
    monkeypatch.setattr(
        inference.httpx,
        "Client",
        lambda **kwargs: client_class(transport=httpx.MockTransport(handler), **kwargs),
    )
    client = inference.InferenceClient()
    client.chat("chat-model", [])
    assert client.embed("embedding-model", ["text"]) == [[0.1, 0.2]]
    assert client.rerank("rerank-model", "query", ["text"])[0]["index"] == 0

    assert seen[0] == ("https://chat.example/v1/chat/completions", "Bearer chat-key")
    if separate:
        assert seen[1] == ("https://embedding.example/v1/embeddings", "Bearer embedding-key")
        assert seen[2] == ("https://rerank.example/v1/rerank", None)
    else:
        assert seen[1] == ("https://chat.example/v1/embeddings", "Bearer chat-key")
        assert seen[2] == ("https://chat.example/v1/rerank", "Bearer chat-key")


def test_tei_rerank_format_and_response(monkeypatch):
    settings = inference.get_settings()
    monkeypatch.setattr(settings, "inference_mode", "proxy")
    monkeypatch.setattr(settings, "rerank_api_format", "tei")
    monkeypatch.setattr(settings, "rag_rerank_model", "bge-reranker-v2-m3")
    monkeypatch.setattr(settings, "rerank_base_url", "http://localhost:18080")
    monkeypatch.setattr(settings, "rerank_api_key", None)

    def handler(request):
        import json

        assert str(request.url) == "http://localhost:18080/rerank"
        assert json.loads(request.content) == {"query": "q", "texts": ["a", "b"], "truncate": True}
        return httpx.Response(200, json=[{"index": 1, "score": 0.8}, {"index": 0, "score": 0.1}])

    client_class = httpx.Client
    monkeypatch.setattr(
        inference.httpx,
        "Client",
        lambda **kwargs: client_class(transport=httpx.MockTransport(handler), **kwargs),
    )
    assert inference.InferenceClient().rerank("bge-reranker-v2-m3", "q", ["a", "b"]) == [
        {"index": 1, "relevance_score": 0.8}, {"index": 0, "relevance_score": 0.1}
    ]


def test_standard_rerank_allows_partial_results(monkeypatch):
    settings = inference.get_settings()
    monkeypatch.setattr(settings, "inference_mode", "proxy")
    monkeypatch.setattr(settings, "rerank_api_format", "standard")
    client_class = httpx.Client
    result = [{"index": 1, "relevance_score": 0.8}]
    monkeypatch.setattr(
        inference.httpx,
        "Client",
        lambda **kwargs: client_class(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"results": result})
            ),
            **kwargs,
        ),
    )
    assert inference.InferenceClient().rerank("model", "q", ["a", "b"]) == result


@pytest.mark.parametrize(
    "results",
    [
        [{"index": 0, "score": 0.5}],
        [{"index": 0, "score": 0.5}, {"index": 0, "score": 0.2}],
        [{"index": 0, "score": 0.5}, {"index": 2, "score": 0.2}],
        [{"index": 0, "score": 0.5}, {"index": 1, "score": "invalid"}],
    ],
)
def test_tei_rejects_invalid_results(monkeypatch, results):
    from judicial_ai.errors import ApiError

    settings = inference.get_settings()
    monkeypatch.setattr(settings, "inference_mode", "proxy")
    monkeypatch.setattr(settings, "rerank_api_format", "tei")
    monkeypatch.setattr(settings, "rag_rerank_model", "bge-reranker-v2-m3")
    client_class = httpx.Client
    monkeypatch.setattr(
        inference.httpx,
        "Client",
        lambda **kwargs: client_class(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=results)),
            **kwargs,
        ),
    )
    with pytest.raises(ApiError):
        inference.InferenceClient().rerank("bge-reranker-v2-m3", "q", ["a", "b"])
