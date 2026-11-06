from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import ApiError
from ..ids import new_id
from ..models import (
    App,
    AppCredential,
    Capability,
    CapabilityGrant,
    CapabilityVersion,
    Tenant,
    utcnow,
)
from ..schemas import (
    AppCreate,
    AppPermissionUpdate,
    CapabilityCreate,
    CapabilityVersionCreate,
    GrantCreate,
    TenantCreate,
    TokenRevocationCreate,
)
from ..security import (
    Principal,
    generate_api_key,
    require_admin,
    require_any,
    split_stored_digest,
)
from ..services.capabilities import (
    capability_to_dict,
    check_grant,
    create_capability,
    create_version,
    publish_version,
    resolve_capability,
    rollback_version,
    version_to_dict,
)
from ..services.observability import audit
from ..services.revocation import (
    delete_revocation,
    list_revocations,
    revoke,
)
from ..services.revocation import (
    to_dict as revocation_to_dict,
)

admin_router = APIRouter(prefix="/api/v1/admin", tags=["admin"])
discovery_router = APIRouter(prefix="/api/v1", tags=["capabilities"])


def _response(request: Request, data: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"data": data, "request_id": getattr(request.state, "request_id", "")},
    )


def _tenant_dict(tenant: Tenant) -> dict[str, Any]:
    return {
        "id": tenant.id,
        "code": tenant.code,
        "name": tenant.name,
        "type": tenant.type,
        "status": tenant.status,
        "contact": tenant.contact,
        "metadata": tenant.extra or {},
        "created_at": tenant.created_at.isoformat() if tenant.created_at else None,
    }


def _app_dict(app: App) -> dict[str, Any]:
    return {
        "id": app.id,
        "tenant_id": app.tenant_id,
        "code": app.code,
        "name": app.name,
        "type": app.type,
        "status": app.status,
        "description": app.description,
        "permissions": app.permissions or [],
        "metadata": app.extra or {},
        "created_at": app.created_at.isoformat() if app.created_at else None,
    }


def _credential_dict(credential: AppCredential) -> dict[str, Any]:
    return {
        "id": credential.id,
        "app_id": credential.app_id,
        "credential_type": credential.credential_type,
        "key_prefix": credential.key_prefix,
        "status": credential.status,
        "expires_at": credential.expires_at.isoformat() if credential.expires_at else None,
        "last_used_at": credential.last_used_at.isoformat() if credential.last_used_at else None,
        "created_at": credential.created_at.isoformat() if credential.created_at else None,
    }


def _find_capability(db: Session, code: str) -> Capability:
    capability = (
        db.query(Capability)
        .filter(Capability.code == code, Capability.deleted_at.is_(None))
        .order_by(Capability.tenant_id.asc())
        .first()
    )
    if capability is None:
        raise ApiError("CAPABILITY_NOT_FOUND", "能力不存在", 404, {"code": code})
    return capability


