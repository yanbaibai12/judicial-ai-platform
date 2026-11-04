from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from sqlalchemy.orm import Session

from ..acl import (
    accessible_document_ids,
    assert_access,
    grant_resource,
    grant_to_dict,
    has_access,
    list_grants,
    revoke_resource,
)
from ..config import get_settings
from ..db import get_db
from ..errors import ApiError
from ..models import (
    ChunkMeta,
    Document,
    DocumentVersion,
    IndexVersion,
    KbVersion,
    KbVersionChunk,
    KnowledgeBase,
    ResourceGrant,
)
from ..schemas import (
    DocumentAccessPolicy,
    DocumentCreate,
    KbAliasCreate,
    KbCreate,
    KbGrantCreate,
    KbVersionCreate,
    RetrievalConfigCreate,
    RetrieveRequest,
)
from ..security import Principal, require_any, require_app, require_permission
from ..services.jobs import enqueue_job, job_to_dict
from ..services.observability import audit, record_event
from ..services.retrieval import (
    add_document,
    add_document_from_file,
    create_kb,
    create_kb_version,
    create_retrieval_config,
    document_to_dict,
    get_kb,
    index_to_dict,
    kb_to_dict,
    retrieve,
    set_alias,
)
from .utils import ok, tenant_scope

router = APIRouter(tags=["retrieval"])


def _kb_for(
    db: Session, principal: Principal, kb_id: int, need: str = "read"
) -> KnowledgeBase:
    scoped = principal.kind in {"app", "user"} and not principal.is_admin
    kb = get_kb(db, principal.tenant_id if scoped else None, kb_id)
    assert_access(db, principal, "kb", kb.id, need)
    return kb


def _kb_payload(db: Session, kb: KnowledgeBase, principal: Principal) -> dict[str, Any]:
    document_ids = accessible_document_ids(db, principal, kb.tenant_id, [int(kb.id)])
    chunk_count = (
        db.query(ChunkMeta)
        .filter(ChunkMeta.kb_id == kb.id, ChunkMeta.document_id.in_(document_ids))
        .count()
    )
    return kb_to_dict(
        kb,
        chunk_count=chunk_count,
        document_count=len(document_ids),
    )


def _document_for(
    db: Session, principal: Principal, document_id: int, *, manage: bool = False
) -> Document:
    query = db.query(Document).filter(Document.id == document_id, Document.deleted_at.is_(None))
    if principal.kind in {"app", "user"} and not principal.is_admin:
        query = query.filter(Document.tenant_id == (principal.tenant_id or 0))
    document = query.first()
    if document is None:
        raise ApiError("RESOURCE_NOT_FOUND", "文档不存在", 404)
    _kb_for(db, principal, document.kb_id, need="owner" if manage else "read")
    if not manage and document.id not in accessible_document_ids(
        db, principal, document.tenant_id, [document.kb_id]
    ):
        raise ApiError("FORBIDDEN", "无权访问该文档", 403)
    return document


def _document_version_info(version: DocumentVersion | None) -> dict[str, Any] | None:
    if version is None:
        return None
    return {
        "id": version.id,
        "version_no": version.version_no,
        "page_count": version.page_count,
        "parse_status": version.parse_status,
        "parse_warning": version.parse_error,
    }


def _auto_index(
    db: Session,
    principal: Principal,
    kb: KnowledgeBase,
    document: Document,
    version_id: int,
) -> int | None:
    """按配置在文档写入后自动入队 kb_index（按文档版本幂等）。"""
    if not get_settings().kb_auto_index_after_upload:
        return None
    job = enqueue_job(
        db,
        "kb_index",
        {"kb_id": kb.id, "document_id": document.id, "trigger": "auto_upload"},
        tenant_id=kb.tenant_id,
        app_id=principal.app_id,
        principal=principal,
        idempotency_key=f"kb_index:auto:{kb.id}:{document.id}:{version_id}",
    )
    return job.id


@router.post("/api/v1/kbs")
def create_knowledge_base(
    payload: KbCreate,
    request: Request,
    principal: Principal = Depends(require_permission("kb:write")),
    db: Session = Depends(get_db),
):
    tenant_id = tenant_scope(principal, payload.tenant_id)
    if tenant_id is None:
        raise ApiError("INVALID_ARGUMENT", "管理员创建知识库必须指定 tenant_id", 400)
    kb = create_kb(db, tenant_id, payload)
    if principal.kind == "app" and principal.app_id is not None:
        grant_resource(db, tenant_id, "kb", kb.id, "app", str(principal.app_id), "owner")
    elif principal.kind == "user" and principal.user_id:
        grant_resource(db, tenant_id, "kb", kb.id, "user", principal.user_id, "owner")
    return ok(request, _kb_payload(db, kb, principal), 201)


