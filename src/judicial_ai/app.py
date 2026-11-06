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
from .services.graph_store import close_graph_store

DEFAULT_MODULES = ["health", "auth", "admin", "gateway", "retrieval", "mcp", "governance", "jobs"]
STATIC_DIR = Path(__file__).parent / "static"

_DEV_HASH_SECRET = "dev-api-key-hash-secret"


def _hash_secret_invalid(secret: str) -> bool:
    return not secret or secret == _DEV_HASH_SECRET or len(secret) < 32


def _legacy_hash_secrets(settings) -> list[str]:  # type: ignore[no-untyped-def]
    raw = getattr(settings, "api_key_hash_legacy_secrets", "") or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


def _hash_secrets_not_distinct(settings) -> bool:  # type: ignore[no-untyped-def]
    secrets_in_use = [settings.api_key_hash_secret, *_legacy_hash_secrets(settings)]
    return len(secrets_in_use) != len(set(secrets_in_use))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level)
    if settings.is_production:
        if settings.allow_static_admin:
            raise RuntimeError("生产环境必须关闭静态管理员令牌 ALLOW_STATIC_ADMIN")
        if _hash_secret_invalid(settings.api_key_hash_secret):
            raise RuntimeError("生产环境必须配置至少 32 字符的 API_KEY_HASH_SECRET")
        if any(_hash_secret_invalid(item) for item in _legacy_hash_secrets(settings)):
            raise RuntimeError(
                "生产环境 API_KEY_HASH_LEGACY_SECRETS 各项必须至少 32 字符且不得使用默认值"
            )
        if _hash_secrets_not_distinct(settings):
            raise RuntimeError("API_KEY_HASH_SECRET 与 legacy 列表不得重复，密钥轮换未生效")
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
    try:
        yield
    finally:
        close_graph_store()


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

