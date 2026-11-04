from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import ApiError
from ..schemas import ContractReportRequest, ContractReviewRequest
from ..security import Principal, require_any
from ..services.contract_report import generate_report
from ..services.contract_review import get_risk_rules, review_contract
from ..services.observability import record_event
from .retrieval import _kb_for
from .utils import ok

router = APIRouter(tags=["contract"])


@router.post("/api/v1/contract/review")
def review(
    payload: ContractReviewRequest,
    request: Request,
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
):
    scoped = principal.kind in {"app", "user"} and not principal.is_admin
    tenant_id = principal.tenant_id if scoped else principal.tenant_id or 0
    if not tenant_id:
        raise ApiError("INVALID_ARGUMENT", "请指定租户上下文", 400)
    for kb_id in set(payload.kb_ids):
        _kb_for(db, principal, kb_id)
    result = review_contract(
        db,
        tenant_id=tenant_id,
        text=payload.text,
        kb_ids=payload.kb_ids,
        top_k=payload.top_k,
        principal=principal,
    )
    return ok(request, result)


@router.get("/api/v1/contract/rules")
def rules(request: Request):
    return ok(request, {"items": get_risk_rules(), "total": len(get_risk_rules())})


@router.post("/api/v1/contract/report")
def report(
    payload: ContractReportRequest,
    request: Request,
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
):
    scoped = principal.kind in {"app", "user"} and not principal.is_admin
    tenant_id = principal.tenant_id if scoped else principal.tenant_id or 0
    if not tenant_id:
        raise ApiError("INVALID_ARGUMENT", "请指定租户上下文", 400)
    for kb_id in set(payload.kb_ids):
        _kb_for(db, principal, kb_id)
    review = review_contract(
        db,
        tenant_id=tenant_id,
        text=payload.text,
        kb_ids=payload.kb_ids,
        top_k=payload.top_k,
        principal=principal,
    )
    result = generate_report(review, title=payload.title, fmt=payload.format)
    record_event(
        db,
        "CONTRACT_REPORT_GENERATED",
        tenant_id=tenant_id,
        app_id=principal.app_id,
        request_id=None,
        trace_id=None,
        payload={"format": result["format"], "overall_risk": result["overall_risk"],
                 "findings": result["finding_count"]},
        object_ref="contract_report",
    )
    return ok(request, result)
