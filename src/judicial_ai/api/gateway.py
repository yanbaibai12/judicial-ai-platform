from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import SessionLocal, get_db
from ..errors import ApiError
from ..metrics import record_capability_invocation
from ..models import Capability, CapabilityGrant, CapabilityVersion, utcnow
from ..schemas import (
    ChatCompletionRequest,
    EmbeddingRequest,
    InvokeRequest,
    RerankRequest,
    RetrieveRequest,
)
from ..security import Principal, require_app
from ..services.capabilities import (
    check_grant,
    find_capability_by_model,
    resolve_capability,
    validate_against_schema,
)
from ..services.inference import get_inference_client
from ..services.mcp_hub import McpError, call_tool
from ..services.observability import record_event, record_usage
from ..services.quota import get_rate_limiter
from ..services.retrieval import retrieve
from .retrieval import _kb_for

router = APIRouter(tags=["gateway"])


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _authorize_and_rate_limit(
    request: Request,
    db: Session,
    principal: Principal,
    capability: Capability,
    version: CapabilityVersion,
) -> CapabilityGrant:
    grant = check_grant(
        db,
        principal.app_id or 0,
        capability.id,
        version.id,
        env=getattr(request.state, "environment", None) or "prod",
    )
    if grant is None:
        raise ApiError("FORBIDDEN", "无能力授权", 403, {"capability": capability.code})
    grant_qps = None
    if isinstance(grant.quota, dict):
        try:
            grant_qps = int(grant.quota.get("qps")) if grant.quota.get("qps") else None
        except (TypeError, ValueError):
            grant_qps = None
    limit = min(
        item for item in (version.qps_limit, grant_qps, get_settings().default_qps_limit) if item
    )
    result = get_rate_limiter().consume(
        f"app:{principal.app_id}:cap:{capability.code}",
        limit,
        window_seconds=get_settings().rate_limit_window_seconds,
    )
    request.state.rate_limit = result
    if not result.allowed:
        raise ApiError(
            "RATE_LIMITED",
            "请求过于频繁，请稍后重试",
            429,
            {"retry_after_ms": result.reset_after_ms, "limit": result.limit},
        )
    return grant


def _model_name(capability: Capability, version: CapabilityVersion) -> str:
    return version.adapter_alias or version.model_name or capability.code


def _extract_usage(output: dict[str, Any]) -> tuple[int, int]:
    usage = output.get("usage") or {}
    return int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 2) if text else 0


def _messages_text(messages: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"{message.get('role', 'user')}: {message.get('content', '')}" for message in messages
    )


def _chat_output_text(output: dict[str, Any]) -> str:
    choices = output.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _event_type_for(capability: Capability) -> str:
    if capability.type == "RAG_RETRIEVE":
        return "RETRIEVAL_COMPLETED"
    if capability.type.startswith("MODEL_"):
        return "INFERENCE_COMPLETED"
    return f"{capability.type}_COMPLETED"


