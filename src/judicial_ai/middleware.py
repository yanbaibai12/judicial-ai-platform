from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .config import get_settings
from .context import bind_request_context, reset_request_context
from .metrics import metrics, record_http_request

logger = logging.getLogger("judicial_ai.access")

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_TRACEPARENT_PATTERN = re.compile(
    r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$"
)


def _request_id(value: str | None) -> str:
    if value and _REQUEST_ID_PATTERN.fullmatch(value):
        return value
    return f"req-{uuid.uuid4().hex[:20]}"


def _trace_id(request: Request) -> tuple[str, str]:
    traceparent = request.headers.get("traceparent", "")
    match = _TRACEPARENT_PATTERN.fullmatch(traceparent.strip().lower())
    if match and match.group(2) != "0" * 32:
        return match.group(2), traceparent
    header_trace = request.headers.get("X-Trace-Id", "")
    if header_trace and _REQUEST_ID_PATTERN.fullmatch(header_trace):
        trace_id = header_trace
    else:
        trace_id = uuid.uuid4().hex
    return trace_id, f"00-{trace_id}-{uuid.uuid4().hex[:16]}-01"


def _security_headers(response: Response, *, is_html: bool, is_docs: bool = False) -> None:
    settings = get_settings()
    if not settings.security_headers_enabled:
        return
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"
    )
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    if settings.hsts_enabled:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    if is_html and not is_docs:
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )


def _too_large_response(request: Request, max_bytes: int) -> JSONResponse:
    return JSONResponse(
        status_code=413,
        content={
            "error": {
                "code": "REQUEST_TOO_LARGE",
                "message": f"请求体超过限制（{max_bytes} bytes）",
                "type": "client_error",
                "request_id": getattr(request.state, "request_id", ""),
                "details": {"max_bytes": max_bytes},
            }
        },
    )


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        settings = get_settings()
        request_id = _request_id(request.headers.get("X-Request-Id"))
        trace_id, traceparent = _trace_id(request)
        client_ip = request.client.host if request.client else ""
        user_agent = request.headers.get("user-agent", "")[:512]
        request.state.request_id = request_id
        request.state.trace_id = trace_id
        request.state.traceparent = traceparent
        request.state.client_ip = client_ip
        request.state.user_agent = user_agent
        tokens = bind_request_context(
            request_id=request_id,
            trace_id=trace_id,
            client_ip=client_ip,
            user_agent=user_agent,
        )

        started = time.perf_counter()
        metrics.add_gauge("judicial_active_requests", 1)
        status_code = 500
        response: Response
        try:
            content_length = request.headers.get("content-length")
            if content_length:
                try:
                    too_large = int(content_length) > settings.max_request_bytes
                except ValueError:
                    too_large = False
                if too_large:
                    response = _too_large_response(request, settings.max_request_bytes)
                    status_code = response.status_code
                    return self._finalize(
                        response,
                        request,
                        request_id,
                        trace_id,
                        traceparent,
                        started,
                        status_code,
                    )
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > settings.max_request_bytes:
                    response = _too_large_response(request, settings.max_request_bytes)
                    status_code = response.status_code
                    return self._finalize(
                        response,
                        request,
                        request_id,
                        trace_id,
                        traceparent,
                        started,
                        status_code,
                    )
                body.extend(chunk)
            # BaseHTTPMiddleware replays a cached body to downstream consumers.
            request._body = bytes(body)
            del body
            try:
                response = await call_next(request)
            except Exception:
                status_code = 500
                raise
            status_code = response.status_code
            return self._finalize(
                response,
                request,
                request_id,
                trace_id,
                traceparent,
                started,
                status_code,
            )
        finally:
            metrics.add_gauge("judicial_active_requests", -1)
            should_log = settings.access_log_enabled and not request.url.path.startswith(
                ("/console/", "/metrics")
            )
            if should_log:
                route = request.scope.get("route")
                route_path = getattr(route, "path", None) or request.url.path
                logger.info(
                    "http_request",
                    extra={
                        "request_id": request_id,
                        "trace_id": trace_id,
                        "tenant_id": getattr(request.state, "tenant_id", None),
                        "method": request.method,
                        "path": route_path,
                        "status_code": status_code,
                        "duration_ms": int((time.perf_counter() - started) * 1000),
                        "client_ip": client_ip,
                    },
                )
            reset_request_context(tokens)

    @staticmethod
    def _finalize(
        response: Response,
        request: Request,
        request_id: str,
        trace_id: str,
        traceparent: str,
        started: float,
        status_code: int,
    ) -> Response:
        response.headers["X-Request-Id"] = request_id
        response.headers["X-Trace-Id"] = trace_id
        response.headers["traceparent"] = traceparent
        rate_limit = getattr(request.state, "rate_limit", None)
        if rate_limit is not None:
            for key, value in rate_limit.headers().items():
                response.headers[key] = value
            if status_code == 429:
                response.headers["Retry-After"] = str(
                    max(1, int(round(rate_limit.reset_after_ms / 1000)))
                )
        is_docs = request.url.path.startswith(("/docs", "/redoc"))
        _security_headers(
            response,
            is_html=response.headers.get("content-type", "").startswith("text/html"),
            is_docs=is_docs,
        )
        route = request.scope.get("route")
        route_path = getattr(route, "path", None) or request.url.path
        record_http_request(
            request.method, route_path, status_code, time.perf_counter() - started
        )
        return response
