from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from ..security import Principal


def ok(request: Request, data: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"data": data, "request_id": getattr(request.state, "request_id", "")},
    )


def tenant_scope(principal: Principal, explicit: int | None = None) -> int | None:
    if principal.kind in {"app", "user"} and not principal.is_admin:
        return principal.tenant_id
    return explicit