def _record(
    db: Session,
    principal: Principal,
    capability: Capability,
    version: CapabilityVersion,
    request_id: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    status: str = "SUCCESS",
    *,
    input_text: str | None = None,
    output_text: str | None = None,
    citations: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    record_usage(
        db,
        principal,
        capability,
        version,
        request_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        status=status,
    )
    record_capability_invocation(capability.type, status.lower())
    payload: dict[str, Any] = {
        "capability": capability.code,
        "version": version.version,
        "type": capability.type,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "latency_ms": latency_ms,
        "status": status,
    }
    if input_text:
        payload["input_text"] = input_text
    if output_text:
        payload["output_text"] = output_text
    if citations:
        payload["citations"] = citations
    if extra:
        payload.update(extra)
    record_event(
        db,
        _event_type_for(capability),
        tenant_id=principal.tenant_id or 0,
        app_id=principal.app_id,
        request_id=request_id,
        trace_id=request_id,
        payload=payload,
        object_ref=f"{capability.code}:{version.version}",
    )


def execute_capability(
    request: Request,
    db: Session,
    principal: Principal,
    capability: Capability,
    version: CapabilityVersion,
    payload: InvokeRequest,
    request_id: str,
) -> dict[str, Any]:
    validate_against_schema(version.request_schema or {}, payload.input, "input")
    grant = _authorize_and_rate_limit(request, db, principal, capability, version)
    inference = get_inference_client()
    started = time.perf_counter()
    input_text: str | None = None
    output_text: str | None = None
    citations: list[dict[str, Any]] | None = None
    extra: dict[str, Any] | None = None

    if capability.type == "MODEL_CHAT":
        messages = payload.input.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ApiError("INVALID_ARGUMENT", "input.messages 必须是非空数组", 400)
        input_text = _messages_text(messages)
        output = inference.chat(_model_name(capability, version), messages, **payload.options)
        output_text = _chat_output_text(output)
        input_tokens, output_tokens = _extract_usage(output)
    elif capability.type == "MODEL_EMBEDDING":
        inputs = payload.input.get("input")
        if isinstance(inputs, str):
            inputs = [inputs]
        if not isinstance(inputs, list) or not inputs:
            raise ApiError("INVALID_ARGUMENT", "input.input 必须是非空字符串或数组", 400)
        embeddings = inference.embed(_model_name(capability, version), inputs)
        output = {
            "object": "list",
            "data": [
                {"object": "embedding", "index": index, "embedding": embedding}
                for index, embedding in enumerate(embeddings)
            ],
            "model": _model_name(capability, version),
        }
        input_tokens = sum(_estimate_tokens(str(item)) for item in inputs)
        output_tokens = 0
    elif capability.type == "MODEL_RERANK":
        query = payload.input.get("query")
        documents = payload.input.get("documents")
        if not isinstance(query, str) or not isinstance(documents, list):
            raise ApiError("INVALID_ARGUMENT", "input 需要 query 和 documents", 400)
        results = inference.rerank(_model_name(capability, version), query, documents)
        output = {"model": _model_name(capability, version), "results": results}
        input_text = query
        input_tokens = _estimate_tokens(query) + sum(
            _estimate_tokens(str(item)) for item in documents
        )
        output_tokens = 0
    elif capability.type == "MODEL_VISION":
        output = inference.vision(_model_name(capability, version), payload.input)
        input_tokens = _estimate_tokens(json.dumps(payload.input, ensure_ascii=False))
        output_tokens = _estimate_tokens(str(output.get("text", "")))
    elif capability.type == "RAG_RETRIEVE":
        retrieve_request = RetrieveRequest(**payload.input)
        for kb_id in set(retrieve_request.kb_ids):
            _kb_for(db, principal, kb_id)
        scope = grant.data_scope if isinstance(grant.data_scope, dict) else {}
        allowed_kb_ids = scope.get("kb_ids")
        if allowed_kb_ids:
            unauthorized = set(retrieve_request.kb_ids) - {int(item) for item in allowed_kb_ids}
            if unauthorized:
                raise ApiError(
                    "FORBIDDEN",
                    "检索请求包含授权范围外的知识库",
                    403,
                    {"kb_ids": sorted(unauthorized)},
                )
        output = retrieve(
            db,
            tenant_id=principal.tenant_id or 0,
            kb_ids=retrieve_request.kb_ids,
            query=retrieve_request.query,
            top_k=min(retrieve_request.top_k, get_settings().max_top_k),
            filters=retrieve_request.filters,
            kb_version_alias=retrieve_request.kb_version,
            index_version=retrieve_request.index_version,
            with_graph=retrieve_request.with_graph,
            with_citation=retrieve_request.with_citation,
            compress=retrieve_request.compress,
            context_budget_chars=retrieve_request.context_budget_chars,
            principal=principal,
        )
        result_items = output.get("results") or []
        input_text = retrieve_request.query
        output_text = "\n".join(str(item.get("text", "")) for item in result_items)
        citations = [
            item["citation"]
            for item in result_items
            if isinstance(item.get("citation"), dict)
        ]
        extra = {
            "query": retrieve_request.query,
            "kb_ids": retrieve_request.kb_ids,
            "chunk_ids": [
                item.get("chunk_id") for item in result_items if item.get("chunk_id")
            ],
        }
        if isinstance(output.get("decision"), dict):
            extra["decision"] = output["decision"]
        if isinstance(output.get("compression"), dict):
            extra["compression"] = output["compression"]
        input_tokens = _estimate_tokens(retrieve_request.query)
        output_tokens = 0
    elif capability.type == "MCP_TOOL":
        tool_name = payload.input.get("name") or payload.input.get("tool") or capability.code
        arguments = payload.input.get("arguments", {})
        try:
            output = call_tool(db, principal, str(tool_name), dict(arguments), request_id)
        except McpError as exc:
            unauthorized = exc.code == -32003
            status_code = 403 if unauthorized else 502
            error_code = "TOOL_NOT_AUTHORIZED" if unauthorized else "TOOL_EXECUTION_FAILED"
            raise ApiError(
                error_code,
                exc.message,
                status_code,
                {"jsonrpc_code": exc.code},
            ) from exc
        input_text = json.dumps(arguments, ensure_ascii=False)
        output_text = json.dumps(output, ensure_ascii=False)
        input_tokens = _estimate_tokens(input_text)
        output_tokens = 0
    elif capability.type == "GRAPH_RETRIEVE":
        kb_ids = payload.input.get("kb_ids")
        query = payload.input.get("query")
        if not isinstance(kb_ids, list) or not kb_ids or not isinstance(query, str):
            raise ApiError("INVALID_ARGUMENT", "input 需要 kb_ids 和 query", 400)
        for kb_id in {int(item) for item in kb_ids}:
            _kb_for(db, principal, kb_id)
        scope = grant.data_scope if isinstance(grant.data_scope, dict) else {}
        allowed_kb_ids = scope.get("kb_ids")
        if allowed_kb_ids:
            unauthorized = {int(item) for item in kb_ids} - {
                int(item) for item in allowed_kb_ids
            }
            if unauthorized:
                raise ApiError(
                    "FORBIDDEN",
                    "检索请求包含授权范围外的知识库",
                    403,
                    {"kb_ids": sorted(unauthorized)},
                )
        result = retrieve(
            db,
            tenant_id=principal.tenant_id or 0,
            kb_ids=[int(item) for item in kb_ids],
            query=query,
            top_k=int(payload.input.get("top_k") or get_settings().default_top_k),
            filters=payload.input.get("filters") or {},
            with_graph=True,
            with_citation=True,
            principal=principal,
        )
        output = {
            "query": result.get("query"),
            "graph": result.get("graph"),
            "results": result.get("results") or [],
            "usage": result.get("usage") or {},
        }
        citations = [
            item["citation"]
            for item in output["results"]
            if isinstance(item.get("citation"), dict)
        ]
        input_text = query
        output_text = json.dumps(output, ensure_ascii=False)
        input_tokens = _estimate_tokens(input_text)
        output_tokens = _estimate_tokens(output_text)
        extra = {"graph": output["graph"]}
    else:
        raise ApiError("INVALID_ARGUMENT", f"不支持的能力类型: {capability.type}", 400)

    latency_ms = int((time.perf_counter() - started) * 1000)
    _record(
        db,
        principal,
        capability,
        version,
        request_id,
        input_tokens,
        output_tokens,
        latency_ms,
        input_text=input_text,
        output_text=output_text,
        citations=citations,
        extra=extra,
    )
    return {
        "request_id": request_id,
        "capability": capability.code,
        "version": version.version,
        "output": output,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        "latency_ms": latency_ms,
        "degraded": False,
    }


def _accumulate_sse_content(chunk: bytes, parts: list[str]) -> None:
    text = chunk.decode("utf-8", errors="ignore")
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        data = line[6:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
            delta = payload.get("choices", [{}])[0].get("delta", {})
            content = delta.get("content")
            if isinstance(content, str):
                parts.append(content)
        except json.JSONDecodeError:
            continue


def _stream_chat_response(
    request: Request,
    principal: Principal,
    capability: Capability,
    version: CapabilityVersion,
    messages: list[dict[str, Any]],
    options: dict[str, Any],
) -> StreamingResponse:
    request_id = _request_id(request)
    inference = get_inference_client()
    model = _model_name(capability, version)
    input_text = _messages_text(messages)
    input_tokens = _estimate_tokens(
        "".join(str(message.get("content", "")) for message in messages)
    )
    started = time.perf_counter()

    def generate() -> Iterator[bytes]:
        parts: list[str] = []
        status = "SUCCESS"
        try:
            for chunk in inference.stream_chat(model, messages, **options):
                _accumulate_sse_content(chunk, parts)
                yield chunk
        except Exception:
            status = "FAILED"
            raise
        finally:
            latency_ms = int((time.perf_counter() - started) * 1000)
            output_text = "".join(parts)
            with SessionLocal() as session:
                captured_capability = session.get(Capability, capability.id)
                captured_version = session.get(CapabilityVersion, version.id)
                if captured_capability and captured_version:
                    _record(
                        session,
                        principal,
                        captured_capability,
                        captured_version,
                        request_id,
                        input_tokens,
                        _estimate_tokens(output_text),
                        latency_ms,
                        status,
                        input_text=input_text,
                        output_text=output_text,
                    )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Request-Id": request_id,
        },
    )


