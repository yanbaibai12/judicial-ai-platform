from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..acl import assert_access
from ..config import get_settings
from ..errors import ApiError
from ..ids import new_id
from ..models import (
    AiEvent,
    AnnotationRecord,
    AnnotationTask,
    AuditLog,
    BadCase,
    Capability,
    Dataset,
    DatasetItem,
    DatasetLineage,
    DatasetVersion,
    EvalResult,
    EvalTask,
    Feedback,
    RetentionJob,
    UsageRecord,
    utcnow,
)
from ..schemas import (
    AnnotationRecordCreate,
    AnnotationTaskCreate,
    BadCaseCreate,
    BadCaseUpdate,
    DatasetCreate,
    DatasetVersionCreate,
    EvalTaskCreate,
    FeedbackCreate,
    RetentionJobCreate,
)
from ..security import Principal
from .governance_pipeline import (
    PIPELINE_VERSION,
    REDACTION_RULES_VERSION,
    answer_overlap,
    content_digest,
    hamming_distance,
    quality_check,
    retrieval_metrics,
    review_status_for,
    sanitize_payload,
    sanitize_text,
    simhash,
)
from .observability import record_event
from .retrieval import get_kb, retrieve


def submit_feedback(db: Session, principal: Principal, payload: FeedbackCreate) -> Feedback:
    feedback = Feedback(
        id=new_id(),
        tenant_id=principal.tenant_id or 0,
        app_id=principal.app_id or 0,
        request_id=payload.request_id,
        rating=payload.rating,
        reason=payload.reason,
        comment=payload.comment,
        corrected_output_ref=payload.corrected_output,
        tags=payload.tags,
        created_by=principal.app_id,
    )
    db.add(feedback)
    db.commit()
    db.refresh(feedback)
    record_event(
        db,
        "FEEDBACK_SUBMITTED",
        tenant_id=principal.tenant_id or 0,
        app_id=principal.app_id,
        request_id=payload.request_id,
        trace_id=payload.request_id,
        payload={
            "rating": payload.rating,
            "reason": payload.reason,
            "comment": payload.comment,
            "corrected_output": payload.corrected_output,
            "tags": payload.tags,
        },
    )
    return feedback


