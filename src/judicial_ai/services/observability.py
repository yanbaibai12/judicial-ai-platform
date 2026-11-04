from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from ..config import get_settings
from ..context import current_request_context
from ..ids import new_id
from ..models import AiEvent, AuditLog, Capability, CapabilityVersion, UsageRecord
from ..security import Principal
from .governance_pipeline import sanitize_payload


def record_usage(
    db: Session,
    principal: Principal,
    capability: Capability,
    version: CapabilityVersion,
    request_id: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    latency_ms: int = 0,
    status: str = "SUCCESS",
) -> UsageRecord:
    record = UsageRecord(
        id=new_id(),
        tenant_id=principal.tenant_id or 0,
        app_id=principal.app_id or 0,
        capability_id=capability.id,
        capability_version_id=version.id,
        request_id=request_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        request_count=1,
        latency_ms=latency_ms,
        status=status,
    )
    db.add(record)
    db.commit()
    return record


def record_event(
    db: Session,
    event_type: str,
    tenant_id: int,
    app_id: int | None,
    request_id: str | None,
    trace_id: str | None,
    payload: dict[str, Any] | None = None,
    *,
    event_id: str | None = None,
    object_ref: str | None = None,
    status: str = "NEW",
    security_level: str = "INTERNAL",
) -> AiEvent:
    if event_id:
        existing = db.query(AiEvent).filter(AiEvent.event_id == event_id).first()
        if existing is not None:
            return existing

    settings = get_settings()
    request_context = current_request_context()
    request_id = request_id or request_context.request_id or None
    trace_id = trace_id or request_context.trace_id or None
    safe_payload: dict[str, Any] = dict(payload or {})
    safe_payload.setdefault("security_level", security_level)
    if settings.governance_store_event_content:
        sanitized, counts = sanitize_payload(
            safe_payload,
            redact=settings.governance_redaction_enabled,
            max_chars=settings.governance_event_max_chars,
        )
        safe_payload = sanitized
        if counts:
            safe_payload["redaction"] = counts

    event = AiEvent(
        id=new_id(),
        event_id=event_id or f"evt-{uuid.uuid4().hex}",
        event_type=event_type,
        tenant_id=tenant_id,
        app_id=app_id,
        request_id=request_id,
        trace_id=trace_id,
        payload=safe_payload,
        object_ref=object_ref,
        status=status,
    )
    db.add(event)
    db.commit()
    return event


def audit(
    db: Session,
    principal: Principal,
    action: str,
    resource_type: str,
    resource_id: str | int | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    *,
    result: str = "SUCCESS",
    tenant_id: int | None = None,
) -> AuditLog:
    request_context = current_request_context()
    record = AuditLog(
        id=new_id(),
        tenant_id=tenant_id if tenant_id is not None else principal.tenant_id or 0,
        operator_id=principal.app_id or 0,
        operator_type="APP" if principal.kind == "app" else "USER",
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        before=before,
        after=after,
        request_id=request_context.request_id or None,
        trace_id=request_context.trace_id or None,
        ip=request_context.client_ip or None,
        user_agent=request_context.user_agent or None,
        result=result,
    )
    db.add(record)
    db.commit()
    return record
