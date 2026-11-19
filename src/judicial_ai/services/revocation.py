"""OIDC 令牌撤销（`specs/target-architecture` T12）。

平台不签发令牌，因此不实现 refresh；平台负责的是**让已签发的令牌立即失效**：

- 精确撤销：按 JWT 的 ``jti`` 拒绝单个令牌；
- 主体撤销：按 ``sub`` 拒绝某时刻之前签发的全部令牌（离职、降权、疑似泄露）；
- 判定结果带本地缓存，缓存时长即"撤销生效时间"的上限，可通过
  ``TOKEN_REVOCATION_CACHE_SECONDS`` 调小到 0 以获得即时生效。

撤销记录都有 ``expires_at``：令牌自然过期后再无意义，可安全清理。
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from ..config import get_settings
from ..errors import ApiError
from ..models import TokenRevocation, utcnow

KIND_JTI = "jti"
KIND_SUBJECT = "subject"

DEFAULT_RETENTION_SECONDS = 24 * 3600

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, bool]] = {}


def _cache_key(kind: str, value: str, issued_at: datetime | None) -> str:
    stamp = int(issued_at.timestamp()) if issued_at else 0
    return f"{kind}:{value}:{stamp}"


def invalidate_cache() -> None:
    """撤销记录变更后清空缓存，避免撤销在缓存窗口内不生效。"""

    with _cache_lock:
        _cache.clear()


def _cache_get(key: str) -> bool | None:
    ttl = get_settings().token_revocation_cache_seconds
    if ttl <= 0:
        return None
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= now:
            _cache.pop(key, None)
            return None
        return value


def _cache_set(key: str, value: bool) -> None:
    ttl = get_settings().token_revocation_cache_seconds
    if ttl <= 0:
        return
    with _cache_lock:
        if len(_cache) > 4096:
            _cache.clear()
        _cache[key] = (time.monotonic() + ttl, value)


def to_datetime(issued_at: Any) -> datetime | None:
    """把 JWT ``iat``（秒或毫秒时间戳）转成 naive UTC datetime。"""

    if issued_at is None:
        return None
    if isinstance(issued_at, datetime):
        return issued_at.replace(tzinfo=None) if issued_at.tzinfo else issued_at
    try:
        value = float(issued_at)
    except (TypeError, ValueError):
        return None
    if value > 1e12:  # 毫秒时间戳
        value /= 1000.0
    return datetime.fromtimestamp(value, tz=UTC).replace(tzinfo=None)


def is_token_revoked(
    db: Session,
    *,
    jti: str | None = None,
    subject: str | None = None,
    issued_at: datetime | None = None,
) -> tuple[bool, str | None]:
    """返回 ``(是否已撤销, 原因)``；未启用撤销或未命中则返回 ``(False, None)``。"""

    if not get_settings().token_revocation_enabled:
        return False, None
    if not jti and not subject:
        return False, None

    now = utcnow()
    checks: list[tuple[str, str]] = []
    if jti:
        checks.append((KIND_JTI, jti))
    if subject:
        checks.append((KIND_SUBJECT, subject))

    for kind, value in checks:
        key = _cache_key(kind, value, issued_at if kind == KIND_SUBJECT else None)
        cached = _cache_get(key)
        if cached is False:
            continue
        if cached is True:
            return True, _reason_of(db, kind, value, issued_at, now)

        record = (
            db.query(TokenRevocation)
            .filter(TokenRevocation.kind == kind, TokenRevocation.value == value)
            .first()
        )
        hit = False
        if record is not None and not (record.expires_at and record.expires_at < now):
            if kind == KIND_JTI:
                hit = True
            else:
                revoked_before = record.revoked_before or record.created_at
                # 缺失 iat 时无法证明令牌晚于撤销时间，按已撤销处理（fail-closed）
                hit = revoked_before is not None and (
                    issued_at is None or issued_at <= revoked_before
                )
        _cache_set(key, hit)
        if hit:
            return True, (record.reason if record is not None else None)

    return False, None


def _reason_of(
    db: Session, kind: str, value: str, issued_at: datetime | None, now: datetime
) -> str | None:
    record = (
        db.query(TokenRevocation)
        .filter(TokenRevocation.kind == kind, TokenRevocation.value == value)
        .first()
    )
    if record is None:
        return None
    if record.expires_at and record.expires_at < now:
        return None
    if kind == KIND_SUBJECT:
        revoked_before = record.revoked_before or record.created_at
        if revoked_before is None or (issued_at is not None and issued_at > revoked_before):
            return None
    return record.reason


def revoke(
    db: Session,
    *,
    jti: str | None = None,
    subject: str | None = None,
    reason: str | None = None,
    revoked_before: datetime | None = None,
    expires_in_seconds: int | None = None,
    created_by: str = "",
) -> TokenRevocation:
    """新增或更新一条撤销记录（同 kind+value 幂等覆盖）。"""

    if bool(jti) == bool(subject):
        raise ApiError("INVALID_ARGUMENT", "jti 与 subject 必须且只能提供一个", 400)

    kind = KIND_JTI if jti else KIND_SUBJECT
    value = (jti or subject or "").strip()
    if not value:
        raise ApiError("INVALID_ARGUMENT", "撤销目标不能为空", 400)

    retention = expires_in_seconds or DEFAULT_RETENTION_SECONDS
    expires_at = utcnow() + timedelta(seconds=retention)
    record = (
        db.query(TokenRevocation)
        .filter(TokenRevocation.kind == kind, TokenRevocation.value == value)
        .first()
    )
    if record is None:
        record = TokenRevocation(
            kind=kind,
            value=value,
            created_at=utcnow(),
        )
        db.add(record)
    record.reason = reason
    record.revoked_before = revoked_before or utcnow() if kind == KIND_SUBJECT else None
    record.expires_at = expires_at
    record.created_by = created_by
    db.commit()
    db.refresh(record)
    invalidate_cache()
    return record


def delete_revocation(db: Session, revocation_id: int) -> None:
    record = db.get(TokenRevocation, revocation_id)
    if record is None:
        raise ApiError("RESOURCE_NOT_FOUND", "撤销记录不存在", 404)
    db.delete(record)
    db.commit()
    invalidate_cache()


def list_revocations(db: Session, limit: int = 100) -> list[TokenRevocation]:
    return (
        db.query(TokenRevocation)
        .order_by(TokenRevocation.created_at.desc())
        .limit(limit)
        .all()
    )


def to_dict(record: TokenRevocation) -> dict[str, Any]:
    return {
        "id": record.id,
        "kind": record.kind,
        "value": record.value,
        "reason": record.reason,
        "revoked_before": record.revoked_before.isoformat() if record.revoked_before else None,
        "expires_at": record.expires_at.isoformat() if record.expires_at else None,
        "created_by": record.created_by,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }
