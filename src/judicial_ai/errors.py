from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger("judicial_ai.errors")

_EXCEPTION_NAME_LIMIT = 64


def sanitize_exception_summary(exc: BaseException) -> str:
    """只暴露异常类型名，禁止把异常正文写入对外响应体。

    异常正文可能包含连接串、内网主机名、凭据片段等；完整堆栈只应进服务端
    日志。
    """
    name = type(exc).__name__ or "UnknownError"
    return name[:_EXCEPTION_NAME_LIMIT]


class ApiError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "") or request.headers.get("X-Request-Id", "")


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "") or request.headers.get("X-Trace-Id", "")


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "type": "client_error" if exc.status_code < 500 else "server_error",
                "request_id": _request_id(request),
                "trace_id": _trace_id(request),
                "details": exc.details,
            }
        },
    )


def _json_safe(value: Any) -> Any:
    """把 pydantic 错误明细转成可 JSON 序列化结构。

    `ctx` 里可能带原始 `ValueError` 实例，直接塞进 JSONResponse 会 500。
    """

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "code": "INVALID_ARGUMENT",
                "message": "参数校验失败",
                "type": "client_error",
                "request_id": _request_id(request),
                "trace_id": _trace_id(request),
                "details": {"errors": _json_safe(exc.errors())},
            }
        },
    )


async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "unhandled_exception",
        extra={
            "request_id": _request_id(request),
            "trace_id": _trace_id(request),
            "path": request.url.path,
            "method": request.method,
        },
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "服务内部错误",
                "type": "server_error",
                "request_id": _request_id(request),
                "trace_id": _trace_id(request),
                "details": {},
            }
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unexpected_error_handler)
