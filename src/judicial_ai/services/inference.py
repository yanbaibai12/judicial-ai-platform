from __future__ import annotations

import json
import time
from collections.abc import Iterator
from functools import lru_cache
from typing import Any

import httpx

from ..config import get_settings
from ..errors import ApiError
from ..text_utils import deterministic_embedding, keyword_score


class InferenceClient:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._client = httpx.Client(
            timeout=httpx.Timeout(self.settings.inference_timeout_seconds),
            headers=self._headers(),
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.inference_api_key:
            headers["Authorization"] = f"Bearer {self.settings.inference_api_key}"
        return headers

    def _url(self, path: str) -> str:
        return f"{self.settings.inference_base_url.rstrip('/')}/{path.lstrip('/')}"

    def _raise_for_upstream(self, exc: Exception) -> None:
        if isinstance(exc, httpx.TimeoutException):
            raise ApiError("UPSTREAM_TIMEOUT", "模型服务超时", 504) from exc
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            if status == 429:
                raise ApiError("RATE_LIMITED", "模型服务繁忙", 429) from exc
            if 400 <= status < 500:
                raise ApiError(
                    "INVALID_ARGUMENT",
                    "模型服务拒绝请求",
                    400,
                    {"upstream_status": status},
                ) from exc
            raise ApiError("UPSTREAM_UNAVAILABLE", "模型服务不可用", 503) from exc
        raise ApiError("UPSTREAM_UNAVAILABLE", "模型服务连接失败", 503) from exc

    def health(self) -> dict[str, Any]:
        if self.settings.inference_mode == "mock":
            return {"status": "ok", "backend": "mock"}
        try:
            response = self._client.get(self._url("models"))
            response.raise_for_status()
            return {
                "status": "ok",
                "backend": "proxy",
                "base_url": self.settings.inference_base_url,
            }
        except Exception as exc:  # noqa: BLE001 - readiness must report the failure
            return {
                "status": "error",
                "backend": "proxy",
                "base_url": self.settings.inference_base_url,
                "error": str(exc)[:240],
            }

    def chat(self, model: str, messages: list[dict[str, Any]], **options: Any) -> dict[str, Any]:
        if self.settings.inference_mode == "mock":
            return self._mock_chat(model, messages, **options)
        payload = {"model": model, "messages": messages, **self._clean_options(options)}
        try:
            response = self._client.post(self._url("chat/completions"), json=payload)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            self._raise_for_upstream(exc)
            raise

    def stream_chat(
        self, model: str, messages: list[dict[str, Any]], **options: Any
    ) -> Iterator[bytes]:
        if self.settings.inference_mode == "mock":
            yield from self._mock_stream_chat(model, messages, **options)
            return
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            **self._clean_options(options),
        }
        try:
            with self._client.stream(
                "POST", self._url("chat/completions"), json=payload
            ) as response:
                response.raise_for_status()
                yield from response.iter_bytes()
        except Exception as exc:
            self._raise_for_upstream(exc)
            raise

    def embed(self, model: str, inputs: list[str]) -> list[list[float]]:
        if self.settings.inference_mode == "mock":
            return [deterministic_embedding(text, self.settings.embedding_dim) for text in inputs]
        payload = {"model": model, "input": inputs}
        try:
            response = self._client.post(self._url("embeddings"), json=payload)
            response.raise_for_status()
            data = response.json()
            return [item["embedding"] for item in data["data"]]
        except Exception as exc:
            self._raise_for_upstream(exc)
            raise

    def rerank(self, model: str, query: str, documents: list[str]) -> list[dict[str, Any]]:
        if self.settings.inference_mode == "mock":
            scored = [
                {"index": index, "relevance_score": keyword_score(query, document)}
                for index, document in enumerate(documents)
            ]
            return sorted(scored, key=lambda item: item["relevance_score"], reverse=True)
        payload = {"model": model, "query": query, "documents": documents}
        try:
            response = self._client.post(self._url("rerank"), json=payload)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict) and "results" in data:
                return data["results"]
            if isinstance(data, list):
                return data
            raise ApiError("UPSTREAM_UNAVAILABLE", "Rerank 响应格式不兼容", 503)
        except ApiError:
            raise
        except Exception as exc:
            self._raise_for_upstream(exc)
            raise

    def vision(self, model: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.settings.inference_mode == "mock":
            return {
                "model": model,
                "text": "（Mock OCR）已识别文档内容。本地开发模式未连接视觉模型。",
                "blocks": [],
                "degraded": True,
            }
        try:
            response = self._client.post(self._url("vision/ocr"), json={"model": model, **payload})
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            self._raise_for_upstream(exc)
            raise

    def _clean_options(self, options: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "temperature",
            "top_p",
            "max_tokens",
            "stop",
            "presence_penalty",
            "frequency_penalty",
            "seed",
            "response_format",
            "stream_options",
            "extra_body",
        }
        return {
            key: value for key, value in options.items() if key in allowed and value is not None
        }

    def _mock_chat(
        self, model: str, messages: list[dict[str, Any]], **options: Any
    ) -> dict[str, Any]:
        user_text = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                content = message.get("content")
                if isinstance(content, str):
                    user_text = content
                else:
                    user_text = json.dumps(content, ensure_ascii=False)
                break
        answer = (
            f"（Mock 推理）已收到问题：{user_text[:160]}。"
            "当前为本地开发模式，未连接 vLLM；生产环境将 INFERENCE_MODE 设为 proxy 即可转发。"
        )
        input_tokens = max(1, sum(len(str(item.get("content", ""))) for item in messages) // 2)
        output_tokens = max(1, len(answer) // 2)
        return {
            "id": f"chatcmpl-mock-{int(time.time() * 1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": answer},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
        }

    def _mock_stream_chat(
        self, model: str, messages: list[dict[str, Any]], **options: Any
    ) -> Iterator[bytes]:
        response = self._mock_chat(model, messages, **options)
        content = response["choices"][0]["message"]["content"]
        completion_id = response["id"]
        created = response["created"]
        for start in range(0, len(content), 12):
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": content[start : start + 12]},
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
        yield b"data: [DONE]\n\n"


@lru_cache
def get_inference_client() -> InferenceClient:
    return InferenceClient()

