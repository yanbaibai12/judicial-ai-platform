from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, cast

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .errors import ApiError
from .metrics import record_auth_failure
from .models import App, AppCredential, Tenant, utcnow
from .services.cache import get_cache

READER_PERMISSIONS = frozenset(
    {
        "capability:read",
        "kb:read",
        "governance:read",
        "system:read",
        "job:read",
        "audit:read",
        "usage:read",
    }
)
OPERATOR_PERMISSIONS = READER_PERMISSIONS | frozenset(
    {
        "capability:write",
        "kb:write",
        "kb:approve",
        "kb:promote",
        "governance:write",
        "job:write",
        "mcp:write",
        "audit:read",
        "usage:read",
    }
)


@dataclass
class Principal:
    kind: str  # admin | app | user
    tenant_id: int | None = None
    app_id: int | None = None
    app_code: str | None = None
    credential_id: int | None = None
    user_id: str | None = None
    username: str | None = None
    display_name: str | None = None
    email: str | None = None
    roles: tuple[str, ...] = field(default_factory=tuple)
    permissions: frozenset[str] = field(default_factory=frozenset)
    auth_method: str = "token"

    @property
    def is_admin(self) -> bool:
        return self.kind == "admin" or "platform-admin" in self.roles

    def has_permission(self, permission: str) -> bool:
        if self.kind == "admin":
            return True
        return "*" in self.permissions or permission in self.permissions


HASH_ALGO_HMAC = "hmac-sha256"
HASH_ALGO_LEGACY_SHA256 = "sha256"