@router.get("/api/v1/kbs")
def list_knowledge_bases(
    request: Request,
    tenant_id: int | None = Query(default=None),
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
):
    query = db.query(KnowledgeBase).filter(KnowledgeBase.deleted_at.is_(None))
    scope = tenant_scope(principal, tenant_id)
    if scope is not None:
        query = query.filter(KnowledgeBase.tenant_id == scope)
    items = [
        item
        for item in query.order_by(KnowledgeBase.created_at).all()
        if has_access(db, principal, "kb", item.id)
    ]
    return ok(
        request,
        {"items": [_kb_payload(db, item, principal) for item in items], "total": len(items)},
    )


@router.get("/api/v1/kbs/{kb_id}")
def get_knowledge_base(
    kb_id: int,
    request: Request,
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
):
    kb = _kb_for(db, principal, kb_id)
    return ok(request, _kb_payload(db, kb, principal))


@router.post("/api/v1/kbs/{kb_id}/documents")
def create_document(
    kb_id: int,
    payload: DocumentCreate,
    request: Request,
    principal: Principal = Depends(require_permission("kb:write")),
    db: Session = Depends(get_db),
):
    kb = _kb_for(db, principal, kb_id, need="write")
    document, version, chunk_count, duplicate = add_document(db, kb, payload)
    if duplicate:
        _document_for(db, principal, document.id)
    index_job_id = _auto_index(db, principal, kb, document, version.id)
    return ok(
        request,
        {
            "document": document_to_dict(document),
            "document_version_id": version.id,
            "page_count": version.page_count,
            "parse_status": version.parse_status,
            "parse_warning": version.parse_error,
            "chunk_count": chunk_count,
            "duplicate": duplicate,
            "index_job_id": index_job_id,
        },
        201,
    )


@router.post("/api/v1/kbs/{kb_id}/documents/upload")
async def upload_document(
    kb_id: int,
    request: Request,
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    doc_type: str = Form(default="file"),
    security_level: str = Form(default="INTERNAL"),
    principal: Principal = Depends(require_permission("kb:write")),
    db: Session = Depends(get_db),
):
    kb = _kb_for(db, principal, kb_id, need="write")
    content = await file.read()
    document, version, chunk_count, duplicate = add_document_from_file(
        db,
        kb,
        file.filename or "upload.txt",
        content,
        title=title,
        doc_type=doc_type,
        security_level=security_level,
    )
    if duplicate:
        _document_for(db, principal, document.id)
    index_job_id = _auto_index(db, principal, kb, document, version.id)
    return ok(
        request,
        {
            "document": document_to_dict(document),
            "document_version_id": version.id,
            "page_count": version.page_count,
            "parse_status": version.parse_status,
            "parse_warning": version.parse_error,
            "chunk_count": chunk_count,
            "duplicate": duplicate,
            "index_job_id": index_job_id,
        },
        201,
    )


@router.get("/api/v1/kbs/{kb_id}/documents")
def list_documents(
    kb_id: int,
    request: Request,
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
):
    kb = _kb_for(db, principal, kb_id)
    documents = (
        db.query(Document)
        .filter(Document.kb_id == kb.id, Document.deleted_at.is_(None))
        .order_by(Document.created_at.desc())
        .all()
    )
    allowed_ids = accessible_document_ids(db, principal, kb.tenant_id, [kb.id])
    documents = [document for document in documents if document.id in allowed_ids]
    return ok(
        request,
        {"items": [document_to_dict(item) for item in documents], "total": len(documents)},
    )


@router.post("/api/v1/kbs/{kb_id}/versions")
def create_version(
    kb_id: int,
    payload: KbVersionCreate,
    request: Request,
    principal: Principal = Depends(require_permission("kb:write")),
    db: Session = Depends(get_db),
):
    kb = _kb_for(db, principal, kb_id, need="write")
    version = create_kb_version(db, kb, payload.version, payload.publish)
    return ok(
        request,
        {
            "id": version.id,
            "kb_id": version.kb_id,
            "version": version.version,
            "status": version.status,
            "document_count": version.document_count,
            "chunk_count": version.chunk_count,
            "snapshot_state": version.snapshot_state,
            "published_at": version.published_at.isoformat() if version.published_at else None,
        },
        201,
    )


