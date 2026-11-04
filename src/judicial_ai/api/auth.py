from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..config import get_settings
from ..security import Principal, require_any
from .utils import ok

router = APIRouter(tags=["auth"])


@router.get("/api/v1/auth/config")
def auth_config(request: Request):
    settings = get_settings()
    return ok(
        request,
        {
            "mode": settings.auth_mode,
            "oidc": {
                "issuer": settings.oidc_issuer,
                "audience": settings.oidc_audience,
                "client_id": settings.oidc_audience,
            },
            "static_admin_enabled": settings.allow_static_admin,
            "environment": settings.app_env,
        },
    )


@router.get("/api/v1/auth/me")
def auth_me(
    request: Request,
    principal: Principal = Depends(require_any),
):
    return ok(
        request,
        {
            "kind": principal.kind,
            "tenant_id": principal.tenant_id,
            "app_id": principal.app_id,
            "app_code": principal.app_code,
            "user_id": principal.user_id,
            "username": principal.username,
            "display_name": principal.display_name,
            "email": principal.email,
            "roles": list(principal.roles),
            "permissions": sorted(principal.permissions),
            "auth_method": principal.auth_method,
        },
    )
