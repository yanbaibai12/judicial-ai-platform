from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..acl import accessible_document_ids, assert_access
from ..config import get_settings
from ..context import current_request_context
from ..db import SessionLocal
from ..errors import ApiError
from ..metrics import record_job_duration, record_job_event
from ..models import App, AppCredential, EvalTask, Job, KnowledgeBase, Tenant, utcnow
from ..security import Principal

logger = logging.getLogger("judicial_ai.jobs")

JobHandler = Callable[[Session, Job], dict[str, Any]]


def _kb_index_handler(db: Session, job: Job) -> dict[str, Any]:
    from ..models import KnowledgeBase
    from .retrieval import index_kb, index_to_dict

    kb_id = int(job.payload.get("kb_id") or 0)
    kb = db.get(KnowledgeBase, kb_id)
    if kb is None or kb.deleted_at is not None:
        raise ValueError(f"知识库不存在: {kb_id}")
    index = index_kb(db, kb, job.payload.get("version"))
    return {"index": index_to_dict(index)}


def _eval_handler(
    db: Session, job: Job, principal_override: Principal | None = None
) -> dict[str, Any]:
    from ..models import Dataset, DatasetVersion, EvalTask
    from ..security import Principal
    from .governance import _run_eval_task
    from .observability import record_event

    eval_task_id = int(job.payload.get("eval_task_id") or 0)
    task = db.get(EvalTask, eval_task_id)
    if task is None or task.tenant_id != job.tenant_id:
        raise ValueError(f"评测任务不存在: {eval_task_id}")
    version = db.get(DatasetVersion, task.dataset_version_id)
    if version is None:
        raise ValueError(f"数据集版本不存在: {task.dataset_version_id}")
    dataset = db.get(Dataset, version.dataset_id)
    if dataset is None or dataset.tenant_id != job.tenant_id:
        raise ValueError(f"数据集不存在: {version.dataset_id}")
    actor = (job.payload or {}).get("principal")
    if principal_override is not None:
        principal = principal_override
    elif not isinstance(actor, dict):
        if get_settings().resource_acl_enforced:
            raise ValueError("评测任务缺少授权主体，请重新创建任务")
        principal = Principal(kind="app", tenant_id=task.tenant_id, app_id=job.app_id)
    else:
        principal = Principal(
            kind=actor["kind"],
            tenant_id=actor.get("tenant_id"),
            app_id=actor.get("app_id"),
            app_code=actor.get("app_code"),
            user_id=actor.get("user_id"),
            username=actor.get("username"),
            roles=tuple(actor.get("roles") or ()),
        )
    results = _run_eval_task(db, task, dataset, version, principal)
    db.commit()
    if task.status != "SUCCEEDED":
        raise ApiError("EVAL_FAILED", "评测失败，请查看评测任务", 422)
    record_event(
        db,
        "EVAL_COMPLETED",
        tenant_id=task.tenant_id,
        app_id=job.app_id,
        request_id=job.request_id,
        trace_id=job.trace_id,
        payload={
            "eval_task_id": task.id,
            "dataset_version_id": version.id,
            "status": task.status,
            "metrics": task.metrics or {},
        },
    )
    return {
        "eval_task_id": task.id,
        "status": task.status,
        "metrics": task.metrics or {},
        "result_count": len(results),
    }


def _worker_principal(db: Session, job: Job) -> Principal:
    """Revalidate queued app jobs without storing the API key."""
    if job.creator_kind != "app" or not job.creator_id or not job.creator_credential_id:
        raise ApiError(
            "AUTH_REVALIDATION_REQUIRED",
            "任务身份无法重新验证，请使用有效应用凭证重新提交",
            403,
        )
    try:
        app_id = int(job.creator_id)
    except (TypeError, ValueError) as exc:
        raise ApiError("AUTH_INVALID", "任务身份无效", 403) from exc
    app = db.get(App, app_id)
    tenant = db.get(Tenant, job.tenant_id)
    credential = db.get(AppCredential, job.creator_credential_id)
    if (
        app is None or app.id != job.app_id or app.tenant_id != job.tenant_id
        or app.status != "ACTIVE" or app.deleted_at is not None
        or tenant is None or tenant.status != "ACTIVE" or tenant.deleted_at is not None
        or credential is None or credential.app_id != app_id or credential.status != "ACTIVE"
        or (credential.expires_at is not None and credential.expires_at <= utcnow())
    ):
        raise ApiError("AUTH_INVALID", "任务应用、租户或凭证已失效", 403)
    return Principal(
        kind="app",
        tenant_id=job.tenant_id,
        app_id=app_id,
        app_code=app.code,
        credential_id=credential.id,
    )