@router.post("/api/v1/capabilities/{code}/invoke")
def invoke_capability(
    code: str,
    payload: InvokeRequest,
    request: Request,
    principal: Principal = Depends(require_app),
    db: Session = Depends(get_db),
):
    capability, version = resolve_capability(db, code, payload.version, principal.tenant_id)
    if capability.type == "MODEL_CHAT" and payload.options.get("stream"):
        messages = payload.input.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ApiError("INVALID_ARGUMENT", "input.messages 必须是非空数组", 400)
        _authorize_and_rate_limit(request, db, principal, capability, version)
        options = {
            key: value
            for key, value in payload.options.items()
            if key in {"temperature", "top_p", "max_tokens", "stop"} and value is not None
        }
        return _stream_chat_response(request, principal, capability, version, messages, options)
    return execute_capability(
        request, db, principal, capability, version, payload, _request_id(request)
    )


@router.post("/v1/chat/completions")
def chat_completions(
    payload: ChatCompletionRequest,
    request: Request,
    principal: Principal = Depends(require_app),
    db: Session = Depends(get_db),
):
    capability, version = find_capability_by_model(db, payload.model, principal.tenant_id)
    _authorize_and_rate_limit(request, db, principal, capability, version)
    messages = [message.model_dump(exclude_none=True) for message in payload.messages]
    options = {
        "temperature": payload.temperature,
        "max_tokens": payload.max_tokens,
        "top_p": payload.top_p,
        "stop": payload.stop,
    }
    options = {key: value for key, value in options.items() if value is not None}

    if payload.stream:
        return _stream_chat_response(request, principal, capability, version, messages, options)

    started = time.perf_counter()
    output = get_inference_client().chat(_model_name(capability, version), messages, **options)
    input_tokens, output_tokens = _extract_usage(output)
    _record(
        db,
        principal,
        capability,
        version,
        _request_id(request),
        input_tokens,
        output_tokens,
        int((time.perf_counter() - started) * 1000),
        input_text=_messages_text(messages),
        output_text=_chat_output_text(output),
    )
    return output


