from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import admin, auth, gateway, governance, health, jobs, mcp, retrieval
from .config import get_settings
from .db import init_db
from .errors import register_exception_handlers
from .logging import setup_logging
from .middleware import RequestContextMiddleware

DEFAULT_MODULES = ["health", "auth", "admin", "gateway", "retrieval", "mcp", "governance", "jobs"]
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level)
    if settings.is_production:
        if settings.allow_static_admin:
            raise RuntimeError("生产环境必须关闭静态管理员令牌 ALLOW_STATIC_ADMIN")
        if settings.cors_origin_list == ["*"]:
            raise RuntimeError("生产环境必须显式配置 CORS_ORIGINS，禁止使用通配符")
        if settings.is_sqlite:
            raise RuntimeError("生产环境禁止使用 SQLite，请配置 MySQL")
        if not settings.resource_acl_enforced:
            raise RuntimeError("生产环境必须启用 RESOURCE_ACL_ENFORCED")
        if settings.inference_mode != "proxy":
            raise RuntimeError("生产环境禁止使用 mock 推理")
        if not settings.index_require_approval:
            raise RuntimeError("生产环境必须启用 INDEX_REQUIRE_APPROVAL")
        if not settings.require_prod_alias:
            raise RuntimeError("生产环境必须启用 REQUIRE_PROD_ALIAS")
    init_db()
    yield


def create_app(modules: list[str] | None = None) -> FastAPI:
    settings = get_settings()
    docs_url = "/docs" if settings.docs_enabled else None
    redoc_url = "/redoc" if settings.docs_enabled else None
    openapi_url = "/openapi.json" if settings.docs_enabled else None
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="统一模型推理、公共检索、MCP 工具与 AI 数据治理平台",
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
        lifespan=lifespan,
    )
    app.add_middleware(RequestContextMiddleware)
    if settings.trusted_host_list != ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_host_list)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=settings.cors_origin_list != ["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[
            "X-Request-Id",
            "X-Trace-Id",
            "traceparent",
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
            "Retry-After",
        ],
    )
    register_exception_handlers(app)

    enabled = modules or DEFAULT_MODULES
    if "health" in enabled:
        app.include_router(health.router)
    if "auth" in enabled:
        app.include_router(auth.router)
    if "jobs" in enabled:
        app.include_router(jobs.router)
    if "admin" in enabled:
        app.include_router(admin.admin_router)
        app.include_router(admin.discovery_router)
    if "gateway" in enabled:
        app.include_router(gateway.router)
    if "retrieval" in enabled:
        app.include_router(retrieval.router)
    if "mcp" in enabled:
        app.include_router(mcp.router)
    if "governance" in enabled:
        app.include_router(governance.router)
    if "retrieval" in enabled:
        from .api import contract

        app.include_router(contract.router)

    if STATIC_DIR.exists():
        app.mount("/console", StaticFiles(directory=STATIC_DIR, html=True), name="console")

    @app.get("/", include_in_schema=False)
    def root():
        index = STATIC_DIR / "index.html"
        if index.exists():
            return FileResponse(index)
        return {
            "name": settings.app_name,
            "version": settings.app_version,
            "environment": settings.app_env,
            "docs": "/docs" if settings.docs_enabled else None,
        }

    return app