def _authorize_index_job(db: Session, job: Job, principal: Principal) -> None:
    payload: dict[str, Any] = job.payload or {}
    try:
        kb_id = int(payload["kb_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiError("INVALID_ARGUMENT", "索引任务缺少有效知识库", 400) from exc
    kb = db.get(KnowledgeBase, kb_id)
    if kb is None or kb.deleted_at is not None or kb.tenant_id != job.tenant_id:
        raise ApiError("FORBIDDEN", "任务知识库不可用", 403)
    assert_access(db, principal, "kb", kb_id, "write")
    document_id = payload.get("document_id")
    if document_id is not None:
        try:
            document_id = int(document_id)
        except (TypeError, ValueError) as exc:
            raise ApiError("INVALID_ARGUMENT", "索引任务文档无效", 400) from exc
        if document_id not in accessible_document_ids(db, principal, job.tenant_id, [kb_id]):
            raise ApiError("FORBIDDEN", "任务文档不可访问", 403)


def _event_process_handler(db: Session, job: Job) -> dict[str, Any]:
    from ..security import Principal
    from .governance import process_pending_events

    principal = Principal(kind="admin", app_code="admin")
    return process_pending_events(
        db,
        principal,
        tenant_id=job.payload.get("tenant_id"),
        limit=int(job.payload.get("limit") or 100),
    )


def _retry_handler(db: Session, job: Job) -> dict[str, Any]:
    return {"retried": True}


JOB_HANDLERS: dict[str, JobHandler] = {
    "kb_index": _kb_index_handler,
    "eval": _eval_handler,
    "event_process": _event_process_handler,
    "retry": _retry_handler,
}


def new_job_id() -> str:
    return f"job-{uuid.uuid4().hex}"


def job_to_dict(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "job_type": job.job_type,
        "status": job.status,
        "tenant_id": job.tenant_id,
        "app_id": job.app_id,
        "request_id": job.request_id,
        "trace_id": job.trace_id,
        "payload": job.payload or {},
        "result": job.result or {},
        "error": job.error,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "status_url": f"/api/v1/jobs/{job.id}",
        "scheduled_at": job.scheduled_at.isoformat() if job.scheduled_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "heartbeat_at": job.heartbeat_at.isoformat() if job.heartbeat_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }


def enqueue_job(
    db: Session,
    job_type: str,
    payload: dict[str, Any],
    *,
    tenant_id: int,
    app_id: int | None = None,
    request_id: str | None = None,
    trace_id: str | None = None,
    idempotency_key: str | None = None,
    max_attempts: int | None = None,
    priority: int = 100,
    principal: Principal | None = None,
) -> Job:
    settings = get_settings()
    creator_kind = principal.kind if principal else ("app" if app_id is not None else "system")
    creator_id = None
    if principal and principal.kind == "app" and principal.app_id is not None:
        creator_id = str(principal.app_id)
    elif principal and principal.kind == "user":
        creator_id = principal.user_id
    elif principal is None and app_id is not None:
        creator_id = str(app_id)
    if principal and principal.kind in {"app", "user"} and not creator_id:
        raise ApiError("AUTH_INVALID", "任务创建主体缺少稳定身份", 401)
    scoped_key = None
    if idempotency_key:
        key_material = json.dumps(
            [tenant_id, creator_kind, creator_id, job_type, idempotency_key],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        scoped_key = "v2:" + hashlib.sha256(key_material.encode("utf-8")).hexdigest()
        existing = db.query(Job).filter(Job.idempotency_key == scoped_key).first()
        if existing is not None:
            if existing.payload != payload:
                raise ApiError("RESOURCE_CONFLICT", "幂等键已用于不同的任务请求", 409)
            return existing
    context = current_request_context()
    job = Job(
        id=new_job_id(),
        job_type=job_type,
        status="PENDING",
        tenant_id=tenant_id,
        app_id=app_id,
        creator_kind=creator_kind,
        creator_id=creator_id,
        creator_credential_id=principal.credential_id if principal else None,
        request_id=request_id or context.request_id or None,
        trace_id=trace_id or context.trace_id or None,
        idempotency_key=scoped_key,
        payload=payload,
        max_attempts=max_attempts or settings.job_max_attempts,
        priority=priority,
        scheduled_at=utcnow(),
    )
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = (
            db.query(Job).filter(Job.idempotency_key == scoped_key).first()
            if scoped_key else None
        )
        if existing is None:
            raise
        if existing.payload != payload:
            raise ApiError("RESOURCE_CONFLICT", "幂等键已用于不同的任务请求", 409) from None
        return existing
    db.refresh(job)
    record_job_event(job.job_type, job.status)
    if settings.job_mode == "inline":
        return execute_job(db, job)
    logger.info(
        "job_enqueued",
        extra={"job_id": job.id, "job_type": job.job_type, "status": job.status},
    )
    return job


def execute_job(db: Session, job: Job) -> Job:
    if job.status != "PENDING":
        return job
    job.status = "RUNNING"
    job.attempts += 1
    job.started_at = utcnow()
    job.heartbeat_at = job.started_at
    db.commit()
    return _execute_running(db, job)


def _execute_running(db: Session, job: Job, *, from_worker: bool = False) -> Job:
    started = time.perf_counter()
    handler = JOB_HANDLERS.get(job.job_type)
    attempt = job.attempts
    job_id = job.id
    stop = threading.Event()
    settings = get_settings()
    interval = max(0.1, min(settings.worker_heartbeat_seconds, settings.worker_stale_seconds / 3))

    def heartbeat() -> None:
        while not stop.wait(interval):
            with SessionLocal() as heartbeat_db:
                try:
                    changed = (
                        heartbeat_db.query(Job)
                        .filter(Job.id == job_id, Job.status == "RUNNING", Job.attempts == attempt)
                        .update({"heartbeat_at": utcnow()}, synchronize_session=False)
                    )
                    heartbeat_db.commit()
                    if not changed:
                        return
                except Exception:
                    logger.exception("job_heartbeat_failed", extra={"job_id": job_id})

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        if handler is None:
            raise ValueError(f"未知任务类型: {job.job_type}")
        if job.status == "CANCELLED":
            return job
        worker_principal = None
        if from_worker and job.job_type in {"kb_index", "eval"}:
            worker_principal = _worker_principal(db, job)
            if job.job_type == "kb_index":
                _authorize_index_job(db, job, worker_principal)
        if worker_principal is not None and job.job_type == "eval":
            result = _eval_handler(db, job, principal_override=worker_principal)
        else:
            result = handler(db, job)
        changed = (
            db.query(Job)
            .filter(Job.id == job_id, Job.status == "RUNNING", Job.attempts == attempt)
            .update(
                {"status": "SUCCEEDED", "result": result, "error": None, "finished_at": utcnow()},
                synchronize_session=False,
            )
        )
        db.commit()
        db.refresh(job)
        if not changed:
            return job
        record_job_event(job.job_type, job.status)
        record_job_duration(job.job_type, time.perf_counter() - started)
        logger.info(
            "job_succeeded",
            extra={"job_id": job.id, "job_type": job.job_type, "status": job.status},
        )
    except Exception as exc:  # noqa: BLE001 - failures must be persisted for retry
        db.rollback()
        job = db.get(Job, job.id) or job
        db.refresh(job)
        if job.status != "RUNNING" or job.attempts != attempt:
            return job
        values: dict[str, Any] = {"error": str(exc)[:2000]}
        auth_failure = isinstance(exc, ApiError) and exc.status_code in {401, 403}
        permanent_failure = auth_failure or (
            isinstance(exc, ApiError) and exc.code == "EVAL_FAILED"
        )
        if job.attempts < job.max_attempts and not permanent_failure:
            values.update(
                status="PENDING",
                finished_at=None,
                scheduled_at=utcnow() + timedelta(seconds=min(300, 2**job.attempts * 5)),
            )
        else:
            values.update(status="FAILED", finished_at=utcnow())
        changed = (
            db.query(Job)
            .filter(Job.id == job_id, Job.status == "RUNNING", Job.attempts == attempt)
            .update(values, synchronize_session=False)
        )
        if changed and auth_failure and job.job_type == "eval":
            try:
                eval_task_id = int((job.payload or {}).get("eval_task_id") or 0)
            except (TypeError, ValueError):
                eval_task_id = 0
            db.query(EvalTask).filter(
                EvalTask.id == eval_task_id,
                EvalTask.tenant_id == job.tenant_id,
                EvalTask.status == "PENDING",
            ).update(
                {"status": "FAILED", "error": str(exc)[:1000], "finished_at": utcnow()},
                synchronize_session=False,
            )
        db.commit()
        db.refresh(job)
        if not changed:
            return job
        record_job_event(job.job_type, job.status)
        record_job_duration(job.job_type, time.perf_counter() - started)
        logger.exception(
            "job_failed",
            extra={"job_id": job.id, "job_type": job.job_type, "status": job.status},
        )
    finally:
        stop.set()
        thread.join()
    return job


def claim_next_job(db: Session) -> Job | None:
    settings = get_settings()
    query = (
        db.query(Job)
        .filter(Job.status == "PENDING", Job.scheduled_at <= utcnow())
        .order_by(Job.priority.asc(), Job.created_at.asc())
    )
    if not settings.is_sqlite:
        query = query.with_for_update(skip_locked=True)
    job = query.first()
    if job is None:
        return None
    now = utcnow()
    claimed = (
        db.query(Job)
        .filter(Job.id == job.id, Job.status == "PENDING", Job.scheduled_at <= now)
        .update(
            {
                "status": "RUNNING",
                "attempts": Job.attempts + 1,
                "started_at": now,
                "heartbeat_at": now,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    if not claimed:
        return None
    db.refresh(job)
    record_job_event(job.job_type, job.status)
    return job


def recover_stale_jobs(db: Session, *, stale_after_seconds: int = 900) -> int:
    cutoff = utcnow() - timedelta(seconds=stale_after_seconds)
    jobs = (
        db.query(Job)
        .filter(
            Job.status == "RUNNING",
            Job.started_at.is_not(None),
            func.coalesce(Job.heartbeat_at, Job.started_at) < cutoff,
        )
        .all()
    )
    recovered = 0
    for job in jobs:
        changed = (
            db.query(Job)
            .filter(
                Job.id == job.id,
                Job.status == "RUNNING",
                Job.attempts == job.attempts,
                func.coalesce(Job.heartbeat_at, Job.started_at) < cutoff,
            )
            .update(
                {
                    "status": "FAILED",
                    "finished_at": utcnow(),
                    "error": "worker heartbeat expired; outcome uncertain, review before retry",
                },
                synchronize_session=False,
            )
        )
        if changed:
            recovered += 1
            record_job_event(job.job_type, "FAILED")
    db.commit()
    return recovered


def run_worker_loop(
    *,
    once: bool = False,
    poll_seconds: float | None = None,
    batch_size: int | None = None,
) -> None:
    settings = get_settings()
    poll = poll_seconds or settings.worker_poll_seconds
    batch = batch_size or settings.worker_batch_size
    db = SessionLocal()
    try:
        while True:
            recover_stale_jobs(db, stale_after_seconds=settings.worker_stale_seconds)
            processed = 0
            for _ in range(batch):
                job = claim_next_job(db)
                if job is None:
                    break
                _execute_running(db, job, from_worker=True)
                processed += 1
            if once:
                return
            if not processed:
                time.sleep(poll)
    finally:
        db.close()
