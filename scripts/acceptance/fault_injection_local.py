"""无容器环境的 A08 降级探测（进程内）。

背景：本地不能跑 Docker，也没有 WSL，而 Milvus 官方轻量版只支持 Linux/macOS，
所以"停容器"这条路走不通。这里改用**依赖不可达地址**制造真实的连接失败：
把 Milvus/Neo4j/Redis/推理地址指向一个无人监听的端口，进程内重建应用后探测，
效果与依赖中断在客户端侧一致（连接拒绝、超时、明确报错）。

验证的是 A08 的核心要求：**依赖不可用时必须是明确错误或已批准降级，不能静默返回
错误结果，也不能 500 崩溃**；恢复配置后必须回到健康。

局限（必须写进结论）：
- 只覆盖"依赖不可达"，不覆盖"依赖抖动/半死/超时重连"；
- 没有真实依赖时无法验证"恢复后数据一致"，那条需要在目标环境做。

用法：
    python scripts/acceptance/fault_injection_local.py
    python scripts/acceptance/fault_injection_local.py --env-file .env.local-staging \
        --api-key <app_key> --kb-id 123
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

SINGLETONS = [
    ("judicial_ai.services.vector_store", "get_vector_store"),
    ("judicial_ai.services.graph_store", "get_graph_store"),
    ("judicial_ai.services.inference", "get_inference_client"),
    ("judicial_ai.services.cache", "get_cache"),
    ("judicial_ai.services.quota", "get_rate_limiter"),
    ("judicial_ai.services.decision", "get_decision_model"),
]


@dataclass
class Stage:
    name: str
    overrides: dict[str, str]
    expect: str  # healthy | degraded
    note: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)


STAGES = [
    Stage("baseline", {}, "healthy", "注入前基线"),
    Stage(
        "redis-down",
        {"REDIS_URL": "redis://127.0.0.1:6390/0", "RATE_LIMIT_FAIL_OPEN": "false"},
        "degraded",
        "限流关闭 fail-open 后，Redis 不可达必须成为 critical",
    ),
    Stage(
        "graph-down",
        {
            "GRAPH_ENABLED": "true",
            "GRAPH_BACKEND": "neo4j",
            "NEO4J_URI": "bolt://127.0.0.1:7690",
            "NEO4J_USER": "neo4j",
            "NEO4J_PASSWORD": "unreachable",
            "GRAPH_FAIL_OPEN": "false",
        },
        "degraded",
        "图谱关闭 fail-open 后，Neo4j 不可达必须成为 critical",
    ),
    Stage(
        "inference-down",
        {
            "INFERENCE_MODE": "proxy",
            "INFERENCE_BASE_URL": "http://127.0.0.1:8099/v1",
        },
        "degraded",
        "proxy 模式下推理不可达必须成为 critical",
    ),
    Stage(
        "vector-down",
        {
            "VECTOR_BACKEND": "milvus",
            "MILVUS_URI": "http://127.0.0.1:19531",
            "VECTOR_FALLBACK_SQL": "false",
        },
        "degraded",
        "禁止 SQL 兜底后，Milvus 不可达必须成为 critical",
    ),
    Stage("recovered", {}, "healthy", "恢复配置后必须回到 ready"),
]


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def reset_singletons() -> None:
    """清掉配置与各依赖单例，让下一阶段按新环境重建。"""

    import importlib

    from judicial_ai.config import get_settings

    get_settings.cache_clear()
    for module_name, function_name in SINGLETONS:
        module = importlib.import_module(module_name)
        func = getattr(module, function_name, None)
        if func is not None and hasattr(func, "cache_clear"):
            func.cache_clear()


def apply_overrides(overrides: dict[str, str]) -> dict[str, str | None]:
    saved: dict[str, str | None] = {}
    for key, value in overrides.items():
        saved[key] = os.environ.get(key)
        os.environ[key] = value
    return saved


def restore(saved: dict[str, str | None]) -> None:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def probe(stage: Stage, api_key: str | None, kb_id: int | None) -> dict[str, Any]:
    from fastapi.testclient import TestClient

    from judicial_ai.app import create_app

    app = create_app()
    result: dict[str, Any] = {"stage": stage.name, "note": stage.note}
    # base_url 必须落在 TRUSTED_HOSTS 内，否则 TrustedHostMiddleware 直接 400。
    with TestClient(app, base_url="http://127.0.0.1") as client:
        ready = client.get("/readyz")
        try:
            body = ready.json()
        except ValueError:
            body = {}
        components = {
            name: (item or {}).get("status")
            for name, item in (body.get("components") or {}).items()
        }
        failures = list(body.get("critical_failures") or [])
        result.update(
            {
                "readyz_status": ready.status_code,
                "status": body.get("status"),
                "components": components,
                "critical_failures": failures,
            }
        )

        healthy = ready.status_code == 200 and not failures
        if stage.expect == "healthy":
            stage.steps.append(
                {
                    "sid": "readyz",
                    "status": PASS if healthy else FAIL,
                    "detail": f"HTTP {ready.status_code}, critical={failures}",
                }
            )
        else:
            explicit = ready.status_code == 503 or bool(failures)
            stage.steps.append(
                {
                    "sid": "readyz",
                    "status": PASS if explicit else FAIL,
                    "detail": f"HTTP {ready.status_code}, critical={failures}",
                }
            )

        if api_key and kb_id:
            retrieved = client.post(
                "/v1/retrieve",
                json={"kb_ids": [kb_id], "query": "借款利率上限", "top_k": 5},
                headers={"Authorization": f"Bearer {api_key}"},
            )
            # 明确错误（4xx/503）可接受；500 说明异常没被兜住。
            acceptable = retrieved.status_code != 500
            detail = f"HTTP {retrieved.status_code}"
            if retrieved.status_code == 200:
                payload = retrieved.json()
                usage = payload.get("usage") or {}
                detail += (
                    f", hits={len(payload.get('results') or [])}, "
                    f"recall_mode={usage.get('recall_mode')}"
                )
            stage.steps.append(
                {
                    "sid": "retrieve",
                    "status": PASS if acceptable else FAIL,
                    "detail": detail,
                }
            )
        else:
            stage.steps.append(
                {
                    "sid": "retrieve",
                    "status": SKIP,
                    "detail": "未提供 --api-key/--kb-id，仅验证健康探测",
                }
            )
    return result


def run(args: argparse.Namespace) -> int:
    env_path = Path(args.env_file)
    if not env_path.is_absolute():
        env_path = ROOT / env_path
    if not env_path.exists():
        print(f"环境文件不存在：{env_path}", file=sys.stderr)
        return 2

    # 必须在导入 judicial_ai 之前写入环境：db.py 在导入时就建 engine。
    # 外部已设置的变量优先（便于临时换库或临时改开关）。
    for key, value in load_env_file(env_path).items():
        os.environ.setdefault(key, value)
    os.environ["APP_ENV"] = args.app_env or os.environ.get("APP_ENV", "local-staging")

    stages = STAGES
    if args.only:
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        stages = [stage for stage in STAGES if stage.name in wanted]
        if not stages:
            print(f"没有匹配的阶段：{args.only}", file=sys.stderr)
            return 2

    evidence: list[dict[str, Any]] = []
    exit_code = 0
    for stage in stages:
        saved = apply_overrides(stage.overrides)
        try:
            reset_singletons()
            evidence.append(probe(stage, args.api_key, args.kb_id))
        except Exception as exc:  # noqa: BLE001 - 任一阶段失败都要继续执行恢复
            stage.steps.append(
                {"sid": "exception", "status": FAIL, "detail": f"{type(exc).__name__}: {exc}"[:300]}
            )
            evidence.append({"stage": stage.name, "error": f"{type(exc).__name__}: {exc}"})
            exit_code = 1
        finally:
            restore(saved)

        failed = [step for step in stage.steps if step["status"] == FAIL]
        print(f"{stage.name}: {'PASS' if not failed else 'FAIL'}")
        for step in failed:
            print(f"  - {step['sid']}: {step['detail']}")
        if failed:
            exit_code = 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.evidence_dir) / f"local-faults-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
                "env_file": str(env_path),
                "scope": "依赖不可达（进程内），不含数据一致性校验",
                "stages": [
                    {"name": stage.name, "expect": stage.expect, "steps": stage.steps}
                    for stage in stages
                ],
                "evidence": evidence,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"证据目录：{out_dir}")
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="无容器 A08 降级探测")
    parser.add_argument("--env-file", default=".env.local-staging")
    parser.add_argument("--app-env", default="local-staging")
    parser.add_argument("--api-key", default=None, help="可选：应用凭证，用于业务请求探测")
    parser.add_argument("--kb-id", type=int, default=None, help="可选：知识库 ID")
    parser.add_argument("--evidence-dir", default="evidence")
    parser.add_argument(
        "--only", default="", help="只跑指定阶段，逗号分隔，如 redis-down,recovered"
    )
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
