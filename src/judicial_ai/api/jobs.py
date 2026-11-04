from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from ..acl import accessible_document_ids, has_access
from ..db import get_db
from ..errors import ApiError
from ..models import Job, KnowledgeBase, utcnow
from ..security import Principal, require_any, require_permission
from ..services.jobs import job_to_dict
from .utils import ok, tenant_scope

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


@router.get("")
def list_jobs(
    request: Request,
    status: str | None = Query(default=None),
    job_type: str | None = Query(default=None),
    tenant_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
):
    jobs = _list_jobs(
        db,
        principal,
        status=status,
        job_type=job_type,
        tenant_id=tenant_id,
        limit=limit,
    )
    return ok(request, {"items": [job_to_dict(item) for item in jobs], "total": len(jobs)})


@router.get("/{job_id}")
def get_job(
    job_id: str,
    request: Request,
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
):
    job = _job_for(db, principal, job_id)
    return ok(request, job_to_dict(job))


@router.post("/{job_id}/cancel")
def cancel_job(
    job_id: str,
    request: Request,
    principal: Principal = Depends(require_permission("job:write")),
    db: Session = Depends(get_db),
):
    job = _job_for(db, principal, job_id, need="write")
    if job.status != "PENDING":
        raise ApiError("INVALID_STATE", "当前任务不可取消", 409, {"status": job.status})
    changed = (
        db.query(Job)
        .filter(Job.id == job_id, Job.status == "PENDING")
        .update({"status": "CANCELLED", "finished_at": utcnow()}, synchronize_session=False)
    )
    db.commit()
    if not changed:
        raise ApiError("INVALID_STATE", "任务状态已改变，无法取消", 409)
    db.refresh(job)
    return ok(request, job_to_dict(job))


@router.post("/{job_id}/retry")
def retry_job(
    job_id: str,
    request: Request,
    principal: Principal = Depends(require_permission("job:write")),
    db: Session = Depends(get_db),
):
    job = _job_for(db, principal, job_id, need="write")
    if job.status not in {"FAILED", "CANCELLED"}:
        raise ApiError("INVALID_STATE", "仅失败或已取消任务可重试", 409, {"status": job.status})
    job.status = "PENDING"
    job.attempts = 0
    job.error = None
    job.result = None
    job.finished_at = None
    job.started_at = None
    job.heartbeat_at = None
    job.scheduled_at = utcnow()
    db.commit()
    db.refresh(job)
    return ok(request, job_to_dict(job))


def _owned_jobs(query, principal: Principal):
    if principal.is_admin:
        return query
    if principal.kind == "app" and principal.app_id is not None:
        return query.filter(or_(
            and_(Job.creator_kind == "app", Job.creator_id == str(principal.app_id)),
            and_(Job.creator_kind.is_(None), Job.app_id == principal.app_id),
        ))
    if principal.kind == "user" and principal.user_id:
        return query.filter(Job.creator_kind == "user", Job.creator_id == principal.user_id)
    return query.filter(False)


def _resource_accessible(db: Session, principal: Principal, job: Job, need: str) -> bool:
    if principal.is_admin or job.job_type != "kb_index":
        return True
    payload = job.payload or {}
    try:
        kb_id = int(payload["kb_id"])
    except (KeyError, TypeError, ValueError):
        return False
    kb = db.query(KnowledgeBase).filter(
        KnowledgeBase.id == kb_id,
        KnowledgeBase.tenant_id == job.tenant_id,
        KnowledgeBase.deleted_at.is_(None),
    ).first()
    if kb is None or not has_access(db, principal, "kb", kb_id, need):
        return False
    if payload.get("document_id") is not None:
        try:
            document_id = int(payload["document_id"])
        except (TypeError, ValueError):
            return False
        if document_id not in accessible_document_ids(db, principal, job.tenant_id, [kb_id]):
            return False
    return True


def _job_for(db: Session, principal: Principal, job_id: str, need: str = "read") -> Job:
    query = _owned_jobs(db.query(Job).filter(Job.id == job_id), principal)
    scope = tenant_scope(principal)
    if scope is not None:
        query = query.filter(Job.tenant_id == scope)
    job = query.first()
    if job is None or not _resource_accessible(db, principal, job, need):
        raise ApiError("RESOURCE_NOT_FOUND", "任务不存在", 404, {"job_id": job_id})
    return job


def _list_jobs(
    db: Session,
    principal: Principal,
    *,
    status: str | None,
    job_type: str | None,
    tenant_id: int | None,
    limit: int,
) -> list[Job]:
    query = _owned_jobs(db.query(Job), principal)
    scope = tenant_scope(principal, tenant_id)
    if scope is not None:
        query = query.filter(Job.tenant_id == scope)
    if status:
        query = query.filter(Job.status == status.upper())
    if job_type:
        query = query.filter(Job.job_type == job_type)
    jobs = []
    while len(jobs) < limit:
        batch = query.order_by(Job.created_at.desc(), Job.id.desc()).limit(max(100, limit)).all()
        if not batch:
            break
        for job in batch:
            if _resource_accessible(db, principal, job, "read"):
                jobs.append(job)
                if len(jobs) >= limit:
                    break
        last = batch[-1]
        query = query.filter(or_(
            Job.created_at < last.created_at,
            and_(Job.created_at == last.created_at, Job.id < last.id),
        ))
    return jobs
