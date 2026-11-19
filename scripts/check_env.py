"""staging / 生产环境变量预检。

解决的问题：`.env` 里少写一项，代码会静默回落到本地默认值（SQLite、mock 推理、
SQL 向量兜底、ACL 关闭），服务照常启动但验收结论无效；而 `APP_ENV=prod` 时
`app.py` 的启动闸门又会在启动瞬间抛错。本脚本在**启动之前**把两类问题都列出来。

用法：
    python scripts/check_env.py --env-file .env.staging
    python scripts/check_env.py --env-file .env.staging --strict-static-admin

退出码：0 全部通过（允许 WARN）；1 存在 FAIL。
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

DEFAULT_HASH_SECRET = "dev-api-key-hash-secret"
DEFAULT_ADMIN_TOKENS = {"", "change-me", "change-me-local-admin", "test-admin", "admin"}

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"


@dataclass
class Check:
    name: str
    level: str
    detail: str


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        values[key] = value
    return values


def _is_placeholder(value: str) -> bool:
    return "<" in value and ">" in value


def check_all(env: dict[str, str], *, strict_static_admin: bool) -> list[Check]:
    from judicial_ai.config import Settings

    # _env_file=None：只信任传入文件，避免被仓库根目录的 .env 覆盖。
    settings = Settings(
        _env_file=None,
        **{key.lower(): value for key, value in env.items()},
    )
    checks: list[Check] = []

    def add(name: str, ok: bool, detail: str, *, warn_only: bool = False) -> None:
        level = PASS if ok else (WARN if warn_only else FAIL)
        checks.append(Check(name, level, detail))

    def placeholder(key: str) -> bool:
        return _is_placeholder(env.get(key, ""))

    # 1. 数据库
    add(
        "DATABASE_URL 非 SQLite",
        not settings.is_sqlite,
        "生产环境禁止 SQLite" if settings.is_sqlite else settings.database_url.split("@")[-1],
    )
    add(
        "DATABASE_URL 无占位符",
        not placeholder("DATABASE_URL"),
        "仍含 <...> 占位符" if placeholder("DATABASE_URL") else "ok",
    )

    # 2. 凭证与令牌
    secret = settings.api_key_hash_secret or ""
    add(
        "API_KEY_HASH_SECRET ≥32 且非默认",
        len(secret) >= 32
        and secret != DEFAULT_HASH_SECRET
        and not placeholder("API_KEY_HASH_SECRET"),
        f"当前长度 {len(secret)}",
    )
    admin_token = settings.admin_token or ""
    add(
        "ADMIN_TOKEN ≥32 且非默认值",
        len(admin_token) >= 32
        and admin_token not in DEFAULT_ADMIN_TOKENS
        and not placeholder("ADMIN_TOKEN"),
        f"当前长度 {len(admin_token)}",
    )
    legacy = [
        item.strip()
        for item in (settings.api_key_hash_legacy_secrets or "").split(",")
        if item.strip()
    ]
    add(
        "legacy 哈希密钥与主密钥不重复",
        not legacy or not (set(legacy) & {secret}),
        f"legacy 数量 {len(legacy)}",
    )

    # 3. 静态管理员（生产必须关闭）
    if strict_static_admin:
        add(
            "ALLOW_STATIC_ADMIN=false",
            not settings.allow_static_admin,
            f"当前 {settings.allow_static_admin}",
        )
    else:
        add(
            "ALLOW_STATIC_ADMIN=false",
            not settings.allow_static_admin,
            "staging 验收例外：验收结束必须改为 false",
            warn_only=True,
        )

    # 4. CORS / Host / HTTPS
    add(
        "CORS_ORIGINS 非通配符",
        settings.cors_origin_list != ["*"],
        ",".join(settings.cors_origin_list),
    )
    add(
        "TRUSTED_HOSTS 非通配符",
        settings.trusted_host_list != ["*"],
        ",".join(settings.trusted_host_list),
    )
    add("HSTS_ENABLED", settings.hsts_enabled, f"当前 {settings.hsts_enabled}")

    # 5. 权限与索引治理
    add(
        "RESOURCE_ACL_ENFORCED=true",
        settings.resource_acl_enforced,
        "关闭时资源级授权形同装饰",
    )
    add("INDEX_REQUIRE_APPROVAL=true", settings.index_require_approval, "索引未经审批可直接发布")
    add("REQUIRE_PROD_ALIAS=true", settings.require_prod_alias, "检索可绕过 prod alias")

    # 6. 推理
    add(
        "INFERENCE_MODE=proxy",
        settings.inference_mode == "proxy",
        "mock 模式下所有回答都是模板文本",
    )
    add(
        "INFERENCE_BASE_URL 无占位符",
        not placeholder("INFERENCE_BASE_URL"),
        settings.inference_base_url,
    )

    # 7. 向量与图谱
    add(
        "VECTOR_BACKEND=milvus",
        settings.vector_backend == "milvus",
        f"当前 {settings.vector_backend}（SQL 兜底只算全表余弦）",
    )
    add("MILVUS_URI 已配置", bool(settings.milvus_uri), settings.milvus_uri or "缺失")
    add(
        "VECTOR_FALLBACK_SQL=false",
        not settings.vector_fallback_sql,
        "允许兜底时 Milvus 故障会静默退化为 SQL 检索",
    )
    add("GRAPH_ENABLED=true", settings.graph_enabled, "图谱关闭时无 GraphRAG")
    add(
        "GRAPH_BACKEND=neo4j",
        settings.graph_backend == "neo4j",
        f"当前 {settings.graph_backend}",
    )
    add(
        "NEO4J_PASSWORD 已配置",
        bool(settings.neo4j_password),
        "缺失" if not settings.neo4j_password else "ok",
    )
    add(
        "GRAPH_FAIL_OPEN=false",
        not settings.graph_fail_open,
        "允许 fail-open 时图谱故障不可见",
    )

    # 8. 缓存与限流
    add("REDIS_URL 已配置", bool(settings.redis_url), settings.redis_url or "缺失")
    add(
        "RATE_LIMIT_FAIL_OPEN=false",
        not settings.rate_limit_fail_open or not settings.rate_limit_enabled,
        "Redis 故障时会放行全部请求",
    )

    # 9. 身份与撤销
    add(
        "AUTH_MODE 非 token（A07 需要真实 IdP）",
        settings.auth_mode in {"oidc", "hybrid"},
        f"当前 {settings.auth_mode}",
        warn_only=True,
    )
    add(
        "TOKEN_REVOCATION_ENABLED=true",
        settings.token_revocation_enabled,
        "关闭时无法满足 A07 撤销生效时间",
    )
    add(
        "TOKEN_REVOCATION_CACHE_SECONDS ≤5",
        settings.token_revocation_cache_seconds <= 5,
        f"当前 {settings.token_revocation_cache_seconds}s，即撤销生效时间上限",
        warn_only=True,
    )

    # 10. 任务与对象存储
    add(
        "JOB_MODE=worker",
        settings.job_mode == "worker",
        "inline 模式下索引任务在请求内同步执行，无法验证积压与重试",
    )
    add(
        "OBJECT_STORE_PATH 已配置",
        bool(settings.object_store_path),
        settings.object_store_path or "缺失",
    )
    add(
        "CONTRACT_REVIEW_ENABLED=false（示例规则）",
        not settings.contract_review_enabled,
        "示例规则未经法律审核，staging 不应开放",
        warn_only=True,
    )

    # 11. 决策链路
    add(
        "DECISION_PROVIDER=rule（JEV 不得切主）",
        settings.decision_provider == "rule",
        f"当前 {settings.decision_provider}",
    )
    add(
        "DECISION_JEV_CANARY_PERCENT=0",
        settings.decision_jev_canary_percent == 0,
        f"当前 {settings.decision_jev_canary_percent}%",
    )
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="staging/生产环境变量预检")
    parser.add_argument("--env-file", default=".env.staging", help="环境变量文件路径")
    parser.add_argument(
        "--strict-static-admin",
        action="store_true",
        help="要求 ALLOW_STATIC_ADMIN=false（生产口径，staging 验收时不要加）",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args(argv)

    path = Path(args.env_file)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        print(f"环境文件不存在：{path}", file=sys.stderr)
        return 1

    env = load_env_file(path)
    for key, value in env.items():
        os.environ[key] = value

    checks = check_all(env, strict_static_admin=args.strict_static_admin)
    failures = [item for item in checks if item.level == FAIL]
    warnings = [item for item in checks if item.level == WARN]

    if args.json:
        import json

        print(
            json.dumps(
                {
                    "env_file": str(path),
                    "passed": len([c for c in checks if c.level == PASS]),
                    "warnings": [c.__dict__ for c in warnings],
                    "failures": [c.__dict__ for c in failures],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"预检文件：{path}")
        print("-" * 78)
        for item in checks:
            print(f"[{item.level:4}] {item.name:44} {item.detail}")
        print("-" * 78)
        passed = len([c for c in checks if c.level == PASS])
        print(f"通过 {passed} / 警告 {len(warnings)} / 失败 {len(failures)}")
        if failures:
            print("\n以下项目必须修正后才能进入验收：")
            for item in failures:
                print(f"  - {item.name}：{item.detail}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