@router.post("/api/v1/kbs/{kb_id}/index-jobs")
def create_index_job(
    kb_id: int,
    request: Request,
    version: str | None = Query(default=None),
    principal: Principal = Depends(require_permission("kb:write")),
    db: Session = Depends(get_db),
):
    kb = _kb_for(db, principal, kb_id, need="write")
    idempotency_key = request.headers.get("Idempotency-Key")
    job = enqueue_job(
        db,
        "kb_index",
        {"kb_id": kb.id, "version": version},
        tenant_id=kb.tenant_id,
        app_id=principal.app_id,
        principal=principal,
        idempotency_key=f"kb_index:{idempotency_key}" if idempotency_key else None,
    )
    data = job_to_dict(job)
    data["index"] = (job.result or {}).get("index")
    return ok(request, data, 202)


@router.get("/api/v1/kbs/{kb_id}/versions")
def list_versions(
    kb_id: int,
    request: Request,
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
):
    kb = _kb_for(db, principal, kb_id)
    kb_versions = (
        db.query(KbVersion)
        .filter(KbVersion.kb_id == kb.id)
        .order_by(KbVersion.created_at.desc())
        .all()
    )
    index_versions = (
        db.query(IndexVersion)
        .filter(IndexVersion.kb_id == kb.id)
        .order_by(IndexVersion.created_at.desc())
        .all()
    )
    return ok(
        request,
        {
            "kb_versions": [
                {
                    "id": item.id,
                    "version": item.version,
                    "status": item.status,
                    "document_count": item.document_count,
                    "chunk_count": item.chunk_count,
                    "snapshot_state": item.snapshot_state,
                    "created_at": item.created_at.isoformat() if item.created_at else None,
                }
                for item in kb_versions
            ],
            "index_versions": [index_to_dict(item) for item in index_versions],
        },
    )


@router.post("/api/v1/kbs/{kb_id}/aliases")
def update_alias(
    kb_id: int,
    payload: KbAliasCreate,
    request: Request,
    principal: Principal = Depends(require_permission("kb:write")),
    db: Session = Depends(get_db),
):
    kb = _kb_for(db, principal, kb_id, need="write")
    alias = set_alias(
        db,
        kb,
        payload.alias,
        payload.kb_version,
        payload.index_version,
        payload.retrieval_config_id,
    )
    return ok(
        request,
        {
            "alias": alias.alias,
            "kb_version_id": alias.kb_version_id,
            "index_version_id": alias.index_version_id,
            "retrieval_config_id": alias.retrieval_config_id,
        },
    )


@router.post("/api/v1/retrieval-configs")
def create_config(
    payload: RetrievalConfigCreate,
    request: Request,
    principal: Principal = Depends(require_permission("kb:write")),
    db: Session = Depends(get_db),
):
    tenant_id = principal.tenant_id or 0
    record = create_retrieval_config(db, tenant_id, payload.code, payload.version, payload.config)
    return ok(
        request,
        {
            "id": record.id,
            "code": record.code,
            "version": record.version,
            "status": record.status,
        },
        201,
    )