@router.post("/v1/embeddings")
def embeddings(
    payload: EmbeddingRequest,
    request: Request,
    principal: Principal = Depends(require_app),
    db: Session = Depends(get_db),
):
    capability, version = find_capability_by_model(db, payload.model, principal.tenant_id)
    if capability.type != "MODEL_EMBEDDING":
        raise ApiError("INVALID_ARGUMENT", "该能力不是 Embedding 类型", 400)
    _authorize_and_rate_limit(request, db, principal, capability, version)
    inputs = [payload.input] if isinstance(payload.input, str) else payload.input
    started = time.perf_counter()
    vectors = get_inference_client().embed(_model_name(capability, version), inputs)
    input_tokens = sum(_estimate_tokens(str(item)) for item in inputs)
    _record(
        db,
        principal,
        capability,
        version,
        _request_id(request),
        input_tokens,
        0,
        int((time.perf_counter() - started) * 1000),
    )
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": index, "embedding": vector}
            for index, vector in enumerate(vectors)
        ],
        "model": payload.model,
        "usage": {"prompt_tokens": input_tokens, "total_tokens": input_tokens},
    }


@router.post("/v1/rerank")
def rerank(
    payload: RerankRequest,
    request: Request,
    principal: Principal = Depends(require_app),
    db: Session = Depends(get_db),
):
    capability, version = find_capability_by_model(db, payload.model, principal.tenant_id)
    if capability.type != "MODEL_RERANK":
        raise ApiError("INVALID_ARGUMENT", "该能力不是 Rerank 类型", 400)
    _authorize_and_rate_limit(request, db, principal, capability, version)
    started = time.perf_counter()
    results = get_inference_client().rerank(
        _model_name(capability, version), payload.query, payload.documents
    )
    if payload.top_n:
        results = results[: payload.top_n]
    input_tokens = _estimate_tokens(payload.query) + sum(
        _estimate_tokens(item) for item in payload.documents
    )
    _record(
        db,
        principal,
        capability,
        version,
        _request_id(request),
        input_tokens,
        0,
        int((time.perf_counter() - started) * 1000),
    )
    return {"model": payload.model, "results": results}


