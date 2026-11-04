from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..errors import ApiError
from ..models import (
    AiEvent,
    AnnotationRecord,
    AnnotationTask,
    BadCase,
    Dataset,
    DatasetItem,
    DatasetLineage,
    DatasetVersion,
    EvalResult,
    EvalTask,
    Feedback,
    RetentionJob,
)
from ..schemas import (
    AnnotationRecordCreate,
    AnnotationTaskCreate,
    BadCaseCreate,
    BadCaseUpdate,
    DatasetCreate,
    DatasetExportRequest,
    DatasetVersionCreate,
    EvalTaskCreate,
    FeedbackCreate,
    RetentionJobCreate,
)
from ..security import Principal, require_admin, require_any, require_app, require_permission
from ..services.governance import (
    annotation_task_stats,
    claim_annotation_task,
    create_bad_case,
    create_dataset_version,
    create_eval_task,
    create_retention_job,
    export_dataset_version,
    get_eval_task,
    governance_metrics,
    list_annotation_tasks,
    list_audit_logs,
    list_dataset_items,
    list_dataset_lineage,
    list_eval_tasks,
    list_events,
    list_retention_jobs,
    list_usage,
    process_pending_events,
    publish_dataset_version,
    replay_event,
    submit_annotation_record,
    submit_feedback,
    update_bad_case,
)
from ..services.governance import (
    create_annotation_task as create_annotation_task_record,
)
from ..services.governance import (
    create_dataset as create_dataset_record,
)
from ..services.jobs import enqueue_job, job_to_dict
from ..services.observability import audit
from .utils import ok, tenant_scope

router = APIRouter(tags=["governance"])


def _feedback_dict(record: Feedback) -> dict[str, Any]:
    return {
        "id": record.id,
        "request_id": record.request_id,
        "rating": record.rating,
        "reason": record.reason,
        "comment": record.comment,
        "tags": record.tags or [],
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


def _bad_case_dict(record: BadCase) -> dict[str, Any]:
    return {
        "id": record.id,
        "request_id": record.request_id,
        "category": record.category,
        "sub_category": record.sub_category,
        "root_cause": record.root_cause,
        "priority": record.priority,
        "status": record.status,
        "owner": record.owner,
        "fix_ref": record.fix_ref,
        "regression_case_id": record.regression_case_id,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "closed_at": record.closed_at.isoformat() if record.closed_at else None,
    }


def _dataset_dict(record: Dataset) -> dict[str, Any]:
    return {
        "id": record.id,
        "tenant_id": record.tenant_id,
        "code": record.code,
        "name": record.name,
        "type": record.type,
        "description": record.description,
        "status": record.status,
        "owner": record.owner,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


def _dataset_version_dict(record: DatasetVersion) -> dict[str, Any]:
    return {
        "id": record.id,
        "dataset_id": record.dataset_id,
        "version": record.version,
        "status": record.status,
        "source_snapshot": record.source_snapshot or {},
        "filter_config": record.filter_config or {},
        "prelabel_model": record.prelabel_model,
        "stats": record.stats or {},
        "approved_by": record.approved_by,
        "published_at": record.published_at.isoformat() if record.published_at else None,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


def _event_dict(record: AiEvent) -> dict[str, Any]:
    return {
        "id": record.id,
        "event_id": record.event_id,
        "event_type": record.event_type,
        "tenant_id": record.tenant_id,
        "app_id": record.app_id,
        "request_id": record.request_id,
        "trace_id": record.trace_id,
        "payload": record.payload or {},
        "object_ref": record.object_ref,
        "status": record.status,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


def _dataset_item_dict(record: DatasetItem) -> dict[str, Any]:
    return {
        "id": record.id,
        "dataset_version_id": record.dataset_version_id,
        "item_type": record.item_type,
        "input": record.input,
        "output": record.output,
        "label": record.label,
        "metadata": record.extra or {},
        "source_event_id": record.source_event_id,
        "quality_score": float(record.quality_score) if record.quality_score else None,
        "review_status": record.review_status,
        "split": record.split,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


def _lineage_dict(record: DatasetLineage) -> dict[str, Any]:
    return {
        "id": record.id,
        "dataset_version_id": record.dataset_version_id,
        "stage": record.stage,
        "input_ref": record.input_ref or {},
        "output_ref": record.output_ref or {},
        "config": record.config or {},
        "stats": record.stats or {},
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


def _annotation_task_dict(record: AnnotationTask) -> dict[str, Any]:
    return {
        "id": record.id,
        "tenant_id": record.tenant_id,
        "dataset_version_id": record.dataset_version_id,
        "name": record.name,
        "task_type": record.task_type,
        "total_items": record.total_items,
        "required_reviewers": record.required_reviewers,
        "status": record.status,
        "assignee": record.assignee,
        "due_at": record.due_at.isoformat() if record.due_at else None,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


def _annotation_record_dict(record: AnnotationRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "task_id": record.task_id,
        "item_id": record.item_id,
        "reviewer_id": record.reviewer_id,
        "label": record.label or {},
        "decision": record.decision,
        "confidence": float(record.confidence) if record.confidence is not None else None,
        "comment": record.comment,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


def _eval_task_dict(record: EvalTask) -> dict[str, Any]:
    return {
        "id": record.id,
        "tenant_id": record.tenant_id,
        "dataset_version_id": record.dataset_version_id,
        "target_type": record.target_type,
        "target_version": record.target_version,
        "config": record.config or {},
        "metrics": record.metrics or {},
        "status": record.status,
        "error": record.error,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "finished_at": record.finished_at.isoformat() if record.finished_at else None,
    }


def _eval_result_dict(record: EvalResult) -> dict[str, Any]:
    return {
        "metric": record.metric,
        "value": float(record.value),
        "detail": record.detail or {},
    }


def _retention_job_dict(record: RetentionJob) -> dict[str, Any]:
    return {
        "id": record.id,
        "tenant_id": record.tenant_id,
        "job_type": record.job_type,
        "target": record.target,
        "policy": record.policy or {},
        "status": record.status,
        "deleted_count": record.deleted_count,
        "detail": record.detail or {},
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "finished_at": record.finished_at.isoformat() if record.finished_at else None,
    }


@router.post("/api/v1/feedback")
def create_feedback(
    payload: FeedbackCreate,
    request: Request,
    principal: Principal = Depends(require_app),
    db: Session = Depends(get_db),
):
    feedback = submit_feedback(db, principal, payload)
    return ok(request, _feedback_dict(feedback), 201)


@router.get("/api/v1/feedback")
def list_feedback(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
):
    query = db.query(Feedback)
    if (principal.kind in {"app", "user"} and not principal.is_admin):
        query = query.filter(Feedback.tenant_id == (principal.tenant_id or 0))
    items = query.order_by(Feedback.created_at.desc()).limit(limit).all()
    return ok(request, {"items": [_feedback_dict(item) for item in items], "total": len(items)})


@router.post("/api/v1/bad-cases")
def report_bad_case(
    payload: BadCaseCreate,
    request: Request,
    principal: Principal = Depends(require_app),
    db: Session = Depends(get_db),
):
    record = create_bad_case(db, principal, payload)
    return ok(request, _bad_case_dict(record), 201)


@router.patch("/api/v1/bad-cases/{case_id}")
def patch_bad_case(
    case_id: int,
    payload: BadCaseUpdate,
    request: Request,
    principal: Principal = Depends(require_app),
    db: Session = Depends(get_db),
):
    record = update_bad_case(db, principal, case_id, payload)
    return ok(request, _bad_case_dict(record))


@router.get("/api/v1/bad-cases")
def list_bad_cases(
    request: Request,
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
):
    query = db.query(BadCase)
    if (principal.kind in {"app", "user"} and not principal.is_admin):
        query = query.filter(BadCase.tenant_id == (principal.tenant_id or 0))
    if status:
        query = query.filter(BadCase.status == status)
    items = query.order_by(BadCase.created_at.desc()).limit(limit).all()
    return ok(request, {"items": [_bad_case_dict(item) for item in items], "total": len(items)})


@router.post("/api/v1/datasets")
def create_dataset(
    payload: DatasetCreate,
    request: Request,
    principal: Principal = Depends(require_permission("governance:write")),
    db: Session = Depends(get_db),
):
    dataset = create_dataset_record(db, principal, payload)
    return ok(request, _dataset_dict(dataset), 201)


@router.get("/api/v1/datasets")
def list_datasets(
    request: Request,
    tenant_id: int | None = Query(default=None),
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
):
    query = db.query(Dataset)
    scope = tenant_scope(principal, tenant_id)
    if scope is not None:
        query = query.filter(Dataset.tenant_id == scope)
    items = query.order_by(Dataset.created_at.desc()).all()
    return ok(request, {"items": [_dataset_dict(item) for item in items], "total": len(items)})


@router.post("/api/v1/datasets/{dataset_id}/versions")
def create_dataset_version_api(
    dataset_id: int,
    payload: DatasetVersionCreate,
    request: Request,
    principal: Principal = Depends(require_permission("governance:write")),
    db: Session = Depends(get_db),
):
    query = db.query(Dataset).filter(Dataset.id == dataset_id)
    if (principal.kind in {"app", "user"} and not principal.is_admin):
        query = query.filter(Dataset.tenant_id == (principal.tenant_id or 0))
    dataset = query.first()
    if dataset is None:
        raise ApiError("RESOURCE_NOT_FOUND", "数据集不存在", 404)
    version = create_dataset_version(db, principal, dataset, payload)
    return ok(request, _dataset_version_dict(version), 201)


@router.get("/api/v1/datasets/{dataset_id}/versions")
def list_dataset_versions(
    dataset_id: int,
    request: Request,
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
):
    query = db.query(Dataset).filter(Dataset.id == dataset_id)
    if (principal.kind in {"app", "user"} and not principal.is_admin):
        query = query.filter(Dataset.tenant_id == (principal.tenant_id or 0))
    if query.first() is None:
        raise ApiError("RESOURCE_NOT_FOUND", "数据集不存在", 404)
    versions = (
        db.query(DatasetVersion)
        .filter(DatasetVersion.dataset_id == dataset_id)
        .order_by(DatasetVersion.created_at.desc())
        .all()
    )
    return ok(
        request,
        {"items": [_dataset_version_dict(item) for item in versions], "total": len(versions)},
    )


@router.post("/api/v1/dataset-versions/{version_id}/publish")
def publish_dataset_version_api(
    version_id: int,
    request: Request,
    principal: Principal = Depends(require_permission("governance:write")),
    db: Session = Depends(get_db),
):
    version = publish_dataset_version(db, principal, version_id)
    audit(db, principal, "dataset.version.publish", "dataset_version", int(version.id))
    return ok(request, _dataset_version_dict(version))


@router.post("/api/v1/dataset-versions/{version_id}/export")
def export_dataset(
    version_id: int,
    request: Request,
    payload: DatasetExportRequest | None = None,
    principal: Principal = Depends(require_permission("governance:write")),
    db: Session = Depends(get_db),
):
    options = payload or DatasetExportRequest()
    filename, content, row_count = export_dataset_version(
        db,
        principal,
        version_id,
        export_format=options.format,
        approved_by=options.approved_by,
    )
    audit(
        db,
        principal,
        "dataset.version.export",
        "dataset_version",
        version_id,
        after={"format": options.format, "rows": row_count},
    )
    media_type = "application/json" if options.format == "json" else "application/x-ndjson"
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Data-Watermark": get_settings().governance_watermark,
            "X-Row-Count": str(row_count),
            "X-Request-Id": getattr(request.state, "request_id", ""),
        },
    )


@router.get("/api/v1/dataset-versions/{version_id}/items")
def dataset_version_items(
    version_id: int,
    request: Request,
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
):
    items = list_dataset_items(db, principal, version_id)
    return ok(
        request,
        {"items": [_dataset_item_dict(item) for item in items], "total": len(items)},
    )


@router.get("/api/v1/dataset-versions/{version_id}/lineage")
def dataset_version_lineage(
    version_id: int,
    request: Request,
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
):
    records = list_dataset_lineage(db, principal, version_id)
    return ok(
        request,
        {"items": [_lineage_dict(item) for item in records], "total": len(records)},
    )


@router.get("/api/v1/events")
def events(
    request: Request,
    tenant_id: int | None = Query(default=None),
    event_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    request_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
):
    records = list_events(
        db,
        principal,
        tenant_id=tenant_id,
        event_type=event_type,
        status=status,
        request_id=request_id,
        limit=limit,
    )
    return ok(
        request,
        {"items": [_event_dict(item) for item in records], "total": len(records)},
    )


@router.post("/api/v1/events/process")
def process_events(
    request: Request,
    tenant_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    result = process_pending_events(db, principal, tenant_id=tenant_id, limit=limit)
    audit(db, principal, "event.process", "ai_event", after=result)
    return ok(request, result)


@router.post("/api/v1/events/{event_id}/replay")
def replay(
    event_id: str,
    request: Request,
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    event = replay_event(db, principal, event_id)
    audit(db, principal, "event.replay", "ai_event", str(event.event_id))
    return ok(request, _event_dict(event))


@router.post("/api/v1/annotation-tasks")
def create_annotation_task(
    payload: AnnotationTaskCreate,
    request: Request,
    principal: Principal = Depends(require_permission("governance:write")),
    db: Session = Depends(get_db),
):
    task = create_annotation_task_record(db, principal, payload)
    audit(db, principal, "annotation.task.create", "annotation_task", int(task.id))
    return ok(request, _annotation_task_dict(task), 201)


@router.get("/api/v1/annotation-tasks")
def get_annotation_tasks(
    request: Request,
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
):
    tasks = list_annotation_tasks(db, principal, status=status, limit=limit)
    return ok(
        request,
        {"items": [_annotation_task_dict(item) for item in tasks], "total": len(tasks)},
    )


@router.post("/api/v1/annotation-tasks/{task_id}/claim")
def claim_task(
    task_id: int,
    request: Request,
    principal: Principal = Depends(require_permission("governance:write")),
    db: Session = Depends(get_db),
):
    task = claim_annotation_task(db, principal, task_id)
    return ok(request, _annotation_task_dict(task))


@router.post("/api/v1/annotation-tasks/{task_id}/records")
def add_annotation_record(
    task_id: int,
    payload: AnnotationRecordCreate,
    request: Request,
    principal: Principal = Depends(require_permission("governance:write")),
    db: Session = Depends(get_db),
):
    record = submit_annotation_record(db, principal, task_id, payload)
    audit(
        db,
        principal,
        "annotation.record.create",
        "annotation_record",
        int(record.id),
        after={"decision": str(record.decision), "item_id": int(record.item_id)},
    )
    return ok(request, _annotation_record_dict(record), 201)


@router.get("/api/v1/annotation-tasks/{task_id}/stats")
def get_annotation_stats(
    task_id: int,
    request: Request,
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
):
    return ok(request, annotation_task_stats(db, principal, task_id))


@router.post("/api/v1/evals")
def create_eval(
    payload: EvalTaskCreate,
    request: Request,
    principal: Principal = Depends(require_permission("governance:write")),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    if settings.job_mode == "worker":
        task, _results = create_eval_task(db, principal, payload, run=False)
        job = enqueue_job(
            db,
            "eval",
            {
                "eval_task_id": task.id,
                "principal": {
                    "kind": principal.kind,
                    "tenant_id": principal.tenant_id,
                    "app_id": principal.app_id,
                    "app_code": principal.app_code,
                    "user_id": principal.user_id,
                    "username": principal.username,
                    "roles": list(principal.roles),
                },
            },
            tenant_id=task.tenant_id,
            app_id=principal.app_id,
            principal=principal,
        )
        data = _eval_task_dict(task)
        data["results"] = []
        data["job"] = job_to_dict(job)
        audit(db, principal, "eval.create", "eval_task", int(task.id), after={"job_id": job.id})
        return ok(request, data, 202)
    task, results = create_eval_task(db, principal, payload)
    audit(
        db,
        principal,
        "eval.create",
        "eval_task",
        int(task.id),
        after={"metrics": task.metrics or {}},
    )
    data = _eval_task_dict(task)
    data["results"] = [_eval_result_dict(item) for item in results]
    return ok(request, data, 201)


@router.get("/api/v1/evals")
def get_evals(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
):
    tasks = list_eval_tasks(db, principal, limit=limit)
    return ok(
        request,
        {"items": [_eval_task_dict(item) for item in tasks], "total": len(tasks)},
    )


@router.get("/api/v1/evals/{eval_id}")
def get_eval(
    eval_id: int,
    request: Request,
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
):
    task, results = get_eval_task(db, principal, eval_id)
    data = _eval_task_dict(task)
    data["results"] = [_eval_result_dict(item) for item in results]
    return ok(request, data)


@router.get("/api/v1/governance/metrics")
def metrics(
    request: Request,
    tenant_id: int | None = Query(default=None),
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
):
    return ok(request, governance_metrics(db, principal, tenant_id=tenant_id))


@router.post("/api/v1/retention-jobs")
def create_retention(
    payload: RetentionJobCreate,
    request: Request,
    principal: Principal = Depends(require_admin),
    db: Session = Depends(get_db),
):
    job = create_retention_job(db, principal, payload)
    audit(
        db,
        principal,
        "retention.run",
        "retention_job",
        int(job.id),
        after={"target": str(job.target), "deleted": int(job.deleted_count or 0)},
    )
    return ok(request, _retention_job_dict(job), 201)


@router.get("/api/v1/retention-jobs")
def get_retention_jobs(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
):
    jobs = list_retention_jobs(db, principal, limit=limit)
    return ok(
        request,
        {"items": [_retention_job_dict(item) for item in jobs], "total": len(jobs)},
    )


@router.get("/api/v1/usage")
def usage(
    request: Request,
    app_id: int | None = Query(default=None),
    from_time: str | None = Query(default=None),
    to_time: str | None = Query(default=None),
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
):
    parsed_from = _parse_time(from_time)
    parsed_to = _parse_time(to_time)
    return ok(request, list_usage(db, principal, app_id, parsed_from, parsed_to))


@router.get("/api/v1/audit-logs")
def audit_logs(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
):
    records = list_audit_logs(db, principal, limit)
    return ok(
        request,
        {
            "items": [
                {
                    "id": item.id,
                    "action": item.action,
                    "resource_type": item.resource_type,
                    "resource_id": item.resource_id,
                    "operator_type": item.operator_type,
                    "operator_id": item.operator_id,
                    "result": item.result,
                    "created_at": item.created_at.isoformat() if item.created_at else None,
                }
                for item in records
            ],
            "total": len(records),
        },
    )


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError as exc:
        raise ApiError("INVALID_ARGUMENT", "时间格式必须为 ISO 8601", 400) from exc