def hash_token_with_secret(token: str, secret: str) -> str:
    """以指定 secret 计算密钥化指纹。禁止缓存结果，避免密钥轮换后命中陈旧值。"""
    digest = hmac.new(secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{HASH_ALGO_HMAC}${digest}"


def hash_token(token: str) -> str:
    return hash_token_with_secret(token, get_settings().api_key_hash_secret)


def sha256_token(token: str) -> str:
    """裸 SHA-256，仅用于识别历史存量哈希，不再用于新建凭证。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def split_stored_digest(value: str) -> tuple[str, str]:
    """把存储值拆成 (hash_algo, digest)，识别带前缀与历史裸哈希两种形态。"""
    if value.startswith(f"{HASH_ALGO_HMAC}$"):
        return HASH_ALGO_HMAC, value.split("$", 1)[1]
    return HASH_ALGO_LEGACY_SHA256, value


def token_digest_candidates(token: str) -> list[tuple[str, str]]:
    """按设计候选顺序生成可能的 digest，供逐个常量时间比较。

    返回 `(hash_algo, digest_hex)` 列表，顺序：主 secret → legacy secrets →
    历史裸 SHA-256。存储态中的算法前缀不参与比较，只做记录。命中裸哈希或
    legacy 时，一律以主 secret 升格写回。每次调用都重新读取配置，避免密钥
    轮换后出现缓存命中陈旧 secret 的情况。
    """
    settings = get_settings()
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for secret in (settings.api_key_hash_secret, *_legacy_hash_secrets(settings)):
        digest = hmac.new(
            secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if digest not in seen:
            seen.add(digest)
            candidates.append((HASH_ALGO_HMAC, digest))
    legacy_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if legacy_digest not in seen:
        candidates.append((HASH_ALGO_LEGACY_SHA256, legacy_digest))
    return candidates


def _legacy_hash_secrets(settings: Any) -> tuple[str, ...]:
    raw = getattr(settings, "api_key_hash_legacy_secrets", "") or ""
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def generate_api_key() -> tuple[str, str, str]:
    """返回 (明文 key, 哈希, 前缀)。明文只返回一次。"""
    token = f"ak_{secrets.token_urlsafe(32)}"
    return token, hash_token(token), token[:10]


def _constant_time_equal(left: str, right: str) -> bool:
    try:
        return secrets.compare_digest(left, right)
    except TypeError:
        return False


def _extract_credential(request: Request) -> tuple[str, str, str]:
    value = request.headers.get("Authorization", "")
    if not value:
        raise ApiError("AUTH_INVALID", "缺少认证信息", 401)
    scheme, _, token = value.partition(" ")
    scheme = scheme.lower()
    token = token.strip()
    if scheme == "bearer" and token:
        return scheme, token, ""
    if scheme == "basic" and token:
        try:
            decoded = base64.b64decode(token, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise ApiError("AUTH_INVALID", "Basic 认证格式错误", 401) from exc
        username, _, password = decoded.partition(":")
        if not username:
            raise ApiError("AUTH_INVALID", "Basic 用户名不能为空", 401)
        return scheme, username, password
    raise ApiError("AUTH_INVALID", "不支持的认证方式", 401)


def _auth_failure_key(request: Request) -> str:
    client_ip = getattr(request.state, "client_ip", None) or (
        request.client.host if request.client else "unknown"
    )
    return f"authfail:{client_ip}"


def _assert_not_locked_out(request: Request) -> None:
    settings = get_settings()
    if not settings.auth_failure_limit:
        return
    raw = get_cache().get(_auth_failure_key(request))
    if raw and int(raw) >= settings.auth_failure_limit:
        raise ApiError(
            "AUTH_LOCKED",
            "认证失败次数过多，请稍后重试",
            429,
            {"retry_after_seconds": settings.auth_failure_window_seconds},
        )


def _record_auth_failure(request: Request, reason: str) -> None:
    settings = get_settings()
    record_auth_failure(reason)
    if settings.auth_failure_limit:
        get_cache().incr(_auth_failure_key(request), settings.auth_failure_window_seconds)


def _clear_auth_failures(request: Request) -> None:
    get_cache().delete(_auth_failure_key(request))


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item]
    return []


def _role_permissions(roles: set[str]) -> frozenset[str]:
    settings = get_settings()
    if roles & settings.oidc_admin_role_set:
        return frozenset({"*"})
    permissions: set[str] = set()
    if roles & settings.oidc_operator_role_set:
        permissions.update(OPERATOR_PERMISSIONS)
    if roles & settings.oidc_reader_role_set or permissions:
        permissions.update(READER_PERMISSIONS)
    return frozenset(permissions)


@lru_cache
def _jwk_client(url: str) -> Any:
    import jwt

    return jwt.PyJWKClient(url, timeout=get_settings().oidc_timeout_seconds)


def _oidc_principal(db: Session, token: str) -> Principal:
    settings = get_settings()
    if not (settings.oidc_issuer or settings.oidc_jwks_url):
        raise ApiError("AUTH_CONFIG", "OIDC 未配置 issuer 或 jwks_url", 503)
    try:
        import jwt
    except ImportError as exc:
        raise ApiError("AUTH_CONFIG", "缺少 PyJWT 依赖，请安装 PyJWT[crypto]", 503) from exc

    issuer = settings.oidc_issuer or ""
    jwks_url = settings.oidc_jwks_url or (issuer.rstrip("/") + "/.well-known/jwks.json")
    try:
        signing_key = _jwk_client(jwks_url).get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=settings.oidc_algorithm_list,
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer,
            options={"require": ["exp", "sub"]},
        )
    except Exception as exc:  # noqa: BLE001 - JWT library raises many exception types
        raise ApiError("AUTH_INVALID", "OIDC Token 无效或已过期", 401) from exc

    roles = set(_as_string_list(claims.get("roles")))
    roles.update(_as_string_list(claims.get("groups")))
    realm_access = claims.get("realm_access")
    if isinstance(realm_access, dict):
        roles.update(_as_string_list(realm_access.get("roles")))
    permissions = set(_as_string_list(claims.get("permissions")))
    permissions.update(_role_permissions(roles))
    if not roles and not permissions:
        permissions.update(READER_PERMISSIONS)

    tenant_value = claims.get("tenant_id") or claims.get("tenant") or claims.get("org_id")
    try:
        tenant_id = (
            int(tenant_value)
            if tenant_value is not None
            else settings.oidc_default_tenant_id
        )
    except (TypeError, ValueError):
        tenant_id = settings.oidc_default_tenant_id

    subject = str(claims.get("sub"))

    # 令牌撤销检查：必须在验签通过之后、返回主体之前完成。
    from .services.revocation import is_token_revoked, to_datetime

    jti = claims.get("jti")
    revoked, revoke_reason = is_token_revoked(
        db,
        jti=str(jti) if jti else None,
        subject=subject,
        issued_at=to_datetime(claims.get("iat")),
    )
    if revoked:
        raise ApiError(
            "TOKEN_REVOKED",
            "令牌已被撤销，请重新认证",
            401,
            {"reason": revoke_reason} if revoke_reason else None,
        )

    return Principal(
        kind="user",
        tenant_id=tenant_id,
        user_id=subject,
        username=str(claims.get("preferred_username") or claims.get("email") or subject),
        display_name=str(claims.get("name") or claims.get("preferred_username") or subject),
        email=str(claims.get("email")) if claims.get("email") else None,
        roles=tuple(sorted(roles)),
        permissions=frozenset(permissions),
        auth_method="oidc",
    )


def _ldap_group_match(groups: set[str], allowed: set[str]) -> bool:
    normalized_groups = {group.strip().lower() for group in groups}
    for allowed_group in allowed:
        allowed_lower = allowed_group.strip().lower()
        if allowed_lower in normalized_groups:
            return True
        if any(allowed_lower in group for group in normalized_groups):
            return True
    return False


def _ldap_principal(username: str, password: str) -> Principal:
    settings = get_settings()
    if not settings.ldap_server:
        raise ApiError("AUTH_CONFIG", "LDAP 未配置 LDAP_SERVER", 503)
    try:
        from ldap3 import NONE, Connection, Server
    except ImportError as exc:
        raise ApiError("AUTH_CONFIG", "缺少 ldap3 依赖", 503) from exc

    bind_dn = settings.ldap_bind_dn_template.format(username=username)
    try:
        server = Server(
            settings.ldap_server,
            use_ssl=settings.ldap_use_tls,
            get_info=NONE,
            connect_timeout=settings.oidc_timeout_seconds,
        )
        conn = Connection(
            server,
            user=bind_dn,
            password=password,
            auto_bind=True,
            receive_timeout=settings.oidc_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001
        raise ApiError("AUTH_INVALID", "LDAP 用户名或密码错误", 401) from exc

    groups: set[str] = set()
    display_name = username
    email: str | None = None
    try:
        if settings.ldap_user_search_base:
            conn.search(
                search_base=settings.ldap_user_search_base,
                search_filter=settings.ldap_user_search_filter.format(username=username),
                attributes=[settings.ldap_group_attribute, "mail", "displayName"],
                size_limit=1,
            )
            if conn.entries:
                entry = conn.entries[0]
                bind_dn = entry.entry_dn
                try:
                    attribute = entry[settings.ldap_group_attribute]
                    groups.update(
                        str(item) for item in _as_string_list(attribute.values)
                    )
                except (KeyError, AttributeError):
                    pass
                if "mail" in entry:
                    email = str(entry.mail)
                if "displayName" in entry:
                    display_name = str(entry.displayName)
    finally:
        conn.unbind()

    roles: set[str] = set()
    if _ldap_group_match(groups, settings.ldap_admin_group_set):
        roles.add("platform-admin")
    if _ldap_group_match(groups, settings.ldap_operator_group_set):
        roles.add("platform-operator")
    if _ldap_group_match(groups, settings.ldap_reader_group_set):
        roles.add("platform-reader")
    if not roles:
        roles.add("platform-reader")
    return Principal(
        kind="user",
        tenant_id=settings.ldap_default_tenant_id,
        user_id=bind_dn,
        username=username,
        display_name=display_name,
        email=email,
        roles=tuple(sorted(roles)),
        permissions=_role_permissions(roles),
        auth_method="ldap",
    )


def _match_credential_digest(token: str, credential: AppCredential) -> bool:
    """逐候选比较；固定顺序扫描全部候选，减少按命中位置的时序差异。

    不因"哪把 secret 命中"或"哪种算法命中"返回不同错误，对外一律统一语义。
    """
    _, stored_digest = split_stored_digest(str(credential.key_hash or ""))
    matched = False
    for _, candidate_digest in token_digest_candidates(token):
        if _constant_time_equal(candidate_digest, stored_digest):
            matched = True
    return matched


def _upgrade_credential_digest(db: Session, credential: AppCredential, token: str) -> None:
    """把命中旧算法/旧 secret 的哈希升格为主 secret 的密钥化哈希。

    撞到 `key_hash` 唯一约束时放弃写回（同一明文已登记过新哈希），鉴权结果
    不受影响；该场景记 `credential.rehash_conflict` 审计供运维排查。
    """
    primary_stored = hash_token(token)
    if credential.key_hash == primary_stored:
        return
    from .services.observability import audit  # noqa: PLC0415 - 避免模块级循环导入

    before_algo = str(credential.hash_algo or "")
    algo, _, _ = primary_stored.partition("$")
    credential.key_hash = cast(Any, primary_stored)
    credential.hash_algo = cast(Any, algo)
    try:
        db.flush()
        db.commit()
    except Exception:  # noqa: BLE001 - 唯一约束等冲突不应影响鉴权结果
        db.rollback()
        audit(
            db,
            Principal(kind="app", tenant_id=None, app_code="system"),
            "credential.rehash_conflict",
            "app_credential",
            cast(int, credential.id),
            before={"hash_algo": before_algo},
            after={"error": "unique_conflict"},
        )
        db.commit()
        return
    audit(
        db,
        Principal(kind="app", tenant_id=None, app_code="system"),
        "credential.rehash",
        "app_credential",
        cast(int, credential.id),
        before={"hash_algo": before_algo},
        after={"hash_algo": algo, "key_prefix": credential.key_prefix},
    )
    db.commit()


def _app_principal(db: Session, token: str) -> Principal | None:
    candidates = token_digest_candidates(token)
    probes = [f"{algo}${digest}" for algo, digest in candidates]
    probes.extend(digest for algo, digest in candidates if algo == HASH_ALGO_LEGACY_SHA256)
    credential = (
        db.query(AppCredential)
        .filter(AppCredential.key_hash.in_(probes), AppCredential.status == "ACTIVE")
        .first()
    )
    if credential is None or not _match_credential_digest(token, credential):
        return None
    if credential.expires_at and credential.expires_at < utcnow():
        raise ApiError("TOKEN_EXPIRED", "凭证已过期", 401)
    app = db.get(App, credential.app_id)
    if app is None or app.status != "ACTIVE" or app.deleted_at is not None:
        raise ApiError("AUTH_INVALID", "应用不可用", 401)
    tenant = db.get(Tenant, app.tenant_id)
    if tenant is None or tenant.status != "ACTIVE" or tenant.deleted_at is not None:
        raise ApiError("AUTH_INVALID", "租户不可用", 401)

    credential.last_used_at = utcnow()
    _upgrade_credential_digest(db, credential, token)
    db.commit()
    return Principal(
        kind="app",
        tenant_id=app.tenant_id,
        app_id=app.id,
        app_code=app.code,
        credential_id=credential.id,
        permissions=frozenset(app.permissions or ["app:invoke"]),
        auth_method="api_key",
    )


def _authenticate(request: Request, db: Session) -> Principal:
    settings = get_settings()
    scheme, token, password = _extract_credential(request)
    if scheme == "basic":
        if settings.auth_mode not in {"ldap", "hybrid"}:
            raise ApiError("AUTH_INVALID", "当前认证模式不支持 Basic", 401)
        return _ldap_principal(token, password)

    if settings.allow_static_admin and _constant_time_equal(token, settings.admin_token):
        return Principal(kind="admin", app_code="admin", permissions=frozenset({"*"}))

    if token.startswith("ak_"):
        principal = _app_principal(db, token)
        if principal is not None:
            return principal
        raise ApiError("AUTH_INVALID", "凭证无效", 401)

    if settings.auth_mode in {"oidc", "hybrid"}:
        return _oidc_principal(db, token)

    principal = _app_principal(db, token)
    if principal is not None:
        return principal
    raise ApiError("AUTH_INVALID", "凭证无效", 401)


def get_principal(request: Request, db: Session = Depends(get_db)) -> Principal:
    _assert_not_locked_out(request)
    try:
        principal = _authenticate(request, db)
    except ApiError as exc:
        if exc.code not in {"AUTH_LOCKED", "AUTH_CONFIG"}:
            _record_auth_failure(request, exc.code)
        raise
    if principal.kind == "user" and principal.tenant_id is None and not principal.is_admin:
        raise ApiError("FORBIDDEN", "用户未绑定租户", 403)
    _clear_auth_failures(request)
    request.state.principal = principal
    request.state.tenant_id = principal.tenant_id
    return principal


def require_admin(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.is_admin:
        raise ApiError("FORBIDDEN", "需要管理员权限", 403)
    return principal


def require_app(principal: Principal = Depends(get_principal)) -> Principal:
    if principal.kind != "app":
        raise ApiError("FORBIDDEN", "需要应用身份", 403)
    if not principal.has_permission("app:invoke"):
        raise ApiError("FORBIDDEN", "应用缺少 app:invoke 权限", 403)
    return principal


def require_any(principal: Principal = Depends(get_principal)) -> Principal:
    return principal


def require_permission(permission: str) -> Callable[..., Principal]:
    def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if not principal.has_permission(permission):
            raise ApiError("FORBIDDEN", f"缺少权限：{permission}", 403)
        return principal

    return dependency