@router.post("/v1/vision/ocr")
def vision_ocr(
    payload: dict[str, Any],
    request: Request,
    principal: Principal = Depends(require_app),
    db: Session = Depends(get_db),
):
    capability, version = resolve_capability(db, "ocr.document", None, principal.tenant_id)
    if capability.type != "MODEL_VISION":
        raise ApiError("INVALID_ARGUMENT", "该能力不是视觉类型", 400)
    _authorize_and_rate_limit(request, db, principal, capability, version)
    started = time.perf_counter()
    output = get_inference_client().vision(_model_name(capability, version), payload)
    _record(
        db,
        principal,
        capability,
        version,
        _request_id(request),
        0,
        0,
        int((time.perf_counter() - started) * 1000),
    )
    return output


@router.get("/v1/models")
def list_models(
    principal: Principal = Depends(require_app),
    db: Session = Depends(get_db),
):
    grants = (
        db.query(CapabilityGrant)
        .filter(
            CapabilityGrant.app_id == principal.app_id,
            CapabilityGrant.status == "ACTIVE",
            or_(CapabilityGrant.expires_at.is_(None), CapabilityGrant.expires_at > utcnow()),
        )
        .all()
    )
    capability_ids = {grant.capability_id for grant in grants}
    capabilities = (
        (
            db.query(Capability)
            .filter(
                Capability.id.in_(capability_ids),
                Capability.status == "PUBLISHED",
                Capability.deleted_at.is_(None),
            )
            .order_by(Capability.code)
            .all()
        )
        if capability_ids
        else []
    )
    return {
        "object": "list",
        "data": [
            {
                "id": capability.code,
                "object": "model",
                "created": int(capability.created_at.timestamp() if capability.created_at else 0),
                "owned_by": "judicial-ai-platform",
            }
            for capability in capabilities
        ],
    }