def _parse_expires(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError as exc:
        raise ApiError("INVALID_ARGUMENT", "expires_at 必须是 ISO 8601 时间", 400) from exc


@admin_router.post("/tenants")
def create_tenant(
    payload: TenantCreate,
    request: Request,
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    existing = db.query(Tenant).filter(Tenant.code == payload.code).first()
    if existing:
        raise ApiError("RESOURCE_CONFLICT", "租户编码已存在", 409)
    tenant = Tenant(
        id=new_id(),
        code=payload.code,
        name=payload.name,
        type=payload.type,
        contact=payload.contact,
        extra=payload.metadata,
    )
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    audit(db, principal, "tenant.create", "tenant", tenant.id, after=_tenant_dict(tenant))
    return _response(request, _tenant_dict(tenant), 201)


@admin_router.get("/tenants")
def list_tenants(
    request: Request,
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    tenants = db.query(Tenant).filter(Tenant.deleted_at.is_(None)).order_by(Tenant.created_at).all()
    return _response(
        request,
        {"items": [_tenant_dict(tenant) for tenant in tenants], "total": len(tenants)},
    )


@admin_router.post("/apps")
def create_app(
    payload: AppCreate,
    request: Request,
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    tenant = db.get(Tenant, payload.tenant_id)
    if tenant is None:
        raise ApiError("RESOURCE_NOT_FOUND", "租户不存在", 404)
    existing = (
        db.query(App).filter(App.tenant_id == payload.tenant_id, App.code == payload.code).first()
    )
    if existing:
        raise ApiError("RESOURCE_CONFLICT", "应用编码已存在", 409)
    app = App(
        id=new_id(),
        tenant_id=payload.tenant_id,
        code=payload.code,
        name=payload.name,
        type=payload.type,
        description=payload.description,
        permissions=sorted(set(payload.permissions)),
        extra=payload.metadata,
    )
    db.add(app)
    db.commit()
    db.refresh(app)
    audit(db, principal, "app.create", "app", app.id, after=_app_dict(app))
    return _response(request, _app_dict(app), 201)


@admin_router.get("/apps")
def list_apps(
    request: Request,
    tenant_id: int | None = Query(default=None),
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    query = db.query(App).filter(App.deleted_at.is_(None))
    if tenant_id:
        query = query.filter(App.tenant_id == tenant_id)
    apps = query.order_by(App.created_at).all()
    return _response(request, {"items": [_app_dict(app) for app in apps], "total": len(apps)})


@admin_router.put("/apps/{app_id}/permissions")
def update_app_permissions(
    app_id: int,
    payload: AppPermissionUpdate,
    request: Request,
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    app = db.get(App, app_id)
    if app is None or app.deleted_at is not None:
        raise ApiError("RESOURCE_NOT_FOUND", "应用不存在", 404)
    permissions = sorted(set(payload.permissions))
    if "*" in permissions:
        raise ApiError("INVALID_ARGUMENT", "应用权限不允许使用通配符", 400)
    before = list(app.permissions or [])
    app.permissions = permissions
    db.commit()
    db.refresh(app)
    audit(
        db,
        principal,
        "app.permissions.update",
        "app",
        app.id,
        before={"permissions": before},
        after={"permissions": permissions},
    )
    return _response(request, _app_dict(app))


@admin_router.post("/apps/{app_id}/credentials")
def create_credential(
    app_id: int,
    request: Request,
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    app = db.get(App, app_id)
    if app is None or app.deleted_at is not None:
        raise ApiError("RESOURCE_NOT_FOUND", "应用不存在", 404)
    plaintext, digest, prefix = generate_api_key()
    credential = AppCredential(
        id=new_id(),
        app_id=app.id,
        credential_type="API_KEY",
        key_hash=digest,
        key_prefix=prefix,
        hash_algo=split_stored_digest(digest)[0],
        created_by=principal.app_id or 0,
    )
    db.add(credential)
    db.commit()
    db.refresh(credential)
    audit(
        db,
        principal,
        "credential.create",
        "app_credential",
        credential.id,
        after={"app_id": app.id, "key_prefix": prefix},
    )
    data = _credential_dict(credential)
    data["api_key"] = plaintext
    return _response(request, data, 201)


@admin_router.get("/apps/{app_id}/credentials")
def list_credentials(
    app_id: int,
    request: Request,
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    credentials = (
        db.query(AppCredential)
        .filter(AppCredential.app_id == app_id)
        .order_by(AppCredential.created_at.desc())
        .all()
    )
    return _response(
        request,
        {"items": [_credential_dict(item) for item in credentials], "total": len(credentials)},
    )


@admin_router.post("/credentials/{credential_id}/rotate")
def rotate_credential(
    credential_id: int,
    request: Request,
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    old = db.get(AppCredential, credential_id)
    if old is None or old.status != "ACTIVE":
        raise ApiError("RESOURCE_NOT_FOUND", "可用凭证不存在", 404)
    app = db.get(App, old.app_id)
    if app is None or app.deleted_at is not None:
        raise ApiError("RESOURCE_NOT_FOUND", "应用不存在", 404)
    plaintext, digest, prefix = generate_api_key()
    credential = AppCredential(
        id=new_id(),
        app_id=app.id,
        credential_type="API_KEY",
        key_hash=digest,
        key_prefix=prefix,
        hash_algo=split_stored_digest(digest)[0],
        created_by=principal.app_id or 0,
    )
    old.status = "REVOKED"
    db.add(credential)
    db.commit()
    db.refresh(credential)
    audit(
        db,
        principal,
        "credential.rotate",
        "app_credential",
        credential.id,
        before={"credential_id": old.id},
        after={"app_id": app.id, "key_prefix": prefix},
    )
    data = _credential_dict(credential)
    data["api_key"] = plaintext
    return _response(request, data, 201)


@admin_router.delete("/credentials/{credential_id}")
def revoke_credential(
    credential_id: int,
    request: Request,
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    credential = db.get(AppCredential, credential_id)
    if credential is None:
        raise ApiError("RESOURCE_NOT_FOUND", "凭证不存在", 404)
    credential.status = "REVOKED"
    db.commit()
    audit(
        db,
        principal,
        "credential.revoke",
        "app_credential",
        credential.id,
        after={"app_id": credential.app_id},
    )
    return _response(request, {"id": credential.id, "status": credential.status})


@admin_router.get("/credentials/hash-stats")
def credential_hash_stats(
    request: Request,
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """按哈希算法分组的凭证计数，供运维判断升格进度。

    只返回计数，不返回任何 `key_hash` 原值。
    """
    rows = (
        db.query(AppCredential.hash_algo, func.count(AppCredential.id))
        .group_by(AppCredential.hash_algo)
        .all()
    )
    by_algo: dict[str, int] = {}
    total = 0
    for algo, count in rows:
        label = algo or "sha256"
        by_algo[label] = by_algo.get(label, 0) + int(count)
        total += int(count)
    audit(db, principal, "credential.hash_stats.read", "app_credential", 0, after={"total": total})
    return _response(request, {"by_algo": by_algo, "total": total})


@admin_router.post("/capabilities")
def register_capability(
    payload: CapabilityCreate,
    request: Request,
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    capability, version = create_capability(db, payload, principal.app_id or 0)
    audit(
        db,
        principal,
        "capability.create",
        "capability",
        capability.id,
        after=capability_to_dict(capability, [version]),
    )
    return _response(
        request,
        {
            "capability": capability_to_dict(capability, [version]),
            "version": version_to_dict(version),
        },
        201,
    )


@admin_router.get("/capabilities")
def list_capabilities(
    request: Request,
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    capabilities = (
        db.query(Capability)
        .filter(Capability.deleted_at.is_(None))
        .order_by(Capability.created_at)
        .all()
    )
    items = []
    for capability in capabilities:
        versions = (
            db.query(CapabilityVersion)
            .filter(CapabilityVersion.capability_id == capability.id)
            .order_by(CapabilityVersion.created_at)
            .all()
        )
        items.append(capability_to_dict(capability, versions))
    return _response(request, {"items": items, "total": len(items)})


@admin_router.get("/capabilities/{code}")
def get_capability(
    code: str,
    request: Request,
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    capability = _find_capability(db, code)
    versions = (
        db.query(CapabilityVersion)
        .filter(CapabilityVersion.capability_id == capability.id)
        .order_by(CapabilityVersion.created_at)
        .all()
    )
    return _response(request, capability_to_dict(capability, versions))


@admin_router.post("/capabilities/{code}/versions")
def add_capability_version(
    code: str,
    payload: CapabilityVersionCreate,
    request: Request,
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    capability = _find_capability(db, code)
    version = create_version(db, capability, payload, principal.app_id or 0)
    audit(
        db,
        principal,
        "capability.version.create",
        "capability_version",
        version.id,
        after=version_to_dict(version),
    )
    return _response(request, version_to_dict(version), 201)


@admin_router.post("/capabilities/{code}/versions/{version}/publish")
def publish_capability_version(
    code: str,
    version: str,
    request: Request,
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    capability = _find_capability(db, code)
    published = publish_version(db, capability, version)
    audit(
        db,
        principal,
        "capability.version.publish",
        "capability_version",
        published.id,
        after=version_to_dict(published),
    )
    return _response(request, version_to_dict(published))


@admin_router.post("/capabilities/{code}/versions/{version}/rollback")
def rollback_capability_version(
    code: str,
    version: str,
    request: Request,
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    capability = _find_capability(db, code)
    target = rollback_version(db, capability, version)
    audit(
        db,
        principal,
        "capability.version.rollback",
        "capability_version",
        target.id,
        after={"version": version},
    )
    return _response(request, version_to_dict(target))


@admin_router.post("/capabilities/{code}/grants")
def grant_capability(
    code: str,
    payload: GrantCreate,
    request: Request,
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    capability = _find_capability(db, code)
    app = db.get(App, payload.app_id)
    if app is None or app.deleted_at is not None:
        raise ApiError("RESOURCE_NOT_FOUND", "应用不存在", 404)

    version_id = None
    if payload.version:
        version = (
            db.query(CapabilityVersion)
            .filter(
                CapabilityVersion.capability_id == capability.id,
                CapabilityVersion.version == payload.version,
            )
            .first()
        )
        if version is None:
            raise ApiError("VERSION_NOT_FOUND", "能力版本不存在", 404)
        version_id = version.id

    existing = (
        db.query(CapabilityGrant)
        .filter(
            CapabilityGrant.app_id == app.id,
            CapabilityGrant.capability_id == capability.id,
            CapabilityGrant.capability_version_id == version_id,
            CapabilityGrant.env == payload.env,
        )
        .first()
    )
    if existing:
        return _response(
            request,
            {
                "id": existing.id,
                "app_id": existing.app_id,
                "capability_id": existing.capability_id,
                "capability_version_id": existing.capability_version_id,
                "status": existing.status,
            },
        )

    grant = CapabilityGrant(
        id=new_id(),
        tenant_id=app.tenant_id,
        app_id=app.id,
        capability_id=capability.id,
        capability_version_id=version_id,
        env=payload.env,
        quota=payload.quota,
        data_scope=payload.data_scope,
        expires_at=_parse_expires(payload.expires_at),
        created_by=principal.app_id or 0,
    )
    db.add(grant)
    db.commit()
    db.refresh(grant)
    audit(
        db,
        principal,
        "capability.grant",
        "capability_grant",
        grant.id,
        after={"app_id": app.id, "capability": code, "version": payload.version},
    )
    return _response(
        request,
        {
            "id": grant.id,
            "app_id": grant.app_id,
            "capability_id": grant.capability_id,
            "capability_version_id": grant.capability_version_id,
            "status": grant.status,
        },
        201,
    )


@admin_router.get("/capabilities/{code}/grants")
def list_grants(
    code: str,
    request: Request,
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    capability = _find_capability(db, code)
    grants = (
        db.query(CapabilityGrant)
        .filter(CapabilityGrant.capability_id == capability.id)
        .order_by(CapabilityGrant.created_at.desc())
        .all()
    )
    app_ids = {grant.app_id for grant in grants}
    apps = {
        app.id: app for app in db.query(App).filter(App.id.in_(app_ids)).all()
    } if app_ids else {}
    version_ids = {
        grant.capability_version_id for grant in grants if grant.capability_version_id
    }
    versions = {
        version.id: version
        for version in db.query(CapabilityVersion)
        .filter(CapabilityVersion.id.in_(version_ids))
        .all()
    } if version_ids else {}
    items = [
        {
            "id": grant.id,
            "app_id": grant.app_id,
            "app_code": apps.get(grant.app_id).code if apps.get(grant.app_id) else None,
            "app_name": apps.get(grant.app_id).name if apps.get(grant.app_id) else None,
            "capability_version_id": grant.capability_version_id,
            "version": (
                versions.get(grant.capability_version_id).version
                if grant.capability_version_id
                and versions.get(grant.capability_version_id)
                else None
            ),
            "env": grant.env,
            "quota": grant.quota or {},
            "data_scope": grant.data_scope or {},
            "status": grant.status,
            "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
            "created_at": grant.created_at.isoformat() if grant.created_at else None,
        }
        for grant in grants
    ]
    return _response(request, {"items": items, "total": len(items)})


@admin_router.delete("/capabilities/{code}/grants/{grant_id}")
def revoke_grant(
    code: str,
    grant_id: int,
    request: Request,
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    capability = _find_capability(db, code)
    grant = db.get(CapabilityGrant, grant_id)
    if grant is None or grant.capability_id != capability.id:
        raise ApiError("RESOURCE_NOT_FOUND", "授权不存在", 404)
    grant.status = "REVOKED"
    db.commit()
    audit(db, principal, "capability.grant.revoke", "capability_grant", grant_id)
    return _response(request, {"id": grant_id, "status": "REVOKED"})


@admin_router.post("/token-revocations")
def create_token_revocation(
    payload: TokenRevocationCreate,
    request: Request,
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    record = revoke(
        db,
        jti=payload.jti,
        subject=payload.subject,
        reason=payload.reason,
        revoked_before=payload.revoked_before,
        expires_in_seconds=payload.expires_in_seconds,
        created_by=principal.username or principal.app_code or "admin",
    )
    audit(
        db,
        principal,
        "token.revoke",
        "token_revocation",
        record.id,
        after=revocation_to_dict(record),
    )
    return _response(request, revocation_to_dict(record), 201)


@admin_router.get("/token-revocations")
def list_token_revocations(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    records = list_revocations(db, limit=limit)
    return _response(
        request,
        {"items": [revocation_to_dict(item) for item in records], "total": len(records)},
    )


@admin_router.delete("/token-revocations/{revocation_id}")
def delete_token_revocation(
    revocation_id: int,
    request: Request,
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    delete_revocation(db, revocation_id)
    audit(db, principal, "token.revocation.delete", "token_revocation", revocation_id)
    return _response(request, {"id": revocation_id, "status": "DELETED"})


@discovery_router.get("/capabilities")
def discover_capabilities(
    request: Request,
    type: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
):
    query = db.query(Capability).filter(
        Capability.status == "PUBLISHED", Capability.deleted_at.is_(None)
    )
    if type:
        query = query.filter(Capability.type == type)
    capabilities = query.order_by(Capability.code).all()

    items = []
    for capability in capabilities:
        if principal.kind == "app":
            grant = (
                db.query(CapabilityGrant)
                .filter(
                    CapabilityGrant.app_id == principal.app_id,
                    CapabilityGrant.capability_id == capability.id,
                    CapabilityGrant.status == "ACTIVE",
                    or_(
                        CapabilityGrant.expires_at.is_(None),
                        CapabilityGrant.expires_at > utcnow(),
                    ),
                )
                .first()
            )
            if grant is None:
                continue
        if tag and tag not in (capability.tags or []):
            continue
        versions = (
            db.query(CapabilityVersion)
            .filter(
                CapabilityVersion.capability_id == capability.id,
                CapabilityVersion.status == "PUBLISHED",
            )
            .order_by(CapabilityVersion.created_at)
            .all()
        )
        default = next((item for item in versions if item.is_default), None)
        items.append(
            {
                "code": capability.code,
                "name": capability.name,
                "type": capability.type,
                "description": capability.description,
                "protocol": default.protocol if default else None,
                "default_version": default.version if default else None,
                "versions": [item.version for item in versions],
                "tags": capability.tags or [],
                "schema_url": f"/api/v1/capabilities/{capability.code}/schema",
            }
        )
    return _response(request, {"items": items, "total": len(items)})


@discovery_router.get("/capabilities/{code}/schema")
def get_capability_schema(
    code: str,
    request: Request,
    version: str | None = Query(default=None),
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
):
    capability, capability_version = resolve_capability(db, code, version, principal.tenant_id)
    if principal.kind == "app" and not check_grant(
        db, principal.app_id or 0, capability.id, capability_version.id
    ):
        raise ApiError("FORBIDDEN", "无能力授权", 403)
    return _response(
        request,
        {
            "code": capability.code,
            "version": capability_version.version,
            "request_schema": capability_version.request_schema,
            "response_schema": capability_version.response_schema,
        },
    )
