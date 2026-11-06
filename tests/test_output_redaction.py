from __future__ import annotations

import os
import tempfile

_TEST_DIR = tempfile.mkdtemp(prefix="judicial-ai-redaction-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TEST_DIR}/redaction.db")
os.environ.setdefault("DATA_DIR", _TEST_DIR)
os.environ.setdefault("OBJECT_STORE_PATH", f"{_TEST_DIR}/objects")
os.environ.setdefault("ADMIN_TOKEN", "test-admin")
os.environ.setdefault("INFERENCE_MODE", "mock")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("GRAPH_ENABLED", "false")
os.environ.setdefault("EMBEDDING_DIM", "128")

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from judicial_ai.api import health as health_api
from judicial_ai.app import create_app
from judicial_ai.config import get_settings
from judicial_ai.db import init_db
from judicial_ai.errors import sanitize_exception_summary
from judicial_ai.seed import _mask_secret
from judicial_ai.services.graph_store import get_graph_store
from judicial_ai.services.vector_store import get_vector_store

REPO_ROOT = Path(__file__).resolve().parent.parent

SENSITIVE_MESSAGE = (
    "could not connect to server at postgres://admin:p@ssw0rd@10.0.0.8:5432/judicial "
    "user_id=ac-918273 kernel_id=42 revision=backoff"
)

LEAK_TOKENS = ("p@ssw0rd", "10.0.0.8", "5432", "postgres://", "ac-918273", "user_id")


@pytest.fixture(autouse=True)
def _ready_db():
    init_db()
    yield


def _assert_no_leak(payload: dict) -> None:
    text = json.dumps(payload, ensure_ascii=False)
    for token in LEAK_TOKENS:
        assert token not in text, f"响应泄露了敏感片段：{token}"


class _ExplodingSession:
    """模拟连接池耗尽/数据库不可达：Session 工厂本身抛错。"""

    def __call__(self):
        raise RuntimeError(SENSITIVE_MESSAGE)


def test_sanitize_exception_summary_keeps_type_name_only():
    class OperationalError(Exception):
        pass

    summary = sanitize_exception_summary(OperationalError(SENSITIVE_MESSAGE))
    assert summary == "OperationalError"
    for token in LEAK_TOKENS:
        assert token not in summary


def test_readyz_database_failure_is_redacted(monkeypatch):
    monkeypatch.setattr(health_api, "SessionLocal", _ExplodingSession())
    result = health_api._check_database()
    assert result["status"] == "error"
    assert result["error"] == "RuntimeError"
    _assert_no_leak(result)


def test_readyz_redis_failure_is_redacted(monkeypatch):
    monkeypatch.setattr(get_settings(), "redis_url", "redis://cache.internal.invalid:6379/0")

    class _BoomRedis:
        @staticmethod
        def from_url(*args, **kwargs):
            raise ValueError(SENSITIVE_MESSAGE)

    fake_redis = type("redis", (), {"Redis": _BoomRedis})
    monkeypatch.setitem(sys.modules, "redis", fake_redis())
    result = health_api._check_redis()
    assert result["status"] == "error"
    assert result["error"] == "ValueError"
    _assert_no_leak(result)


def test_readyz_unavailable_backends_are_redacted(monkeypatch):
    monkeypatch.setattr(get_settings(), "vector_backend", "milvus")
    monkeypatch.setattr(get_settings(), "vector_fallback_sql", False)
    monkeypatch.setattr(get_settings(), "graph_enabled", True)
    monkeypatch.setattr(get_settings(), "graph_backend", "neo4j")
    # 指向确定不可达的端口，避免依赖本机是否恰好跑着 Milvus / Neo4j
    monkeypatch.setattr(get_settings(), "milvus_uri", "http://127.0.0.1:1")
    monkeypatch.setattr(get_settings(), "neo4j_uri", "bolt://127.0.0.1:1")

    # 工厂函数带 lru_cache，需先清空才能按新的 settings 重建后端实例
    get_vector_store.cache_clear()
    get_graph_store.cache_clear()
    try:
        for probe in (health_api._check_vector, health_api._check_graph):
            result = probe()
            assert result["status"] == "error"
            assert (
                result["error"] and "$" not in result["error"] and "@" not in result["error"]
            )
            _assert_no_leak(result)
    finally:
        get_vector_store.cache_clear()
        get_graph_store.cache_clear()


def test_readyz_payload_excludes_exception_text(monkeypatch):
    monkeypatch.setattr(health_api, "SessionLocal", _ExplodingSession())
    monkeypatch.setattr(get_settings(), "redis_url", None)
    monkeypatch.setattr(get_settings(), "vector_backend", "sql")
    monkeypatch.setattr(get_settings(), "graph_enabled", False)
    monkeypatch.setattr(get_settings(), "inference_mode", "mock")

    from fastapi import Response

    response = Response()
    result = health_api.readyz(response)
    payload = result if isinstance(result, dict) else json.loads(bytes(result.body))
    assert response.status_code == 503
    assert payload["status"] == "not_ready"
    assert "database" in payload["critical_failures"]
    _assert_no_leak(payload)


def test_healthz_and_version_have_no_config_values():
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as client:
        healthz = client.get("/healthz")
        version = client.get("/version")

    assert healthz.status_code == 200
    assert healthz.json() == {"status": "ok"}

    version_body = version.json()
    assert set(version_body) == {"name", "version", "environment"}
    text = json.dumps(version_body, ensure_ascii=False).lower()
    for token in ("sqlite", "postgres", "http://", "password", "admin_token", "api_key", "secret"):
        assert token not in text, f"/version 响应泄露了配置片段：{token}"


def test_mask_secret_keeps_prefix_and_suffix_only():
    assert _mask_secret("ak_abcdefghijklmno") == "ak_a****lmno"
    assert _mask_secret("") == ""
    assert len(_mask_secret("short")) == 5


def _run_seed(*args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "DATABASE_URL": f"sqlite:///{tempfile.gettempdir()}/judicial-seed-test.db",
        "DATA_DIR": str(Path(tempfile.gettempdir()) / "judicial-seed-data"),
    }
    return subprocess.run(
        [sys.executable, "-m", "judicial_ai.seed", *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        check=False,
    )


def test_seed_cli_default_output_hides_secrets():
    result = _run_seed()
    assert result.returncode == 0, result.stderr[-2000:]
    combined = result.stdout + result.stderr
    assert re.search(r"ak_[A-Za-z0-9_\-]{20,}", combined) is None
    assert "dev-admin-token" not in combined

    payload = json.loads(result.stdout)
    assert "api_key" not in payload
    assert "admin_token" not in payload
    assert payload["admin_token_source"] == "ADMIN_TOKEN"
    assert payload["api_key_prefix"].startswith("ak_")


def test_seed_cli_show_credentials_writes_restricted_file(tmp_path):
    credential_file = tmp_path / "creds.json"
    result = _run_seed("--show-credentials", "--credential-file", str(credential_file))
    assert result.returncode == 0, result.stderr[-2000:]

    combined = result.stdout + result.stderr
    assert re.search(r"ak_[A-Za-z0-9_\-]{20,}", combined) is None

    assert credential_file.exists()
    payload = json.loads(result.stdout)
    assert "****" in payload["api_key"]
    if os.name == "posix":
        mode = credential_file.stat().st_mode & 0o777
        assert mode == 0o600, f"凭证文件权限应为 0600，实际 {oct(mode)}"