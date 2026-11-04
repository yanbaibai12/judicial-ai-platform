"""细粒度资源级 ACL（KB / 文档级授权）。

设计：
- ``ResourceGrant`` 表按 (resource_type, resource_id, subject_kind, subject_id) 唯一，
  level 取值 read < write < owner（owner 隐含 write）。
- ``settings.resource_acl_enforced=False``（默认）时仅做租户隔离，不查 grant 表，
  保证存量部署零影响；开启后未获授权的用户/应用将被拒绝。
- admin / 平台管理员始终放行。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from sqlalchemy.orm import Session

from .config import get_settings
from .errors import ApiError
from .models import Document, ResourceGrant, utcnow
from .security import Principal

LEVEL_ORDER: dict[str, int] = {"read": 0, "write": 1, "owner": 2}

ResourceType = Literal["kb", "document"]
AccessType = Literal["read", "write"]


def _subject_ids(principal: Principal) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    if principal.user_id:
        pairs.append(("user", principal.user_id))
        if principal.username:
            pairs.append(("user", principal.username))
    if principal.app_id is not None:
        pairs.append(("app", str(principal.app_id)))
        if principal.app_code:
            pairs.append(("app", principal.app_code))
    for role in principal.roles:
        pairs.append(("group", role))
    return pairs


def grant_resource(
    db: Session,
    tenant_id: int,
    resource_type: str,
    resource_id: int,
    subject_kind: str,
    subject_id: str,
    level: str = "read",
    granted_by: str = "",
    expires_at: datetime | None = None,
    *,
    commit: bool = True,
) -> ResourceGrant:
    if subject_kind not in {"user", "app", "group"}:
        raise ApiError("INVALID_ARGUMENT", "subject_kind 必须是 user/app/group", 400)
    if level not in LEVEL_ORDER:
        raise ApiError("INVALID_ARGUMENT", "level 必须是 read/write/owner", 400)
    existing = (
        db.query(ResourceGrant)
        .filter(
            ResourceGrant.resource_type == resource_type,
            ResourceGrant.resource_id == resource_id,
            ResourceGrant.subject_kind == subject_kind,
            ResourceGrant.subject_id == subject_id,
        )
        .first()
    )
    if existing is not None:
        existing.level = level
        existing.status = "ACTIVE"
        existing.expires_at = expires_at
        existing.granted_by = granted_by or existing.granted_by
        db.commit() if commit else db.flush()
        db.refresh(existing)
        return existing
    grant = ResourceGrant(
        tenant_id=tenant_id,
        resource_type=resource_type,
        resource_id=resource_id,
        subject_kind=subject_kind,
        subject_id=subject_id,
        level=level,
        granted_by=granted_by,
        expires_at=expires_at,
    )
    db.add(grant)
    db.commit() if commit else db.flush()
    db.refresh(grant)
    return grant


def revoke_resource(
    db: Session,
    resource_type: str,
    resource_id: int,
    subject_kind: str,
    subject_id: str,
    *,
    commit: bool = True,
) -> bool:
    grant = (
        db.query(ResourceGrant)
        .filter(
            ResourceGrant.resource_type == resource_type,
            ResourceGrant.resource_id == resource_id,
            ResourceGrant.subject_kind == subject_kind,
            ResourceGrant.subject_id == subject_id,
        )
        .first()
    )
    if grant is None:
        return False
    grant.status = "REVOKED"
    db.commit() if commit else db.flush()
    return True


def list_grants(
    db: Session,
    resource_type: str,
    resource_id: int,
) -> list[ResourceGrant]:
    return (
        db.query(ResourceGrant)
        .filter(
            ResourceGrant.resource_type == resource_type,
            ResourceGrant.resource_id == resource_id,
            ResourceGrant.status == "ACTIVE",
        )
        .order_by(ResourceGrant.created_at)
        .all()
    )


def has_access(
    db: Session,
    principal: Principal,
    resource_type: str,
    resource_id: int,
    need: str = "read",
) -> bool:
    """判断主体对资源是否具备 need 级别权限（不含租户隔离，租户隔离由调用方负责）。"""
    if need not in LEVEL_ORDER:
        raise ApiError("INVALID_ARGUMENT", f"未知权限级别：{need}", 400)
    if principal.is_admin or principal.kind == "admin":
        return True
    if not get_settings().resource_acl_enforced:
        return True
    now = utcnow()
    grants = (
        db.query(ResourceGrant)
        .filter(
            ResourceGrant.resource_type == resource_type,
            ResourceGrant.resource_id == resource_id,
            ResourceGrant.status == "ACTIVE",
            ResourceGrant.expires_at.is_(None) | (ResourceGrant.expires_at > now),
        )
        .all()
    )
    subjects = _subject_ids(principal)
    required = LEVEL_ORDER[need]
    for grant in grants:
        if (grant.subject_kind, grant.subject_id) not in subjects:
            continue
        if LEVEL_ORDER.get(grant.level, -1) >= required:
            return True
    return False


def assert_access(
    db: Session,
    principal: Principal,
    resource_type: str,
    resource_id: int,
    need: str = "read",
) -> None:
    if not has_access(db, principal, resource_type, resource_id, need):
        raise ApiError(
            "FORBIDDEN",
            f"对{resource_type}:{resource_id}缺少 {need} 权限",
            403,
            {"resource_type": resource_type, "resource_id": resource_id, "need": need},
        )


def accessible_document_ids(
    db: Session, principal: Principal, tenant_id: int, kb_ids: list[int]
) -> set[int]:
    """Return currently readable documents; callers must also check KB access."""
    documents = (
        db.query(Document.id, Document.kb_id, Document.access_policy)
        .filter(
            Document.tenant_id == tenant_id,
            Document.kb_id.in_(kb_ids),
            Document.deleted_at.is_(None),
            Document.status == "ACTIVE",
        )
        .all()
    )
    if principal.is_admin or principal.kind == "admin" or not get_settings().resource_acl_enforced:
        return {row.id for row in documents}

    owners = {
        kb_id for kb_id in kb_ids if has_access(db, principal, "kb", kb_id, "owner")
    }
    visible = {
        row.id for row in documents if row.access_policy == "INHERIT" or row.kb_id in owners
    }
    restricted = [row.id for row in documents if row.access_policy == "RESTRICTED"]
    if restricted:
        grants = (
            db.query(ResourceGrant)
            .filter(
                ResourceGrant.tenant_id == tenant_id,
                ResourceGrant.resource_type == "document",
                ResourceGrant.resource_id.in_(restricted),
                ResourceGrant.status == "ACTIVE",
                ResourceGrant.expires_at.is_(None) | (ResourceGrant.expires_at > utcnow()),
            )
            .all()
        )
        subjects = set(_subject_ids(principal))
        visible.update(
            grant.resource_id for grant in grants
            if (grant.subject_kind, grant.subject_id) in subjects
            and LEVEL_ORDER.get(grant.level, -1) >= LEVEL_ORDER["read"]
        )
    return visible


def grant_to_dict(grant: ResourceGrant) -> dict[str, object]:
    return {
        "id": grant.id,
        "tenant_id": grant.tenant_id,
        "resource_type": grant.resource_type,
        "resource_id": grant.resource_id,
        "subject_kind": grant.subject_kind,
        "subject_id": grant.subject_id,
        "level": grant.level,
        "granted_by": grant.granted_by,
        "status": grant.status,
        "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
        "created_at": grant.created_at.isoformat() if grant.created_at else None,
    }