def create_bad_case(db: Session, principal: Principal, payload: BadCaseCreate) -> BadCase:
    record = BadCase(
        id=new_id(),
        tenant_id=principal.tenant_id or 0,
        request_id=payload.request_id,
        category=payload.category,
        sub_category=payload.sub_category,
        root_cause=payload.root_cause,
        priority=payload.priority,
        owner=payload.owner,
        status="OPEN",
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def update_bad_case(
    db: Session, principal: Principal, case_id: int, payload: BadCaseUpdate
) -> BadCase:
    record = (
        db.query(BadCase)
        .filter(BadCase.id == case_id, BadCase.tenant_id == (principal.tenant_id or 0))
        .first()
    )
    if record is None:
        raise ApiError("RESOURCE_NOT_FOUND", "Bad Case 不存在", 404)
    for field in ("root_cause", "priority", "owner", "fix_ref"):
        value = getattr(payload, field)
        if value is not None:
            setattr(record, field, value)
    if payload.status is not None:
        record.status = payload.status
        if payload.status == "CLOSED":
            record.closed_at = utcnow()
    db.commit()
    db.refresh(record)

    should_promote = record.regression_case_id is None and (
        payload.add_to_regression
        or (record.status == "CLOSED" and bool(record.fix_ref))
    )
    if should_promote:
        promote_bad_case_to_regression(db, principal, record)
    db.refresh(record)
    return record


def create_dataset(db: Session, principal: Principal, payload: DatasetCreate) -> Dataset:
    tenant_id = principal.tenant_id or 0
    existing = (
        db.query(Dataset)
        .filter(Dataset.tenant_id == tenant_id, Dataset.code == payload.code)
        .first()
    )
    if existing:
        raise ApiError("RESOURCE_CONFLICT", "数据集编码已存在", 409)
    dataset = Dataset(
        id=new_id(),
        tenant_id=tenant_id,
        code=payload.code,
        name=payload.name,
        type=payload.type,
        description=payload.description,
        owner=payload.owner,
    )
    db.add(dataset)
    db.commit()
    db.refresh(dataset)
    return dataset


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _flatten_input_text(payload: dict[str, Any]) -> tuple[str, str]:
    """从事件/调用入参中提取 (system 指令, 用户输入)。"""
    messages = payload.get("messages")
    if isinstance(messages, list):
        system_parts: list[str] = []
        user_parts: list[str] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, list):
                content = json.dumps(content, ensure_ascii=False)
            text = str(content or "")
            role = message.get("role")
            if role == "system":
                system_parts.append(text)
            elif role == "user":
                user_parts.append(text)
        if user_parts:
            return " ".join(system_parts), "\n".join(user_parts)

    for key in ("input_text", "query", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return "", value
    if "input" in payload:
        return "", json.dumps(payload["input"], ensure_ascii=False)
    return "", json.dumps(payload, ensure_ascii=False)


def _add_lineage(
    db: Session,
    tenant_id: int,
    dataset_version_id: int,
    stage: str,
    *,
    input_ref: dict[str, Any] | None = None,
    output_ref: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    stats: dict[str, Any] | None = None,
    operator_id: int = 0,
) -> DatasetLineage:
    record = DatasetLineage(
        id=new_id(),
        tenant_id=tenant_id,
        dataset_version_id=dataset_version_id,
        stage=stage,
        input_ref=input_ref or {},
        output_ref=output_ref or {},
        config=config or {},
        stats=stats or {},
        operator_id=operator_id,
    )
    db.add(record)
    return record


def _event_candidate(event: AiEvent) -> dict[str, Any]:
    payload = event.payload or {}
    instruction, input_text = _flatten_input_text(payload)
    output = payload.get("output_text") or payload.get("output") or ""
    if not isinstance(output, str):
        output = json.dumps(output, ensure_ascii=False)
    corrected = payload.get("corrected_output") or ""
    if not isinstance(corrected, str):
        corrected = json.dumps(corrected, ensure_ascii=False)
    return {
        "source_event_id": event.event_id,
        "request_id": event.request_id,
        "item_type": "event",
        "instruction": instruction,
        "input_text": input_text,
        "output_text": output,
        "corrected_output": corrected,
        "input_payload": payload.get("input_payload") or {},
        "label": {
            "event_type": event.event_type,
            "rating": payload.get("rating"),
            "reason": payload.get("reason"),
            "comment": payload.get("comment"),
            "tags": payload.get("tags") or [],
        },
        "security_level": payload.get("security_level", "INTERNAL"),
        "redaction": payload.get("redaction") or {},
        "has_citation": bool(payload.get("citations") or payload.get("chunk_ids")),
        "business_score": 1.0 if payload.get("rating") == "POSITIVE" else 0.5,
        "query": payload.get("query") or "",
        "kb_ids": payload.get("kb_ids") or [],
        "chunk_ids": payload.get("chunk_ids") or [],
        "relevant_chunk_ids": payload.get("relevant_chunk_ids") or [],
    }


def _feedback_candidate(
    db: Session, dataset: Dataset, feedback: Feedback
) -> dict[str, Any]:
    event = (
        db.query(AiEvent)
        .filter(
            AiEvent.tenant_id == dataset.tenant_id,
            AiEvent.request_id == feedback.request_id,
            AiEvent.event_type == "INFERENCE_COMPLETED",
        )
        .order_by(AiEvent.created_at.desc())
        .first()
    )
    instruction = ""
    input_text = feedback.request_id
    output_text = ""
    security_level = "INTERNAL"
    has_citation = False
    if event is not None:
        payload = event.payload or {}
        instruction, event_input = _flatten_input_text(payload)
        if event_input.strip():
            input_text = event_input
        output_text = str(payload.get("output_text") or "")
        security_level = str(payload.get("security_level", "INTERNAL"))
        has_citation = bool(payload.get("citations") or payload.get("chunk_ids"))
    if not output_text:
        output_text = feedback.corrected_output_ref or feedback.comment or "（无输出记录）"
    corrected = feedback.corrected_output_ref or ""
    business_score = (
        1.0
        if feedback.rating == "POSITIVE"
        else (0.5 if corrected else 0.2)
    )
    return {
        "source_event_id": event.event_id if event is not None else None,
        "request_id": feedback.request_id,
        "item_type": "feedback",
        "instruction": instruction,
        "input_text": input_text,
        "output_text": output_text,
        "corrected_output": corrected,
        "input_payload": {"request_id": feedback.request_id},
        "label": {
            "rating": feedback.rating,
            "reason": feedback.reason,
            "comment": feedback.comment,
            "tags": feedback.tags or [],
        },
        "security_level": security_level,
        "has_citation": has_citation,
        "business_score": business_score,
    }


def create_dataset_version(
    db: Session,
    principal: Principal,
    dataset: Dataset,
    payload: DatasetVersionCreate,
) -> DatasetVersion:
    existing = (
        db.query(DatasetVersion)
        .filter(
            DatasetVersion.dataset_id == dataset.id,
            DatasetVersion.version == payload.version,
        )
        .first()
    )
    if existing:
        raise ApiError("RESOURCE_CONFLICT", "数据集版本已存在", 409)

    settings = get_settings()
    source_snapshot = payload.source_snapshot or {}
    filter_config = payload.filter_config or {}
    from_time = _parse_datetime(source_snapshot.get("from"))
    to_time = _parse_datetime(source_snapshot.get("to"))
    event_types = [str(item) for item in (source_snapshot.get("event_types") or [])]

    candidates: list[dict[str, Any]] = []
    if event_types:
        query = db.query(AiEvent).filter(
            AiEvent.tenant_id == dataset.tenant_id,
            AiEvent.event_type.in_(event_types),
        )
        if from_time:
            query = query.filter(AiEvent.created_at >= from_time)
        if to_time:
            query = query.filter(AiEvent.created_at <= to_time)
        for event in query.order_by(AiEvent.created_at.asc()).all():
            candidates.append(_event_candidate(event))
    else:
        query = db.query(Feedback).filter(Feedback.tenant_id == dataset.tenant_id)
        if from_time:
            query = query.filter(Feedback.created_at >= from_time)
        if to_time:
            query = query.filter(Feedback.created_at <= to_time)
        rating_filter = filter_config.get("rating")
        if rating_filter:
            query = query.filter(Feedback.rating.in_(rating_filter))
        for feedback in query.order_by(Feedback.created_at.asc()).all():
            candidates.append(_feedback_candidate(db, dataset, feedback))

    version = DatasetVersion(
        id=new_id(),
        dataset_id=dataset.id,
        version=payload.version,
        status="PUBLISHED" if payload.publish else "DRAFT",
        source_snapshot=source_snapshot,
        filter_config=filter_config,
        prelabel_model="rule-based/v1",
        stats={},
        created_by=principal.app_id or 0,
        published_at=utcnow() if payload.publish else None,
    )
    db.add(version)
    db.flush()

    min_score = float(
        filter_config.get("min_quality_score", settings.governance_min_quality_score)
    )
    auto_accept = float(
        filter_config.get("auto_accept_score", settings.governance_auto_accept_score)
    )
    dedup_distance = int(
        filter_config.get(
            "dedup_hamming_distance", settings.governance_dedup_hamming_distance
        )
    )
    redact = bool(filter_config.get("redact", settings.governance_redaction_enabled))
    citation_required = bool(filter_config.get("citation_required", False))
    excluded_security_levels = tuple(
        filter_config.get("exclude_security_levels")
        or filter_config.get("security_levels")
        or ()
    )
    dedup_enabled = bool(filter_config.get("dedup", False))
    max_chars = settings.governance_event_max_chars
    retrieval_eval = dataset.type == "RETRIEVAL_EVAL"

    accepted: list[dict[str, Any]] = []
    rejected = 0
    duplicate = 0
    redaction_stats: dict[str, int] = {}
    seen_digests: set[str] = set()
    seen_simhashes: list[int] = []

    for candidate in candidates:
        raw_input = str(candidate.get("input_text") or "")
        raw_output = str(candidate.get("output_text") or "")
        raw_corrected = str(candidate.get("corrected_output") or "")
        for name, hit in (candidate.get("redaction") or {}).items():
            redaction_stats[name] = redaction_stats.get(name, 0) + int(hit)

        if retrieval_eval:
            query_text, query_counts = sanitize_text(
                str(candidate.get("query") or raw_input),
                redact=redact,
                max_chars=max_chars,
            )
            if not query_text:
                rejected += 1
                continue
            chunk_ids = [str(item) for item in candidate.get("chunk_ids") or []]
            relevant = [
                str(item) for item in candidate.get("relevant_chunk_ids") or []
            ] or chunk_ids
            digest = content_digest(query_text)
            if dedup_enabled and digest in seen_digests:
                duplicate += 1
                continue
            seen_digests.add(digest)
            for name, hit in query_counts.items():
                redaction_stats[name] = redaction_stats.get(name, 0) + hit
            accepted.append(
                {
                    "candidate": candidate,
                    "input": {
                        "query": query_text,
                        "kb_ids": candidate.get("kb_ids") or [],
                    },
                    "output": {"chunk_ids": chunk_ids},
                    "label": {"relevant_chunk_ids": relevant},
                    "quality_score": 1.0,
                    "review_status": "AUTO_ACCEPTED",
                    "redaction": query_counts,
                    "quality": {"rules": {"query_not_empty": True}},
                }
            )
            continue

        masked_input, input_counts = sanitize_text(
            raw_input, redact=redact, max_chars=max_chars
        )
        masked_output, output_counts = sanitize_text(
            raw_output, redact=redact, max_chars=max_chars
        )
        masked_corrected, corrected_counts = sanitize_text(
            raw_corrected, redact=redact, max_chars=max_chars
        )
        for counts in (input_counts, output_counts, corrected_counts):
            for name, hit in counts.items():
                redaction_stats[name] = redaction_stats.get(name, 0) + hit

        score, passed, detail = quality_check(
            masked_input,
            masked_output,
            security_level=str(candidate.get("security_level", "INTERNAL")),
            excluded_security_levels=excluded_security_levels,
            has_citation=bool(candidate.get("has_citation")),
            citation_required=citation_required,
            business_score=float(candidate.get("business_score", 0.5)),
            min_score=min_score,
        )
        if not passed:
            rejected += 1
            continue

        chosen = masked_corrected or masked_output
        dedup_text = f"{masked_input}\n{chosen}"
        digest = content_digest(dedup_text)
        signature = simhash(dedup_text)
        if dedup_enabled:
            if digest in seen_digests or any(
                hamming_distance(signature, previous) <= dedup_distance
                for previous in seen_simhashes
            ):
                duplicate += 1
                continue
            seen_digests.add(digest)
            seen_simhashes.append(signature)

        label_payload, _ = sanitize_payload(
            candidate.get("label") or {}, redact=redact, max_chars=max_chars
        )
        instruction, _ = sanitize_text(
            str(candidate.get("instruction") or ""),
            redact=redact,
            max_chars=max_chars,
        )
        instruction = instruction or "请完成以下司法任务"
        item_input: dict[str, Any] = {
            "instruction": instruction,
            "input": masked_input,
        }
        item_output: dict[str, Any]
        if dataset.type == "PREFERENCE":
            item_input = {"prompt": masked_input}
            item_output = {
                "chosen": chosen,
                "rejected": masked_output,
            }
        elif dataset.type == "EVAL":
            item_input = {"input": masked_input}
            item_output = {"reference": chosen, "prediction": masked_output}
        elif dataset.type == "BAD_CASE":
            item_input = {"input": masked_input}
            item_output = {"text": masked_output}
        elif dataset.type == "SAFETY":
            item_input = {"input": masked_input}
            item_output = {"expected_behavior": masked_corrected or "REFUSE"}
        elif dataset.type == "RETRIEVAL_EVAL":
            item_input = {"query": masked_input}
            item_output = {"chunk_ids": []}
        else:
            item_output = {"output": chosen}

        accepted.append(
            {
                "candidate": candidate,
                "input": item_input,
                "output": item_output,
                "label": label_payload,
                "quality_score": score,
                "review_status": review_status_for(score, auto_accept),
                "redaction": {
                    **input_counts,
                    **output_counts,
                    **corrected_counts,
                },
                "quality": detail,
            }
        )

    total = len(accepted)
    train_ratio = float(payload.split.get("train", 0.8))
    validation_ratio = float(payload.split.get("validation", 0.1))
    train_count = int(total * train_ratio)
    validation_count = int(total * validation_ratio)
    split_stats = {"train": 0, "validation": 0, "test": 0}
    score_sum = 0.0

    for index, item in enumerate(accepted):
        if index < train_count:
            split = "train"
        elif index < train_count + validation_count:
            split = "validation"
        else:
            split = "test"
        split_stats[split] += 1
        score_sum += float(item["quality_score"])
        candidate = item["candidate"]
        db.add(
            DatasetItem(
                id=new_id(),
                dataset_version_id=version.id,
                item_type=str(candidate.get("item_type", "feedback")),
                input=item["input"],
                output=item["output"],
                label=item["label"],
                extra={
                    "sources": [candidate.get("request_id") or candidate.get("source_event_id")],
                    "source_event_id": candidate.get("source_event_id"),
                    "security_level": candidate.get("security_level", "INTERNAL"),
                    "quality": item["quality"],
                    "redaction": item["redaction"],
                    "pipeline_version": PIPELINE_VERSION,
                },
                source_event_id=candidate.get("source_event_id"),
                quality_score=item["quality_score"],
                review_status=item["review_status"],
                split=split,
            )
        )

    version.stats = {
        "total": total,
        "source_count": len(candidates),
        "accepted": total,
        "rejected": rejected,
        "duplicate": duplicate,
        "avg_quality_score": round(score_sum / total, 6) if total else 0.0,
        "redaction": redaction_stats,
        **split_stats,
    }
    _add_lineage(
        db,
        dataset.tenant_id,
        version.id,
        "COLLECT",
        input_ref=source_snapshot,
        stats={"source_count": len(candidates), "event_types": event_types},
        operator_id=principal.app_id or 0,
    )
    _add_lineage(
        db,
        dataset.tenant_id,
        version.id,
        "DESENSITIZE",
        config={"redact": redact, "rules_version": REDACTION_RULES_VERSION},
        stats=redaction_stats,
        operator_id=principal.app_id or 0,
    )
    _add_lineage(
        db,
        dataset.tenant_id,
        version.id,
        "QUALITY",
        config={
            "min_quality_score": min_score,
            "auto_accept_score": auto_accept,
            "citation_required": citation_required,
        },
        stats={
            "accepted": total,
            "rejected": rejected,
            "avg_quality_score": version.stats["avg_quality_score"],
        },
        operator_id=principal.app_id or 0,
    )
    _add_lineage(
        db,
        dataset.tenant_id,
        version.id,
        "DEDUP",
        config={"enabled": dedup_enabled, "hamming_distance": dedup_distance},
        stats={"duplicates": duplicate},
        operator_id=principal.app_id or 0,
    )
    _add_lineage(
        db,
        dataset.tenant_id,
        version.id,
        "SPLIT",
        config=payload.split,
        stats=split_stats,
        operator_id=principal.app_id or 0,
    )
    if payload.publish:
        _add_lineage(
            db,
            dataset.tenant_id,
            version.id,
            "PUBLISH",
            output_ref={"status": "PUBLISHED", "item_count": total},
            operator_id=principal.app_id or 0,
        )
    db.commit()
    db.refresh(version)
    return version


def list_usage(
    db: Session,
    principal: Principal,
    app_id: int | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
) -> dict[str, Any]:
    query = db.query(
        UsageRecord.capability_id,
        func.count(UsageRecord.id).label("requests"),
        func.sum(UsageRecord.input_tokens).label("input_tokens"),
        func.sum(UsageRecord.output_tokens).label("output_tokens"),
        func.sum(UsageRecord.total_tokens).label("total_tokens"),
        func.avg(UsageRecord.latency_ms).label("avg_latency_ms"),
    )
    if (principal.kind in {"app", "user"} and not principal.is_admin):
        query = query.filter(UsageRecord.app_id == principal.app_id)
    elif app_id:
        query = query.filter(UsageRecord.app_id == app_id)
    if principal.tenant_id is not None:
        query = query.filter(UsageRecord.tenant_id == principal.tenant_id)
    if from_time:
        query = query.filter(UsageRecord.created_at >= from_time)
    if to_time:
        query = query.filter(UsageRecord.created_at <= to_time)
    rows = query.group_by(UsageRecord.capability_id).all()

    capabilities = (
        {
            capability.id: capability
            for capability in db.query(Capability)
            .filter(Capability.id.in_([row.capability_id for row in rows]))
            .all()
        }
        if rows
        else {}
    )

    items = []
    for row in rows:
        capability = capabilities.get(row.capability_id)
        items.append(
            {
                "capability_id": row.capability_id,
                "capability_code": capability.code if capability else None,
                "capability_name": capability.name if capability else None,
                "requests": int(row.requests or 0),
                "input_tokens": int(row.input_tokens or 0),
                "output_tokens": int(row.output_tokens or 0),
                "total_tokens": int(row.total_tokens or 0),
                "avg_latency_ms": round(float(row.avg_latency_ms or 0), 2),
            }
        )
    return {
        "items": items,
        "total_requests": sum(item["requests"] for item in items),
        "total_tokens": sum(item["total_tokens"] for item in items),
    }


def list_audit_logs(
    db: Session,
    principal: Principal,
    limit: int = 100,
) -> list[AuditLog]:
    query = db.query(AuditLog)
    if (principal.kind in {"app", "user"} and not principal.is_admin):
        query = query.filter(AuditLog.tenant_id == (principal.tenant_id or 0))
    return query.order_by(AuditLog.created_at.desc()).limit(min(limit, 500)).all()


def list_events(
    db: Session,
    principal: Principal,
    *,
    tenant_id: int | None = None,
    event_type: str | None = None,
    status: str | None = None,
    request_id: str | None = None,
    limit: int = 100,
) -> list[AiEvent]:
    query = db.query(AiEvent)
    if (principal.kind in {"app", "user"} and not principal.is_admin):
        query = query.filter(AiEvent.tenant_id == (principal.tenant_id or 0))
    elif tenant_id is not None:
        query = query.filter(AiEvent.tenant_id == tenant_id)
    if event_type:
        query = query.filter(AiEvent.event_type == event_type)
    if status:
        query = query.filter(AiEvent.status == status)
    if request_id:
        query = query.filter(AiEvent.request_id == request_id)
    return query.order_by(AiEvent.created_at.desc()).limit(min(limit, 500)).all()


def process_pending_events(
    db: Session,
    principal: Principal,
    *,
    tenant_id: int | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    settings = get_settings()
    query = db.query(AiEvent).filter(AiEvent.status == "NEW")
    if (principal.kind in {"app", "user"} and not principal.is_admin):
        query = query.filter(AiEvent.tenant_id == (principal.tenant_id or 0))
    elif tenant_id is not None:
        query = query.filter(AiEvent.tenant_id == tenant_id)
    events = query.order_by(AiEvent.created_at.asc()).limit(min(limit, 500)).all()

    processed = 0
    redaction: dict[str, int] = {}
    for event in events:
        payload, counts = sanitize_payload(
            event.payload or {},
            redact=settings.governance_redaction_enabled,
            max_chars=settings.governance_event_max_chars,
        )
        if counts:
            payload["redaction"] = counts
            for name, hit in counts.items():
                redaction[name] = redaction.get(name, 0) + hit
        event.payload = payload
        event.status = "PROCESSED"
        processed += 1
    db.commit()
    return {"processed": processed, "redaction": redaction}


def replay_event(db: Session, principal: Principal, event_id: str) -> AiEvent:
    query = db.query(AiEvent).filter(AiEvent.event_id == event_id)
    if (principal.kind in {"app", "user"} and not principal.is_admin):
        query = query.filter(AiEvent.tenant_id == (principal.tenant_id or 0))
    event = query.first()
    if event is None:
        raise ApiError("RESOURCE_NOT_FOUND", "事件不存在", 404, {"event_id": event_id})
    event.status = "NEW"
    db.commit()
    db.refresh(event)
    return event


def _dataset_version_for(
    db: Session, principal: Principal, version_id: int
) -> tuple[Dataset, DatasetVersion]:
    query = (
        db.query(Dataset, DatasetVersion)
        .join(DatasetVersion, DatasetVersion.dataset_id == Dataset.id)
        .filter(DatasetVersion.id == version_id)
    )
    if (principal.kind in {"app", "user"} and not principal.is_admin):
        query = query.filter(Dataset.tenant_id == (principal.tenant_id or 0))
    row = query.first()
    if row is None:
        raise ApiError("RESOURCE_NOT_FOUND", "数据集版本不存在", 404)
    return row[0], row[1]


def publish_dataset_version(
    db: Session, principal: Principal, version_id: int
) -> DatasetVersion:
    dataset, version = _dataset_version_for(db, principal, version_id)
    if version.status == "ARCHIVED":
        raise ApiError("JOB_STATE_INVALID", "已归档版本不能发布", 409)
    if version.status == "PUBLISHED":
        return version
    item_count = (
        db.query(func.count(DatasetItem.id))
        .filter(DatasetItem.dataset_version_id == version.id)
        .scalar()
        or 0
    )
    if item_count == 0:
        raise ApiError("JOB_STATE_INVALID", "空数据集版本不能发布", 409)
    version.status = "PUBLISHED"
    version.approved_by = principal.app_id or 0
    version.published_at = utcnow()
    _add_lineage(
        db,
        dataset.tenant_id,
        version.id,
        "PUBLISH",
        output_ref={"status": "PUBLISHED", "item_count": int(item_count)},
        operator_id=principal.app_id or 0,
    )
    db.commit()
    db.refresh(version)
    return version


def list_dataset_items(
    db: Session, principal: Principal, version_id: int
) -> list[DatasetItem]:
    _dataset, version = _dataset_version_for(db, principal, version_id)
    return (
        db.query(DatasetItem)
        .filter(DatasetItem.dataset_version_id == version.id)
        .order_by(DatasetItem.created_at)
        .all()
    )


def list_dataset_lineage(
    db: Session, principal: Principal, version_id: int
) -> list[DatasetLineage]:
    _dataset, version = _dataset_version_for(db, principal, version_id)
    return (
        db.query(DatasetLineage)
        .filter(DatasetLineage.dataset_version_id == version.id)
        .order_by(DatasetLineage.created_at)
        .all()
    )


def export_dataset_version(
    db: Session,
    principal: Principal,
    version_id: int,
    *,
    export_format: str = "jsonl",
    approved_by: str | None = None,
) -> tuple[str, str, int]:
    settings = get_settings()
    dataset, version = _dataset_version_for(db, principal, version_id)
    if version.status != "PUBLISHED":
        raise ApiError("JOB_STATE_INVALID", "只有已发布版本可以导出", 409)
    items = (
        db.query(DatasetItem)
        .filter(DatasetItem.dataset_version_id == version.id)
        .order_by(DatasetItem.created_at)
        .all()
    )
    watermark = f"{settings.governance_watermark}:{dataset.code}:{version.version}"
    rows = []
    for item in items:
        rows.append(
            {
                "item_type": item.item_type,
                "input": item.input,
                "output": item.output,
                "label": item.label,
                "split": item.split,
                "quality_score": float(item.quality_score) if item.quality_score else None,
                "review_status": item.review_status,
                "metadata": item.extra or {},
                "_watermark": watermark,
            }
        )
    if export_format == "json":
        content = json.dumps(
            {"watermark": watermark, "version": version.version, "items": rows},
            ensure_ascii=False,
            indent=2,
        )
        suffix = "json"
    else:
        content = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
        suffix = "jsonl"
    _add_lineage(
        db,
        dataset.tenant_id,
        version.id,
        "EXPORT",
        input_ref={"version": version.version},
        output_ref={"format": suffix, "rows": len(rows)},
        config={"approved_by": approved_by},
        operator_id=principal.app_id or 0,
    )
    db.commit()
    filename = f"{dataset.code}-{version.version}.{suffix}"
    return filename, content, len(rows)


def _annotation_task_for(db: Session, principal: Principal, task_id: int) -> AnnotationTask:
    query = db.query(AnnotationTask).filter(AnnotationTask.id == task_id)
    if (principal.kind in {"app", "user"} and not principal.is_admin):
        query = query.filter(AnnotationTask.tenant_id == (principal.tenant_id or 0))
    task = query.first()
    if task is None:
        raise ApiError("RESOURCE_NOT_FOUND", "标注任务不存在", 404)
    return task


def create_annotation_task(
    db: Session, principal: Principal, payload: AnnotationTaskCreate
) -> AnnotationTask:
    dataset_version = None
    dataset = None
    if payload.dataset_version_id:
        dataset, dataset_version = _dataset_version_for(
            db, principal, payload.dataset_version_id
        )
    tenant_id = dataset.tenant_id if dataset is not None else (principal.tenant_id or 0)
    total_items = payload.total_items
    if dataset_version is not None and total_items <= 0:
        total_items = int(
            db.query(func.count(DatasetItem.id))
            .filter(DatasetItem.dataset_version_id == dataset_version.id)
            .scalar()
            or 0
        )
    task = AnnotationTask(
        id=new_id(),
        tenant_id=tenant_id,
        dataset_version_id=dataset_version.id if dataset_version else None,
        name=payload.name,
        task_type=payload.task_type,
        total_items=total_items,
        required_reviewers=payload.required_reviewers,
        status="PENDING",
        assignee=payload.assignee,
        due_at=_parse_datetime(payload.due_at),
        created_by=principal.app_id or 0,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def list_annotation_tasks(
    db: Session,
    principal: Principal,
    *,
    status: str | None = None,
    limit: int = 100,
) -> list[AnnotationTask]:
    query = db.query(AnnotationTask)
    if (principal.kind in {"app", "user"} and not principal.is_admin):
        query = query.filter(AnnotationTask.tenant_id == (principal.tenant_id or 0))
    if status:
        query = query.filter(AnnotationTask.status == status)
    return query.order_by(AnnotationTask.created_at.desc()).limit(min(limit, 500)).all()


def claim_annotation_task(
    db: Session, principal: Principal, task_id: int
) -> AnnotationTask:
    task = _annotation_task_for(db, principal, task_id)
    if task.status in {"COMPLETED", "CANCELLED"}:
        raise ApiError("JOB_STATE_INVALID", "任务已结束，不能领取", 409)
    task.assignee = principal.app_code or "admin"
    task.status = "IN_PROGRESS"
    db.commit()
    db.refresh(task)
    return task


def _refresh_annotation_task_status(db: Session, task: AnnotationTask) -> None:
    if task.dataset_version_id is None:
        records = (
            db.query(func.count(AnnotationRecord.id))
            .filter(AnnotationRecord.task_id == task.id)
            .scalar()
            or 0
        )
        if records:
            task.status = "IN_PROGRESS"
        return
    items = (
        db.query(DatasetItem)
        .filter(DatasetItem.dataset_version_id == task.dataset_version_id)
        .all()
    )
    if not items:
        return
    completed = 0
    for item in items:
        review_count = (
            db.query(func.count(AnnotationRecord.id))
            .filter(
                AnnotationRecord.task_id == task.id,
                AnnotationRecord.item_id == item.id,
            )
            .scalar()
            or 0
        )
        if review_count >= task.required_reviewers:
            completed += 1
    task.total_items = len(items)
    task.status = "COMPLETED" if completed >= len(items) else "IN_PROGRESS"


def submit_annotation_record(
    db: Session, principal: Principal, task_id: int, payload: AnnotationRecordCreate
) -> AnnotationRecord:
    task = _annotation_task_for(db, principal, task_id)
    if task.status == "CANCELLED":
        raise ApiError("JOB_STATE_INVALID", "任务已取消", 409)
    item = db.get(DatasetItem, payload.item_id)
    if item is None:
        raise ApiError("RESOURCE_NOT_FOUND", "标注样本不存在", 404)
    if task.dataset_version_id and item.dataset_version_id != task.dataset_version_id:
        raise ApiError("INVALID_ARGUMENT", "样本不属于该标注任务", 400)
    reviewer_id = (
        payload.reviewer_id
        if payload.reviewer_id is not None
        else (principal.app_id or 0)
    )
    existing = (
        db.query(AnnotationRecord)
        .filter(
            AnnotationRecord.task_id == task.id,
            AnnotationRecord.item_id == item.id,
            AnnotationRecord.reviewer_id == reviewer_id,
        )
        .first()
    )
    if existing:
        raise ApiError("RESOURCE_CONFLICT", "该复核人已提交过此样本", 409)
    record = AnnotationRecord(
        id=new_id(),
        task_id=task.id,
        item_id=item.id,
        reviewer_id=reviewer_id,
        label=payload.label,
        decision=payload.decision,
        confidence=payload.confidence,
        comment=payload.comment,
    )
    db.add(record)
    db.flush()

    records = (
        db.query(AnnotationRecord)
        .filter(
            AnnotationRecord.task_id == task.id,
            AnnotationRecord.item_id == item.id,
        )
        .all()
    )
    decisions = {record_item.decision for record_item in records}
    if "ARBITRATED" in decisions:
        item.review_status = "ARBITRATED"
    elif len(records) >= task.required_reviewers:
        if decisions == {"APPROVED"}:
            item.review_status = "APPROVED"
        elif decisions == {"REJECTED"}:
            item.review_status = "REJECTED"
        else:
            item.review_status = "ARBITRATION_REQUIRED"
    else:
        item.review_status = "IN_REVIEW"

    _refresh_annotation_task_status(db, task)
    if task.dataset_version_id:
        _add_lineage(
            db,
            task.tenant_id,
            task.dataset_version_id,
            "ANNOTATE",
            input_ref={"task_id": task.id, "item_id": item.id},
            output_ref={"decision": payload.decision},
            config={"reviewer_id": reviewer_id},
            operator_id=principal.app_id or 0,
        )
    db.commit()
    db.refresh(record)
    record_event(
        db,
        "ANNOTATION_COMPLETED",
        tenant_id=task.tenant_id,
        app_id=principal.app_id,
        request_id=None,
        trace_id=None,
        payload={
            "task_id": task.id,
            "item_id": item.id,
            "reviewer_id": reviewer_id,
            "decision": payload.decision,
            "confidence": payload.confidence,
        },
    )
    return record


def _agreement_metrics(records: list[AnnotationRecord]) -> dict[str, float]:
    grouped: dict[tuple[int, int], list[str]] = {}
    for record in records:
        grouped.setdefault((record.task_id, record.item_id), []).append(record.decision)

    pairs: list[tuple[int, int]] = []
    same = 0
    comparable = 0
    for decisions in grouped.values():
        if len(decisions) < 2:
            continue
        for left in range(len(decisions)):
            for right in range(left + 1, len(decisions)):
                comparable += 1
                left_value = 1 if decisions[left] == "APPROVED" else 0
                right_value = 1 if decisions[right] == "APPROVED" else 0
                pairs.append((left_value, right_value))
                if decisions[left] == decisions[right]:
                    same += 1
    agreement = same / comparable if comparable else 0.0
    if not pairs:
        return {"agreement_rate": 0.0, "cohen_kappa": 0.0}
    observed = same / len(pairs)
    positive = sum(left + right for left, right in pairs) / (2 * len(pairs))
    expected = positive**2 + (1 - positive) ** 2
    if expected >= 1:
        kappa = 1.0 if observed >= 1 else 0.0
    else:
        kappa = (observed - expected) / (1 - expected)
    return {
        "agreement_rate": round(agreement, 6),
        "cohen_kappa": round(kappa, 6),
    }


def annotation_task_stats(
    db: Session, principal: Principal, task_id: int
) -> dict[str, Any]:
    task = _annotation_task_for(db, principal, task_id)
    records = (
        db.query(AnnotationRecord)
        .filter(AnnotationRecord.task_id == task.id)
        .order_by(AnnotationRecord.created_at)
        .all()
    )
    decision_counts: dict[str, int] = {}
    confidence_sum = 0.0
    confidence_count = 0
    reviewed_items: set[int] = set()
    for record in records:
        decision = str(record.decision)
        decision_counts[decision] = decision_counts.get(decision, 0) + 1
        reviewed_items.add(record.item_id)
        if record.confidence is not None:
            confidence_sum += float(record.confidence)
            confidence_count += 1
    approved = decision_counts.get("APPROVED", 0)
    rejected = decision_counts.get("REJECTED", 0)
    agreement = _agreement_metrics(records)
    return {
        "task_id": task.id,
        "status": task.status,
        "total_items": task.total_items,
        "reviewed_items": len(reviewed_items),
        "records": len(records),
        "decision_counts": decision_counts,
        "approval_rate": round(
            approved / (approved + rejected), 6
        )
        if approved + rejected
        else 0.0,
        "avg_confidence": round(confidence_sum / confidence_count, 6)
        if confidence_count
        else 0.0,
        **agreement,
    }


def create_eval_task(
    db: Session, principal: Principal, payload: EvalTaskCreate, *, run: bool = True
) -> tuple[EvalTask, list[EvalResult]]:
    dataset, version = _dataset_version_for(db, principal, payload.dataset_version_id)
    task = EvalTask(
        id=new_id(),
        tenant_id=dataset.tenant_id,
        dataset_version_id=version.id,
        target_type=payload.target_type,
        target_version=payload.target_version,
        config=payload.config,
        metrics={},
        status="PENDING",
    )
    db.add(task)
    db.flush()
    if not run:
        db.commit()
        db.refresh(task)
        return task, []
    results = _run_eval_task(db, task, dataset, version, principal)
    db.commit()
    db.refresh(task)
    record_event(
        db,
        "EVAL_COMPLETED",
        tenant_id=task.tenant_id,
        app_id=principal.app_id,
        request_id=None,
        trace_id=None,
        payload={
            "eval_task_id": task.id,
            "dataset_version_id": version.id,
            "status": task.status,
            "metrics": task.metrics or {},
        },
    )
    return task, results


def _run_eval_task(
    db: Session, task: EvalTask, dataset: Dataset, version: DatasetVersion, principal: Principal
) -> list[EvalResult]:
    task.status = "RUNNING"
    items = (
        db.query(DatasetItem)
        .filter(DatasetItem.dataset_version_id == version.id)
        .order_by(DatasetItem.created_at)
        .all()
    )
    config = task.config or {}
    try:
        if dataset.type == "RETRIEVAL_EVAL" or task.target_type == "RETRIEVAL":
            metrics = _run_retrieval_eval(db, task, items, config, principal)
        else:
            metrics = _run_text_eval(items, config)
        task.metrics = metrics
        task.status = "SUCCEEDED"
        task.finished_at = utcnow()
        results: list[EvalResult] = []
        for metric, value in metrics.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            result = EvalResult(
                id=new_id(),
                eval_task_id=task.id,
                metric=metric,
                value=float(value),
                detail={"dataset_type": dataset.type},
            )
            db.add(result)
            results.append(result)
        _add_lineage(
            db,
            task.tenant_id,
            version.id,
            "EVAL",
            input_ref={"eval_task_id": task.id, "target": task.target_version},
            output_ref={"status": task.status},
            config=config,
            stats=metrics,
        )
        return results
    except Exception as exc:  # noqa: BLE001 - 评测失败需落库并保留现场
        task.status = "FAILED"
        task.error = str(exc)[:1000]
        task.finished_at = utcnow()
        task.metrics = {"error": str(exc)[:1000]}
        return []


def _run_retrieval_eval(
    db: Session,
    task: EvalTask,
    items: list[DatasetItem],
    config: dict[str, Any],
    principal: Principal,
) -> dict[str, Any]:
    top_k = int(config.get("top_k", 3))
    aggregates: dict[str, list[float]] = {
        "recall_at_k": [],
        "precision_at_5": [],
        "mrr": [],
        "ndcg_at_10": [],
    }
    samples = 0
    for item in items:
        input_payload = item.input or {}
        query = str(input_payload.get("query") or "")
        kb_ids = [int(value) for value in input_payload.get("kb_ids") or []]
        if not query or not kb_ids:
            continue
        for kb_id in set(kb_ids):
            kb = get_kb(db, task.tenant_id, kb_id)
            assert_access(db, principal, "kb", kb.id)
        relevant = [
            str(value)
            for value in (item.label or {}).get("relevant_chunk_ids")
            or (item.output or {}).get("chunk_ids")
            or []
        ]
        result = retrieve(
            db,
            tenant_id=task.tenant_id,
            kb_ids=kb_ids,
            query=query,
            top_k=top_k,
            filters=config.get("filters") or {},
            kb_version_alias=str(config.get("kb_version", "prod")),
            index_version=config.get("index_version"),
            with_graph=bool(config.get("with_graph", False)),
            with_citation=False,
            principal=principal,
        )
        retrieved = [str(hit["chunk_id"]) for hit in result.get("results", [])]
        metrics = retrieval_metrics(relevant, retrieved, top_k)
        for name, value in metrics.items():
            aggregates.setdefault(name, []).append(float(value))
        samples += 1
    result: dict[str, Any] = {"samples": samples}
    for name, values in aggregates.items():
        result[name] = round(sum(values) / len(values), 6) if values else 0.0
    gates = config.get("gates") or {}
    gate_results = {
        name: result.get(name, 0.0) >= float(threshold)
        for name, threshold in gates.items()
    }
    if gate_results:
        result["gate_passed"] = all(gate_results.values())
        result["gates"] = gate_results
    return result


def _run_text_eval(
    items: list[DatasetItem], config: dict[str, Any]
) -> dict[str, Any]:
    precision_values: list[float] = []
    recall_values: list[float] = []
    f1_values: list[float] = []
    exact_values: list[float] = []
    for item in items:
        output_payload = item.output or {}
        label = item.label or {}
        prediction = str(
            output_payload.get("prediction")
            or output_payload.get("output")
            or output_payload.get("text")
            or output_payload.get("chosen")
            or ""
        )
        reference = str(
            label.get("reference")
            or output_payload.get("reference")
            or ""
        )
        if not reference:
            continue
        overlap = answer_overlap(reference, prediction)
        precision_values.append(overlap["precision"])
        recall_values.append(overlap["recall"])
        f1_values.append(overlap["f1"])
        exact_values.append(1.0 if prediction.strip() == reference.strip() else 0.0)
    total = len(f1_values)
    pass_threshold = float(config.get("pass_threshold", 0.5))
    metrics = {
        "samples": total,
        "precision": round(sum(precision_values) / total, 6) if total else 0.0,
        "recall": round(sum(recall_values) / total, 6) if total else 0.0,
        "f1": round(sum(f1_values) / total, 6) if total else 0.0,
        "exact_match": round(sum(exact_values) / total, 6) if total else 0.0,
        "pass_rate": round(
            sum(1 for value in f1_values if value >= pass_threshold) / total, 6
        )
        if total
        else 0.0,
    }
    gates = config.get("gates") or {}
    gate_results = {
        name: metrics.get(name, 0.0) >= float(threshold)
        for name, threshold in gates.items()
    }
    if gate_results:
        metrics["gate_passed"] = all(gate_results.values())
        metrics["gates"] = gate_results
    return metrics


def get_eval_task(
    db: Session, principal: Principal, eval_id: int
) -> tuple[EvalTask, list[EvalResult]]:
    query = db.query(EvalTask).filter(EvalTask.id == eval_id)
    if (principal.kind in {"app", "user"} and not principal.is_admin):
        query = query.filter(EvalTask.tenant_id == (principal.tenant_id or 0))
    task = query.first()
    if task is None:
        raise ApiError("RESOURCE_NOT_FOUND", "评测任务不存在", 404)
    results = (
        db.query(EvalResult)
        .filter(EvalResult.eval_task_id == task.id)
        .order_by(EvalResult.metric)
        .all()
    )
    return task, results


def list_eval_tasks(
    db: Session, principal: Principal, *, limit: int = 100
) -> list[EvalTask]:
    query = db.query(EvalTask)
    if (principal.kind in {"app", "user"} and not principal.is_admin):
        query = query.filter(EvalTask.tenant_id == (principal.tenant_id or 0))
    return query.order_by(EvalTask.created_at.desc()).limit(min(limit, 500)).all()


def governance_metrics(
    db: Session,
    principal: Principal,
    *,
    tenant_id: int | None = None,
) -> dict[str, Any]:
    scoped_principal = principal.kind in {"app", "user"} and not principal.is_admin
    scope = principal.tenant_id if scoped_principal else tenant_id

    def scoped(query: Any, model: Any) -> Any:
        if scope is not None:
            return query.filter(model.tenant_id == scope)
        return query

    event_total = scoped(db.query(func.count(AiEvent.id)), AiEvent).scalar() or 0
    event_types = (
        scoped(
            db.query(AiEvent.event_type, func.count(AiEvent.id)),
            AiEvent,
        )
        .group_by(AiEvent.event_type)
        .all()
    )
    event_statuses = (
        scoped(
            db.query(AiEvent.status, func.count(AiEvent.id)),
            AiEvent,
        )
        .group_by(AiEvent.status)
        .all()
    )
    feedback_total = scoped(db.query(func.count(Feedback.id)), Feedback).scalar() or 0
    positive_feedback = (
        scoped(
            db.query(func.count(Feedback.id)).filter(Feedback.rating == "POSITIVE"),
            Feedback,
        ).scalar()
        or 0
    )
    bad_case_total = scoped(db.query(func.count(BadCase.id)), BadCase).scalar() or 0
    bad_case_statuses = (
        scoped(db.query(BadCase.status, func.count(BadCase.id)), BadCase)
        .group_by(BadCase.status)
        .all()
    )
    closed_cases = scoped(
        db.query(BadCase).filter(BadCase.status == "CLOSED"), BadCase
    ).all()
    close_hours = [
        (case.closed_at - case.created_at).total_seconds() / 3600
        for case in closed_cases
        if case.closed_at is not None and case.created_at is not None
    ]

    annotation_tasks = scoped(db.query(AnnotationTask), AnnotationTask).all()
    task_ids = [task.id for task in annotation_tasks]
    records = (
        db.query(AnnotationRecord).filter(AnnotationRecord.task_id.in_(task_ids)).all()
        if task_ids
        else []
    )
    agreement = _agreement_metrics(records)
    approved_records = sum(1 for record in records if record.decision == "APPROVED")
    rejected_records = sum(1 for record in records if record.decision == "REJECTED")

    dataset_query = scoped(db.query(Dataset), Dataset)
    datasets = dataset_query.all()
    dataset_ids = [dataset.id for dataset in datasets]
    versions = (
        db.query(DatasetVersion).filter(DatasetVersion.dataset_id.in_(dataset_ids)).all()
        if dataset_ids
        else []
    )
    version_ids = [version.id for version in versions]
    items = (
        db.query(DatasetItem).filter(DatasetItem.dataset_version_id.in_(version_ids)).all()
        if version_ids
        else []
    )
    quality_scores = [
        float(item.quality_score) for item in items if item.quality_score is not None
    ]
    review_statuses: dict[str, int] = {}
    for item in items:
        review_status = str(item.review_status)
        review_statuses[review_status] = review_statuses.get(review_status, 0) + 1

    inference_count = (
        scoped(
            db.query(func.count(AiEvent.id)).filter(
                AiEvent.event_type == "INFERENCE_COMPLETED"
            ),
            AiEvent,
        ).scalar()
        or 0
    )

    return {
        "events": {
            "total": int(event_total),
            "by_type": {row[0]: int(row[1]) for row in event_types},
            "by_status": {row[0]: int(row[1]) for row in event_statuses},
        },
        "feedback": {
            "total": int(feedback_total),
            "positive": int(positive_feedback),
            "positive_rate": round(positive_feedback / feedback_total, 6)
            if feedback_total
            else 0.0,
            "coverage_rate": round(
                min(feedback_total / inference_count, 1.0), 6
            )
            if inference_count
            else 0.0,
        },
        "bad_cases": {
            "total": int(bad_case_total),
            "by_status": {row[0]: int(row[1]) for row in bad_case_statuses},
            "close_rate": round(len(closed_cases) / bad_case_total, 6)
            if bad_case_total
            else 0.0,
            "avg_close_hours": round(sum(close_hours) / len(close_hours), 2)
            if close_hours
            else 0.0,
        },
        "annotations": {
            "tasks": len(annotation_tasks),
            "records": len(records),
            "approval_rate": round(
                approved_records / (approved_records + rejected_records), 6
            )
            if approved_records + rejected_records
            else 0.0,
            **agreement,
        },
        "datasets": {
            "total": len(datasets),
            "versions": len(versions),
            "published_versions": sum(
                1 for version in versions if version.status == "PUBLISHED"
            ),
            "items": len(items),
            "avg_quality_score": round(
                sum(quality_scores) / len(quality_scores), 6
            )
            if quality_scores
            else 0.0,
            "review_status": review_statuses,
        },
    }


def create_retention_job(
    db: Session, principal: Principal, payload: RetentionJobCreate
) -> RetentionJob:
    scoped_principal = principal.kind in {"app", "user"} and not principal.is_admin
    tenant_id = principal.tenant_id if scoped_principal else payload.tenant_id
    if tenant_id is None:
        raise ApiError("INVALID_ARGUMENT", "管理员执行保留任务必须指定 tenant_id", 400)
    settings = get_settings()
    before = _parse_datetime(payload.before)
    if before is None:
        before = utcnow() - timedelta(days=settings.governance_retention_days)
    target = payload.target.upper()
    deleted_count = 0
    if target == "AI_EVENT":
        query = db.query(AiEvent).filter(
            AiEvent.tenant_id == tenant_id, AiEvent.created_at < before
        )
        if payload.event_types:
            query = query.filter(AiEvent.event_type.in_(payload.event_types))
        deleted_count = query.count()
        if not payload.dry_run and deleted_count:
            query.delete(synchronize_session=False)
    elif target == "FEEDBACK":
        query = db.query(Feedback).filter(
            Feedback.tenant_id == tenant_id, Feedback.created_at < before
        )
        deleted_count = query.count()
        if not payload.dry_run and deleted_count:
            query.delete(synchronize_session=False)
    elif target == "BAD_CASE":
        query = db.query(BadCase).filter(
            BadCase.tenant_id == tenant_id,
            BadCase.created_at < before,
            BadCase.status == "CLOSED",
        )
        deleted_count = query.count()
        if not payload.dry_run and deleted_count:
            query.delete(synchronize_session=False)
    else:
        raise ApiError("INVALID_ARGUMENT", "不支持的保留目标", 400, {"target": target})

    job = RetentionJob(
        id=new_id(),
        tenant_id=tenant_id,
        job_type=target,
        target=target,
        policy={
            "before": before.isoformat(),
            "event_types": payload.event_types,
            "dry_run": payload.dry_run,
        },
        status="SUCCEEDED",
        deleted_count=deleted_count,
        detail={"dry_run": payload.dry_run},
        finished_at=utcnow(),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def list_retention_jobs(
    db: Session, principal: Principal, *, limit: int = 100
) -> list[RetentionJob]:
    query = db.query(RetentionJob)
    if (principal.kind in {"app", "user"} and not principal.is_admin):
        query = query.filter(RetentionJob.tenant_id == (principal.tenant_id or 0))
    return query.order_by(RetentionJob.created_at.desc()).limit(min(limit, 500)).all()


def promote_bad_case_to_regression(
    db: Session, principal: Principal, record: BadCase
) -> DatasetItem:
    dataset = (
        db.query(Dataset)
        .filter(
            Dataset.tenant_id == record.tenant_id,
            Dataset.code == "bad-case-regression",
        )
        .first()
    )
    if dataset is None:
        dataset = Dataset(
            id=new_id(),
            tenant_id=record.tenant_id,
            code="bad-case-regression",
            name="Bad Case 回归集",
            type="BAD_CASE",
            description="由已关闭 Bad Case 自动沉淀的回归样本",
            owner="ai-platform",
        )
        db.add(dataset)
        db.flush()
    version = (
        db.query(DatasetVersion)
        .filter(
            DatasetVersion.dataset_id == dataset.id,
            DatasetVersion.status.in_(["DRAFT", "REVIEWING"]),
        )
        .order_by(DatasetVersion.created_at.desc())
        .first()
    )
    if version is None:
        version = DatasetVersion(
            id=new_id(),
            dataset_id=dataset.id,
            version=f"reg-{utcnow().strftime('%Y%m%d%H%M%S')}-{record.id % 10000}",
            status="DRAFT",
            source_snapshot={"source": "bad_case", "bad_case_id": record.id},
            filter_config={"redact": True},
            prelabel_model="rule-based/v1",
            stats={},
            created_by=principal.app_id or 0,
        )
        db.add(version)
        db.flush()
    item = DatasetItem(
        id=new_id(),
        dataset_version_id=version.id,
        item_type="bad_case",
        input={"request_id": record.request_id},
        output={},
        label={
            "bad_case_id": record.id,
            "category": record.category,
            "sub_category": record.sub_category,
            "root_cause": record.root_cause,
            "priority": record.priority,
        },
        extra={
            "sources": [record.request_id],
            "bad_case_id": record.id,
            "pipeline_version": PIPELINE_VERSION,
        },
        source_event_id=None,
        quality_score=1.0,
        review_status="APPROVED",
        split="test",
    )
    db.add(item)
    db.flush()
    record.regression_case_id = item.id
    _add_lineage(
        db,
        record.tenant_id,
        version.id,
        "REGRESSION",
        input_ref={"bad_case_id": record.id},
        output_ref={"item_id": item.id},
        stats={"category": record.category},
        operator_id=principal.app_id or 0,
    )
    db.commit()
    db.refresh(item)
    return item