@router.get("/api/v1/documents/{document_id}")
def get_document(
    document_id: int,
    request: Request,
    version: int | None = Query(default=None, ge=1),
    kb_version_id: int | None = Query(default=None, ge=1),
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
):
    document = _document_for(db, principal, document_id)
    version_query = db.query(DocumentVersion).filter(
        DocumentVersion.document_id == document.id
    )
    document_version = (
        version_query.filter(DocumentVersion.id == version).first()
        if version is not None else
        version_query.order_by(DocumentVersion.version_no.desc()).first()
    )
    if version is not None and document_version is None:
        raise ApiError("RESOURCE_NOT_FOUND", "文档版本不存在", 404)
    if kb_version_id is not None:
        if version is None:
            raise ApiError("INVALID_ARGUMENT", "访问知识库快照时必须指定文档版本", 400)
        kb_version = db.query(KbVersion).filter(
            KbVersion.id == kb_version_id, KbVersion.kb_id == document.kb_id
        ).first()
        if kb_version is None or kb_version.snapshot_state != "PARSED":
            raise ApiError("RESOURCE_NOT_FOUND", "知识库快照不存在或不可用", 404)
        snapshots = db.query(KbVersionChunk).filter(
            KbVersionChunk.kb_version_id == kb_version_id,
            KbVersionChunk.payload["document_id"].as_integer() == document.id,
            KbVersionChunk.payload["document_version_id"].as_integer() == version,
        ).all()
        selected = list(snapshots)
        if not selected:
            raise ApiError("RESOURCE_NOT_FOUND", "文档版本不在该知识库快照中", 404)
        selected.sort(key=lambda item: (item.payload.get("extra") or {}).get("chunk_index", 0))
        snapshot_document = selected[0].document or {}
        document_data = {
            "id": document.id,
            "tenant_id": document.tenant_id,
            "kb_id": document.kb_id,
            "title": snapshot_document.get("title"),
            "metadata": snapshot_document.get("metadata"),
            "access_policy": document.access_policy,
        }
        return ok(request, {
            "document": document_data,
            "document_version": _document_version_info(document_version),
            "snapshot": {
                "kb_version_id": kb_version_id,
                "document_version_id": version,
                "document_version_info_source": "CURRENT_RECORD",
            },
            "chunks": [
                {
                    "chunk_id": item.chunk_id,
                    "text": item.payload.get("text"),
                    "metadata": item.payload.get("extra") or {},
                    "page_from": item.payload.get("page_from"),
                    "page_to": item.payload.get("page_to"),
                }
                for item in selected
            ],
        })
    query = db.query(ChunkMeta).filter(ChunkMeta.document_id == document.id)
    if version is not None:
        query = query.filter(ChunkMeta.document_version_id == version)
    chunks = query.order_by(ChunkMeta.created_at).all()
    return ok(
        request,
        {
            "document": document_to_dict(document),
            "document_version": _document_version_info(document_version),
            "chunks": [
                {
                    "chunk_id": chunk.chunk_id,
                    "text": chunk.text,
                    "metadata": chunk.extra or {},
                    "page_from": chunk.page_from,
                    "page_to": chunk.page_to,
                }
                for chunk in chunks
            ],
        },
    )


@router.post("/v1/retrieve")
def retrieve_api(
    payload: RetrieveRequest,
    request: Request,
    principal: Principal = Depends(require_app),
    db: Session = Depends(get_db),
):
    for kb_id in set(payload.kb_ids):
        _kb_for(db, principal, kb_id)
    result = retrieve(
        db,
        tenant_id=principal.tenant_id or 0,
        kb_ids=payload.kb_ids,
        query=payload.query,
        top_k=payload.top_k,
        filters=payload.filters,
        kb_version_alias=payload.kb_version,
        index_version=payload.index_version,
        with_graph=payload.with_graph,
        with_citation=payload.with_citation,
        compress=payload.compress,
        context_budget_chars=payload.context_budget_chars,
        principal=principal,
    )
    request_id = getattr(request.state, "request_id", "")
    result["request_id"] = request_id
    results = result.get("results") or []
    event_payload: dict[str, Any] = {
        "query": payload.query,
        "kb_ids": payload.kb_ids,
        "chunk_ids": [
            item.get("chunk_id") for item in results if item.get("chunk_id")
        ],
        "citations": [
            item["citation"] for item in results if isinstance(item.get("citation"), dict)
        ],
        "result_count": len(results),
        "versions": result.get("versions") or {},
        "security_level": "INTERNAL",
    }
    if isinstance(result.get("decision"), dict):
        event_payload["decision"] = result["decision"]
    if isinstance(result.get("compression"), dict):
        event_payload["compression"] = result["compression"]
    try:
        record_event(
            db,
            "RETRIEVAL_COMPLETED",
            tenant_id=principal.tenant_id or 0,
            app_id=principal.app_id,
            request_id=request_id,
            trace_id=request_id,
            payload=event_payload,
            object_ref=f"retrieval:{request_id}",
        )
    except Exception:
        pass
    return result


@router.put("/api/v1/documents/{document_id}/access-policy")
def update_document_access_policy(
    document_id: int,
    payload: DocumentAccessPolicy,
    request: Request,
    principal: Principal = Depends(require_permission("kb:write")),
    db: Session = Depends(get_db),
):
    document = _document_for(db, principal, document_id, manage=True)
    previous_policy = document.access_policy
    if document.access_policy == "RESTRICTED" and payload.access_policy == "INHERIT":
        db.query(ResourceGrant).filter(
            ResourceGrant.resource_type == "document",
            ResourceGrant.resource_id == document.id,
            ResourceGrant.status == "ACTIVE",
        ).update({"status": "REVOKED"}, synchronize_session=False)
    document.access_policy = payload.access_policy
    audit(
        db, principal, "document.access_policy.update", "document", document.id,
        before={"access_policy": previous_policy}, after={"access_policy": document.access_policy},
        tenant_id=document.tenant_id,
    )
    return ok(request, {"document_id": document.id, "access_policy": document.access_policy})


@router.get("/api/v1/documents/{document_id}/grants")
def list_document_grants(
    document_id: int,
    request: Request,
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
):
    document = _document_for(db, principal, document_id, manage=True)
    grants = list_grants(db, "document", document.id)
    return ok(request, {"items": [grant_to_dict(item) for item in grants], "total": len(grants)})


@router.post("/api/v1/documents/{document_id}/grants")
def create_document_grant(
    document_id: int,
    payload: KbGrantCreate,
    request: Request,
    principal: Principal = Depends(require_permission("kb:write")),
    db: Session = Depends(get_db),
):
    document = _document_for(db, principal, document_id, manage=True)
    if document.access_policy != "RESTRICTED":
        raise ApiError("RESOURCE_CONFLICT", "请先将文档设为受限访问", 409)
    grant = grant_resource(
        db, document.tenant_id, "document", document.id,
        payload.subject_kind, payload.subject_id, payload.level,
        granted_by=principal.username or principal.app_code or "system",
        expires_at=payload.expires_at,
        commit=False,
    )
    audit(
        db, principal, "document.grant.create", "document", document.id,
        after=grant_to_dict(grant), tenant_id=document.tenant_id,
    )
    return ok(request, grant_to_dict(grant), 201)


@router.delete("/api/v1/documents/{document_id}/grants")
def delete_document_grant(
    document_id: int,
    request: Request,
    subject_kind: str = Query(...),
    subject_id: str = Query(...),
    principal: Principal = Depends(require_permission("kb:write")),
    db: Session = Depends(get_db),
):
    document = _document_for(db, principal, document_id, manage=True)
    if not revoke_resource(
        db, "document", document.id, subject_kind, subject_id, commit=False
    ):
        raise ApiError("RESOURCE_NOT_FOUND", "授权不存在", 404)
    audit(
        db, principal, "document.grant.revoke", "document", document.id,
        after={"subject_kind": subject_kind, "subject_id": subject_id},
        tenant_id=document.tenant_id,
    )
    return ok(request, {"revoked": True})


@router.get("/api/v1/kbs/{kb_id}/grants")
def list_kb_grants(
    kb_id: int,
    request: Request,
    principal: Principal = Depends(require_any),
    db: Session = Depends(get_db),
):
    kb = _kb_for(db, principal, kb_id, need="owner")
    grants = list_grants(db, "kb", kb.id)
    return ok(
        request,
        {"items": [grant_to_dict(item) for item in grants], "total": len(grants)},
    )


@router.post("/api/v1/kbs/{kb_id}/grants")
def create_kb_grant(
    kb_id: int,
    payload: KbGrantCreate,
    request: Request,
    principal: Principal = Depends(require_permission("kb:write")),
    db: Session = Depends(get_db),
):
    kb = _kb_for(db, principal, kb_id, need="owner")
    grant = grant_resource(
        db,
        tenant_id=kb.tenant_id,
        resource_type="kb",
        resource_id=kb.id,
        subject_kind=payload.subject_kind,
        subject_id=payload.subject_id,
        level=payload.level,
        granted_by=principal.username or principal.app_code or "system",
        expires_at=payload.expires_at,
        commit=False,
    )
    audit(
        db, principal, "kb.grant.create", "kb", kb.id,
        after=grant_to_dict(grant), tenant_id=kb.tenant_id,
    )
    return ok(request, grant_to_dict(grant), 201)


@router.delete("/api/v1/kbs/{kb_id}/grants")
def delete_kb_grant(
    kb_id: int,
    request: Request,
    subject_kind: str = Query(...),
    subject_id: str = Query(...),
    principal: Principal = Depends(require_permission("kb:write")),
    db: Session = Depends(get_db),
):
    kb = _kb_for(db, principal, kb_id, need="owner")
    revoked = revoke_resource(db, "kb", kb.id, subject_kind, subject_id, commit=False)
    if not revoked:
        raise ApiError("RESOURCE_NOT_FOUND", "授权不存在", 404)
    audit(
        db, principal, "kb.grant.revoke", "kb", kb.id,
        after={"subject_kind": subject_kind, "subject_id": subject_id},
        tenant_id=kb.tenant_id,
    )
    return ok(request, {"revoked": True})



