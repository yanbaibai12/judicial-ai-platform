from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text

from ..config import get_settings
from ..db import SessionLocal
from ..metrics import render_metrics

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _check_database() -> dict[str, Any]:
    started = time.perf_counter()
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok", "latency_ms": int((time.perf_counter() - started) * 1000)}
    except Exception as exc:  # noqa: BLE001 - health endpoints must report probes
        return {"status": "error", "error": str(exc)[:240]}
    finally:
        db.close()


def _check_redis() -> dict[str, Any]:
    settings = get_settings()
    if not settings.redis_url:
        return {"status": "disabled"}
    started = time.perf_counter()
    try:
        from redis import Redis

        client = Redis.from_url(
            settings.redis_url,
            socket_timeout=settings.redis_socket_timeout_seconds,
            socket_connect_timeout=settings.redis_socket_timeout_seconds,
        )
        client.ping()
        return {"status": "ok", "latency_ms": int((time.perf_counter() - started) * 1000)}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": str(exc)[:240]}


def _check_vector() -> dict[str, Any]:
    settings = get_settings()
    if settings.vector_backend == "sql":
        return {"status": "ok", "backend": "sql"}
    try:
        from ..services.vector_store import get_vector_store

        return get_vector_store().health()
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "backend": settings.vector_backend, "error": str(exc)[:240]}


def _check_inference() -> dict[str, Any]:
    try:
        from ..services.inference import get_inference_client

        return get_inference_client().health()
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": str(exc)[:240]}


def _check_graph() -> dict[str, Any]:
    settings = get_settings()
    if not settings.graph_enabled:
        return {"status": "disabled", "backend": settings.graph_backend}
    try:
        from ..services.graph_store import get_graph_store

        return get_graph_store().health()
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "backend": settings.graph_backend, "error": str(exc)[:240]}


@router.get("/readyz")
def readyz(response: Response):
    settings = get_settings()
    components = {
        "database": _check_database(),
        "redis": _check_redis(),
        "vector": _check_vector(),
        "graph": _check_graph(),
        "inference": _check_inference(),
    }
    critical_failures = []
    if components["database"]["status"] == "error":
        critical_failures.append("database")
    if (
        settings.rate_limit_enabled
        and settings.redis_url
        and not settings.rate_limit_fail_open
        and components["redis"]["status"] != "ok"
    ):
        critical_failures.append("redis")
    if (
        settings.vector_backend != "sql"
        and not settings.vector_fallback_sql
        and components["vector"]["status"] == "error"
    ):
        critical_failures.append("vector")
    if (
        settings.graph_enabled
        and not settings.graph_fail_open
        and components["graph"]["status"] == "error"
    ):
        critical_failures.append("graph")
    if settings.inference_mode == "proxy" and components["inference"]["status"] == "error":
        critical_failures.append("inference")
    ready = not critical_failures
    payload = {
        "status": "ready" if ready else "not_ready",
        "environment": settings.app_env,
        "version": settings.app_version,
        "components": components,
        "critical_failures": critical_failures,
    }
    if not ready:
        response.status_code = 503
        return JSONResponse(status_code=503, content=payload)
    return payload


@router.get("/version")
def version() -> dict[str, str]:
    settings = get_settings()
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "environment": settings.app_env,
    }


@router.get("/metrics", include_in_schema=False)
def prometheus_metrics() -> PlainTextResponse:
    return PlainTextResponse(
        render_metrics(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )

