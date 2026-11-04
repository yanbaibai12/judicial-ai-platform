from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx


class JudicialAIError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str = "CLIENT_ERROR",
        status_code: int = 0,
        request_id: str = "",
        trace_id: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.request_id = request_id
        self.trace_id = trace_id
        self.details = details or {}


def _error_from_response(response: httpx.Response) -> JudicialAIError:
    payload: dict[str, Any] = {}
    try:
        body = response.json()
        payload = body if isinstance(body, dict) else {}
    except ValueError:
        payload = {}
    raw_error = payload.get("error")
    error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
    return JudicialAIError(
        str(error.get("message") or response.text[:500] or "request failed"),
        code=str(error.get("code") or f"HTTP_{response.status_code}"),
        status_code=response.status_code,
        request_id=str(error.get("request_id") or response.headers.get("X-Request-Id", "")),
        trace_id=str(error.get("trace_id") or response.headers.get("X-Trace-Id", "")),
        details=error.get("details") if isinstance(error.get("details"), dict) else {},
    )


def _should_retry(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


class Client:
    """司法 AI 中台同步客户端。"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        timeout: float = 30.0,
        max_retries: int = 2,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or "http://127.0.0.1:8080").rstrip("/")
        self.api_key = api_key or ""
        self.max_retries = max_retries
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _headers(self, request_id: str | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers["X-Request-Id"] = request_id or f"sdk-{uuid.uuid4().hex[:20]}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        last_error: JudicialAIError | None = None
        for attempt in range(self.max_retries + 1):
            response = self._client.request(
                method,
                path,
                headers=self._headers(request_id),
                json=json_body,
                params=params,
            )
            if response.status_code < 400:
                if not response.content:
                    return {}
                return response.json()
            last_error = _error_from_response(response)
            if not _should_retry(response.status_code) or attempt >= self.max_retries:
                raise last_error
            retry_after = response.headers.get("Retry-After")
            delay = (
                float(retry_after)
                if retry_after and retry_after.isdigit()
                else 0.25 * 2**attempt
            )
            time.sleep(min(delay, 5.0))
        raise last_error or JudicialAIError("request failed")

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/healthz")

    def ready(self) -> dict[str, Any]:
        return self._request("GET", "/readyz")

    def list_models(self) -> dict[str, Any]:
        return self._request("GET", "/v1/models")

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        stream: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        stop: list[str] | str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any] | Iterator[dict[str, Any]]:
        body = {
            "model": model,
            "messages": messages,
            "stream": stream,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": top_p,
            "stop": stop,
        }
        body = {key: value for key, value in body.items() if value is not None}
        if not stream:
            return self._request(
                "POST", "/v1/chat/completions", json_body=body, request_id=request_id
            )
        return self._stream_chat(body, request_id)

    def _stream_chat(
        self, body: dict[str, Any], request_id: str | None
    ) -> Iterator[dict[str, Any]]:
        with self._client.stream(
            "POST",
            "/v1/chat/completions",
            headers=self._headers(request_id),
            json=body,
        ) as response:
            if response.status_code >= 400:
                response.read()
                raise _error_from_response(response)
            for line in response.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                if data:
                    yield json.loads(data)

    def embeddings(
        self,
        model: str,
        input: str | list[str],
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/embeddings",
            json_body={"model": model, "input": input},
            request_id=request_id,
        )

    def rerank(
        self,
        model: str,
        query: str,
        documents: list[str],
        *,
        top_n: int | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"model": model, "query": query, "documents": documents}
        if top_n:
            body["top_n"] = top_n
        return self._request("POST", "/v1/rerank", json_body=body, request_id=request_id)

    def retrieve(
        self,
        kb_ids: list[int],
        query: str,
        *,
        top_k: int = 10,
        kb_version: str = "prod",
        filters: dict[str, Any] | None = None,
        with_graph: bool = False,
        with_citation: bool = True,
        compress: bool = False,
        context_budget_chars: int | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "kb_ids": kb_ids,
            "query": query,
            "top_k": top_k,
            "kb_version": kb_version,
            "filters": filters or {},
            "with_graph": with_graph,
            "with_citation": with_citation,
            "compress": compress,
        }
        if context_budget_chars is not None:
            body["context_budget_chars"] = context_budget_chars
        return self._request("POST", "/v1/retrieve", json_body=body, request_id=request_id)

    def invoke(
        self,
        code: str,
        input: dict[str, Any],
        *,
        version: str | None = None,
        options: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"input": input, "options": options or {}}
        if version:
            body["version"] = version
        return self._request(
            "POST",
            f"/api/v1/capabilities/{code}/invoke",
            json_body=body,
            request_id=request_id,
        )


class AsyncClient:
    """司法 AI 中台异步客户端。"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        timeout: float = 30.0,
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or "http://127.0.0.1:8080").rstrip("/")
        self.api_key = api_key or ""
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> AsyncClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    def _headers(self, request_id: str | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers["X-Request-Id"] = request_id or f"sdk-{uuid.uuid4().hex[:20]}"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        last_error: JudicialAIError | None = None
        for attempt in range(self.max_retries + 1):
            response = await self._client.request(
                method,
                path,
                headers=self._headers(request_id),
                json=json_body,
                params=params,
            )
            if response.status_code < 400:
                if not response.content:
                    return {}
                return response.json()
            last_error = _error_from_response(response)
            if not _should_retry(response.status_code) or attempt >= self.max_retries:
                raise last_error
            retry_after = response.headers.get("Retry-After")
            delay = (
                float(retry_after)
                if retry_after and retry_after.isdigit()
                else 0.25 * 2**attempt
            )
            import asyncio

            await asyncio.sleep(min(delay, 5.0))
        raise last_error or JudicialAIError("request failed")

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/healthz")

    async def ready(self) -> dict[str, Any]:
        return await self._request("GET", "/readyz")

    async def list_models(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/models")

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        stream: bool = False,
        request_id: str | None = None,
        **options: Any,
    ) -> dict[str, Any] | AsyncIterator[dict[str, Any]]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
            **{key: value for key, value in options.items() if value is not None},
        }
        if not stream:
            return await self._request(
                "POST", "/v1/chat/completions", json_body=body, request_id=request_id
            )
        return self._stream_chat(body, request_id)

    async def _stream_chat(
        self, body: dict[str, Any], request_id: str | None
    ) -> AsyncIterator[dict[str, Any]]:
        async with self._client.stream(
            "POST",
            "/v1/chat/completions",
            headers=self._headers(request_id),
            json=body,
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                raise _error_from_response(response)
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                if data:
                    yield json.loads(data)

    async def retrieve(
        self,
        kb_ids: list[int],
        query: str,
        *,
        top_k: int = 10,
        request_id: str | None = None,
        **options: Any,
    ) -> dict[str, Any]:
        body = {"kb_ids": kb_ids, "query": query, "top_k": top_k, **options}
        return await self._request("POST", "/v1/retrieve", json_body=body, request_id=request_id)

    async def embeddings(
        self,
        model: str,
        input: str | list[str],
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/embeddings",
            json_body={"model": model, "input": input},
            request_id=request_id,
        )

    async def rerank(
        self,
        model: str,
        query: str,
        documents: list[str],
        *,
        top_n: int | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"model": model, "query": query, "documents": documents}
        if top_n:
            body["top_n"] = top_n
        return await self._request("POST", "/v1/rerank", json_body=body, request_id=request_id)

    async def invoke(
        self,
        code: str,
        input: dict[str, Any],
        *,
        version: str | None = None,
        request_id: str | None = None,
        **options: Any,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"input": input, "options": options}
        if version:
            body["version"] = version
        return await self._request(
            "POST",
            f"/api/v1/capabilities/{code}/invoke",
            json_body=body,
            request_id=request_id,
        )


