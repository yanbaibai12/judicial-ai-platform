from __future__ import annotations

import re
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..errors import ApiError
from ..ids import new_id
from ..models import (
    Capability,
    CapabilityGrant,
    CapabilityVersion,
    utcnow,
)
from ..schemas import CapabilityCreate, CapabilityVersionCreate

CODE_RE = re.compile(r"^[a-z][a-z0-9_.-]{2,127}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


def validate_code(code: str) -> None:
    if not CODE_RE.match(code):
        raise ApiError(
            "INVALID_ARGUMENT",
            "能力编码不合法",
            400,
            {"code": code, "pattern": CODE_RE.pattern},
        )


def validate_semver(version: str) -> None:
    if not SEMVER_RE.match(version):
        raise ApiError("INVALID_ARGUMENT", "版本号必须为 SemVer", 400, {"version": version})


def validate_json_schema(schema: dict[str, Any], field: str = "schema") -> None:
    if not isinstance(schema, dict):
        raise ApiError("INVALID_ARGUMENT", f"{field} 必须是 JSON 对象", 400)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ApiError(
            "SCHEMA_VALIDATION_FAILED",
            f"{field} 不是合法的 JSON Schema",
            400,
            {"field": field, "reason": exc.message},
        ) from exc


def validate_against_schema(schema: dict[str, Any], payload: Any, field: str = "input") -> None:
    if not schema:
        return
    errors = sorted(Draft202012Validator(schema).iter_errors(payload), key=lambda e: list(e.path))
    if errors:
        first: ValidationError = errors[0]
        raise ApiError(
            "SCHEMA_VALIDATION_FAILED",
            f"{field} 不符合 Schema",
            400,
            {
                "field": field,
                "path": "/".join(str(part) for part in first.path),
                "reason": first.message,
            },
        )


def create_capability(
    db: Session, payload: CapabilityCreate, operator_id: int = 0
) -> tuple[Capability, CapabilityVersion]:
    validate_code(payload.code)
    validate_semver(payload.version)
    validate_json_schema(payload.request_schema, "request_schema")
    validate_json_schema(payload.response_schema, "response_schema")

    tenant_id = payload.tenant_id or 0
    existing = (
        db.query(Capability)
        .filter(
            Capability.tenant_id == tenant_id,
            Capability.code == payload.code,
            Capability.deleted_at.is_(None),
        )
        .first()
    )
    if existing:
        raise ApiError("RESOURCE_CONFLICT", "能力编码已存在", 409, {"code": payload.code})

    capability = Capability(
        id=new_id(),
        tenant_id=tenant_id,
        code=payload.code,
        name=payload.name,
        type=payload.type,
        scope=payload.scope,
        owner=payload.owner,
        description=payload.description,
        status="DRAFT",
        tags=payload.tags,
    )
    db.add(capability)
    db.flush()

    version = CapabilityVersion(
        id=new_id(),
        capability_id=capability.id,
        version=payload.version,
        status="DRAFT",
        protocol=payload.protocol,
        endpoint=payload.endpoint,
        model_deployment_id=payload.model_deployment_id,
        model_name=payload.model_name,
        adapter_alias=payload.adapter_alias,
        request_schema=payload.request_schema,
        response_schema=payload.response_schema,
        default_timeout_ms=payload.default_timeout_ms,
        max_timeout_ms=payload.max_timeout_ms,
        max_input_tokens=payload.max_input_tokens,
        max_output_tokens=payload.max_output_tokens,
        qps_limit=payload.qps_limit,
        concurrency_limit=payload.concurrency_limit,
        retry_policy=payload.retry_policy,
        health_check=payload.health_check,
        security_level=payload.security_level,
        is_default=False,
        created_by=operator_id,
    )
    db.add(version)
    db.commit()
    db.refresh(capability)
    db.refresh(version)
    return capability, version


def create_version(
    db: Session, capability: Capability, payload: CapabilityVersionCreate, operator_id: int = 0
) -> CapabilityVersion:
    validate_semver(payload.version)
    validate_json_schema(payload.request_schema, "request_schema")
    validate_json_schema(payload.response_schema, "response_schema")

    existing = (
        db.query(CapabilityVersion)
        .filter(
            CapabilityVersion.capability_id == capability.id,
            CapabilityVersion.version == payload.version,
        )
        .first()
    )
    if existing:
        raise ApiError("RESOURCE_CONFLICT", "能力版本已存在", 409, {"version": payload.version})

    version = CapabilityVersion(
        id=new_id(),
        capability_id=capability.id,
        version=payload.version,
        status="DRAFT",
        protocol=payload.protocol,
        endpoint=payload.endpoint,
        model_deployment_id=payload.model_deployment_id,
        model_name=payload.model_name,
        adapter_alias=payload.adapter_alias,
        request_schema=payload.request_schema,
        response_schema=payload.response_schema,
        default_timeout_ms=payload.default_timeout_ms,
        max_timeout_ms=payload.max_timeout_ms,
        max_input_tokens=payload.max_input_tokens,
        max_output_tokens=payload.max_output_tokens,
        qps_limit=payload.qps_limit,
        concurrency_limit=payload.concurrency_limit,
        retry_policy=payload.retry_policy,
        health_check=payload.health_check,
        security_level=payload.security_level,
        is_default=False,
        created_by=operator_id,
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    return version


def publish_version(db: Session, capability: Capability, version: str) -> CapabilityVersion:
    target = (
        db.query(CapabilityVersion)
        .filter(
            CapabilityVersion.capability_id == capability.id,
            CapabilityVersion.version == version,
        )
        .first()
    )
    if target is None:
        raise ApiError("VERSION_NOT_FOUND", "能力版本不存在", 404, {"version": version})

    db.query(CapabilityVersion).filter(CapabilityVersion.capability_id == capability.id).update(
        {CapabilityVersion.is_default: False}
    )

    target.status = "PUBLISHED"
    target.is_default = True
    target.published_at = utcnow()
    capability.status = "PUBLISHED"
    db.commit()
    db.refresh(target)
    return target


def rollback_version(db: Session, capability: Capability, version: str) -> CapabilityVersion:
    target = (
        db.query(CapabilityVersion)
        .filter(
            CapabilityVersion.capability_id == capability.id,
            CapabilityVersion.version == version,
        )
        .first()
    )
    if target is None:
        raise ApiError("VERSION_NOT_FOUND", "能力版本不存在", 404, {"version": version})
    if target.status not in {"PUBLISHED", "DEPRECATED"}:
        raise ApiError("JOB_STATE_INVALID", "只能回滚到已发布版本", 409)

    db.query(CapabilityVersion).filter(CapabilityVersion.capability_id == capability.id).update(
        {CapabilityVersion.is_default: False}
    )
    target.is_default = True
    db.commit()
    db.refresh(target)
    return target


def resolve_capability(
    db: Session, code: str, version: str | None = None, tenant_id: int | None = None
) -> tuple[Capability, CapabilityVersion]:
    query = db.query(Capability).filter(Capability.code == code, Capability.deleted_at.is_(None))
    if tenant_id is not None:
        query = query.filter(or_(Capability.tenant_id == 0, Capability.tenant_id == tenant_id))
    capability = query.order_by(Capability.tenant_id.desc()).first()
    if capability is None:
        raise ApiError("CAPABILITY_NOT_FOUND", "能力不存在", 404, {"code": code})

    versions = db.query(CapabilityVersion).filter(
        CapabilityVersion.capability_id == capability.id,
        CapabilityVersion.status == "PUBLISHED",
    )
    if version:
        versions = versions.filter(CapabilityVersion.version == version)
    else:
        versions = versions.filter(CapabilityVersion.is_default.is_(True))

    capability_version = versions.first()
    if capability_version is None:
        raise ApiError(
            "VERSION_NOT_FOUND",
            "能力版本不存在或未发布",
            404,
            {"code": code, "version": version or "default"},
        )
    return capability, capability_version


def find_capability_by_model(
    db: Session, model: str, tenant_id: int | None = None
) -> tuple[Capability, CapabilityVersion]:
    if "@" in model:
        code, version = model.rsplit("@", 1)
        return resolve_capability(db, code, version, tenant_id)

    try:
        return resolve_capability(db, model, None, tenant_id)
    except ApiError as exc:
        if exc.code != "CAPABILITY_NOT_FOUND":
            raise

    query = (
        db.query(CapabilityVersion)
        .join(Capability, CapabilityVersion.capability_id == Capability.id)
        .filter(
            CapabilityVersion.status == "PUBLISHED",
            CapabilityVersion.is_default.is_(True),
            Capability.deleted_at.is_(None),
            or_(
                CapabilityVersion.model_name == model,
                CapabilityVersion.adapter_alias == model,
            ),
        )
    )
    if tenant_id is not None:
        query = query.filter(or_(Capability.tenant_id == 0, Capability.tenant_id == tenant_id))
    version = query.order_by(
        CapabilityVersion.adapter_alias.is_(None).desc(),
        Capability.tenant_id.desc(),
    ).first()
    if version is None:
        raise ApiError("CAPABILITY_NOT_FOUND", "模型未绑定任何已发布能力", 404, {"model": model})
    capability = db.get(Capability, version.capability_id)
    if capability is None:
        raise ApiError("CAPABILITY_NOT_FOUND", "能力不存在", 404, {"model": model})
    return capability, version


def check_grant(
    db: Session,
    app_id: int,
    capability_id: int,
    capability_version_id: int,
    *,
    env: str | None = None,
) -> CapabilityGrant | None:
    query = db.query(CapabilityGrant).filter(
        CapabilityGrant.app_id == app_id,
        CapabilityGrant.capability_id == capability_id,
        CapabilityGrant.status == "ACTIVE",
        or_(
            CapabilityGrant.capability_version_id.is_(None),
            CapabilityGrant.capability_version_id == capability_version_id,
        ),
        or_(CapabilityGrant.expires_at.is_(None), CapabilityGrant.expires_at > utcnow()),
    )
    if env:
        query = query.filter(or_(CapabilityGrant.env == env, CapabilityGrant.env == "*"))
    return query.first()


def version_to_dict(version: CapabilityVersion) -> dict[str, Any]:
    return {
        "id": version.id,
        "capability_id": version.capability_id,
        "version": version.version,
        "status": version.status,
        "protocol": version.protocol,
        "endpoint": version.endpoint,
        "model_name": version.model_name,
        "adapter_alias": version.adapter_alias,
        "request_schema": version.request_schema,
        "response_schema": version.response_schema,
        "default_timeout_ms": version.default_timeout_ms,
        "max_timeout_ms": version.max_timeout_ms,
        "max_input_tokens": version.max_input_tokens,
        "max_output_tokens": version.max_output_tokens,
        "qps_limit": version.qps_limit,
        "concurrency_limit": version.concurrency_limit,
        "security_level": version.security_level,
        "is_default": version.is_default,
        "published_at": version.published_at.isoformat() if version.published_at else None,
        "created_at": version.created_at.isoformat() if version.created_at else None,
    }


def capability_to_dict(
    capability: Capability, versions: list[CapabilityVersion] | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": capability.id,
        "tenant_id": capability.tenant_id,
        "code": capability.code,
        "name": capability.name,
        "type": capability.type,
        "scope": capability.scope,
        "owner": capability.owner,
        "description": capability.description,
        "status": capability.status,
        "tags": capability.tags or [],
        "created_at": capability.created_at.isoformat() if capability.created_at else None,
    }
    if versions is not None:
        payload["versions"] = [version_to_dict(item) for item in versions]
        default = next((item for item in versions if item.is_default), None)
        payload["default_version"] = default.version if default else None
    return payload
