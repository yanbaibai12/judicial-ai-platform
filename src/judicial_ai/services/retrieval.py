from __future__ import annotations

import io
import logging
import mimetypes
import time
from typing import Any

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from ..acl import accessible_document_ids, assert_access
from ..config import get_settings
from ..errors import ApiError
from ..ids import new_id
from ..models import (
    ChunkMeta,
    Document,
    DocumentVersion,
    IndexChunkSnapshot,
    IndexVersion,
    KbAlias,
    KbVersion,
    KbVersionChunk,
    KnowledgeBase,
    RetrievalConfig,
    utcnow,
)
from ..schemas import DocumentCreate, KbCreate
from ..security import Principal
from ..text_utils import (
    chunk_text,
    content_hash,
    cosine,
    keyword_score,
    normalize_text,
)
from .context_compression import compress_results
from .decision import RetrievalPlan, plan_query
from .graph_store import get_graph_store, graph_search_or_degraded
from .inference import get_inference_client
from .object_store import get_object_store, sha256_bytes
from .vector_store import VectorHit, get_vector_store

logger = logging.getLogger("judicial_ai.retrieval")


def _validate_embeddings(chunks: list[Any], embeddings: list[list[float]]) -> None:
    if len(chunks) != len(embeddings):
        raise ApiError("UPSTREAM_UNAVAILABLE", "向量数量与文本块数量不一致", 503)
    if embeddings and (not embeddings[0] or any(
        len(embedding) != len(embeddings[0]) for embedding in embeddings
    )):
        raise ApiError("UPSTREAM_UNAVAILABLE", "向量为空或维度不一致", 503)


def cleanup_index_artifacts(
    index_version_id: int, stores: tuple[str, ...] = ("vector", "graph")
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    errors: list[str] = []
    for name in stores:
        try:
            store = get_vector_store() if name == "vector" else get_graph_store()
            store.delete_index(index_version_id)
            results[name] = "deleted"
        except Exception as exc:  # noqa: BLE001
            message = f"{name}: {str(exc)[:240]}"
            errors.append(message)
            results[name] = {"status": "error", "error": str(exc)[:240]}
    return {"status": "FAILED" if errors else "COMPLETED", "stores": results, "errors": errors}


def _fail_index(
    db: Session,
    index_version: IndexVersion,
    stats: dict[str, Any],
    error: str,
    *,
    cleanup: bool,
) -> None:
    cleanup_result = cleanup_index_artifacts(index_version.id) if cleanup else None
    index_version.status = "FAILED"
    index_version.cleanup_status = (
        cleanup_result["status"] if cleanup_result else "NOT_REQUIRED"
    )
    index_version.stats = {
        **stats,
        "error": error[:500],
        **({"cleanup": cleanup_result} if cleanup_result else {}),
    }
    db.commit()


def create_kb(db: Session, tenant_id: int, payload: KbCreate) -> KnowledgeBase:
    existing = (
        db.query(KnowledgeBase)
        .filter(
            KnowledgeBase.tenant_id == tenant_id,
            KnowledgeBase.code == payload.code,
            KnowledgeBase.deleted_at.is_(None),
        )
        .first()
    )
    if existing:
        raise ApiError("RESOURCE_CONFLICT", "知识库编码已存在", 409, {"code": payload.code})

    settings = get_settings()
    kb = KnowledgeBase(
        id=new_id(),
        tenant_id=tenant_id,
        code=payload.code,
        name=payload.name,
        domain=payload.domain,
        description=payload.description,
        security_level=payload.security_level,
        embedding_model=payload.embedding_model or settings.embedding_model,
        chunk_config=payload.chunk_config or {"max_chars": 1000, "overlap": 100},
        status="ACTIVE",
        owner=payload.owner,
    )
    db.add(kb)
    db.commit()
    db.refresh(kb)
    return kb


def get_kb(db: Session, tenant_id: int | None, kb_id: int) -> KnowledgeBase:
    query = db.query(KnowledgeBase).filter(
        KnowledgeBase.id == kb_id, KnowledgeBase.deleted_at.is_(None)
    )
    if tenant_id is not None:
        query = query.filter(KnowledgeBase.tenant_id == tenant_id)
    kb = query.first()
    if kb is None:
        raise ApiError("RESOURCE_NOT_FOUND", "知识库不存在", 404, {"kb_id": kb_id})
    return kb


def add_document(
    db: Session,
    kb: KnowledgeBase,
    payload: DocumentCreate,
    *,
    pages: list[str] | None = None,
    raw_content: bytes | None = None,
    original_filename: str | None = None,
    content_type: str | None = None,
) -> tuple[Document, DocumentVersion, int, bool]:
    source_content = raw_content if raw_content is not None else payload.text.encode("utf-8")
    digest = sha256_bytes(source_content)
    filename = original_filename or "document.txt"
    media_type = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    existing = (
        db.query(Document)
        .filter(
            Document.kb_id == kb.id,
            Document.content_hash == digest,
            Document.status == "ACTIVE",
            Document.deleted_at.is_(None),
        )
        .first()
    )
    if existing:
        version = (
            db.query(DocumentVersion)
            .filter(DocumentVersion.document_id == existing.id)
            .order_by(DocumentVersion.version_no.desc())
            .first()
        )
        count = (
            db.query(func.count(ChunkMeta.chunk_id))
            .filter(ChunkMeta.document_id == existing.id)
            .scalar()
            or 0
        )
        if version is None:
            raise ApiError("INTERNAL_ERROR", "文档版本缺失", 500)
        store = get_object_store()
        if (
            version.storage_status != "AVAILABLE"
            or not version.object_key
            or not store.exists(version.object_key)
        ):
            object_key = (
                f"tenant-{kb.tenant_id}/kb-{kb.id}/document-{existing.id}/"
                f"version-{version.id}/{digest}"
            )
            info = store.put(object_key, source_content, media_type)
            if info.sha256 != digest:
                store.delete(object_key)
                raise ApiError("OBJECT_INTEGRITY_FAILED", "原件写入校验失败", 503)
            version.object_key = object_key
            version.content_hash = digest
            version.original_filename = filename
            version.content_type = media_type
            version.object_size = len(source_content)
            version.storage_status = "AVAILABLE"
            existing.content_hash = digest
            db.commit()
        return existing, version, int(count), True

    document_id = new_id()
    version_id = new_id()
    object_key = (
        f"tenant-{kb.tenant_id}/kb-{kb.id}/document-{document_id}/"
        f"version-{version_id}/{digest}"
    )
    document = Document(
        id=document_id,
        tenant_id=kb.tenant_id,
        kb_id=kb.id,
        title=payload.title,
        doc_type=payload.doc_type,
        source=payload.source,
        source_ref=payload.source_ref,
        content_hash=digest,
        security_level=payload.security_level,
        extra=payload.metadata,
        status="ACTIVE",
    )

    missing_pages = [index for index, page in enumerate(pages or [], 1) if not page.strip()]
    version = DocumentVersion(
        id=version_id,
        document_id=document.id,
        version_no=1,
        object_key=object_key,
        content_hash=digest,
        original_filename=filename,
        content_type=media_type,
        object_size=len(source_content),
        storage_status="AVAILABLE",
        page_count=len(pages) if pages is not None else None,
        parse_status="PARTIAL" if missing_pages else "PARSED",
        parse_error=(
            "部分页面未提取到文本，可能为空白页或扫描件，需人工确认"
            if missing_pages else None
        ),
    )

    chunk_config = kb.chunk_config or {}
    max_chars = int(chunk_config.get("max_chars", 1000))
    overlap = int(chunk_config.get("overlap", 100))
    if pages is None:
        chunk_pages = [(chunk, None) for chunk in chunk_text(payload.text, max_chars, overlap)]
    else:
        chunk_pages = [
            (chunk, page_number)
            for page_number, text in enumerate(pages, 1)
            for chunk in chunk_text(text, max_chars, overlap)
        ]
    chunks = [chunk for chunk, _ in chunk_pages]
    if not chunks:
        raise ApiError("INVALID_ARGUMENT", "文档内容为空", 400)

    embeddings = get_inference_client().embed(kb.embedding_model, chunks)
    _validate_embeddings(chunks, embeddings)
    chunk_records = [
        ChunkMeta(
                chunk_id=f"chk_{new_id()}",
                tenant_id=kb.tenant_id,
                kb_id=kb.id,
                document_id=document.id,
                document_version_id=version.id,
                chunk_type="paragraph",
                section_path=None,
                page_from=page,
                page_to=page,
                token_count=max(1, len(chunk) // 2),
                text_hash=content_hash(chunk),
                text=chunk,
                extra={
                    **payload.metadata,
                    "doc_type": payload.doc_type,
                    "security_level": payload.security_level,
                    "chunk_index": index,
                    **({"pages_without_text": missing_pages} if missing_pages else {}),
                },
                embedding=embedding,
            )
        for index, ((chunk, page), embedding) in enumerate(
            zip(chunk_pages, embeddings, strict=True)
        )
    ]
    store = get_object_store()
    info = store.put(object_key, source_content, media_type)
    if info.sha256 != digest:
        store.delete(object_key)
        raise ApiError("OBJECT_INTEGRITY_FAILED", "原件写入校验失败", 503)
    try:
        db.add(document)
        db.add(version)
        db.add_all(chunk_records)
        db.commit()
    except Exception:
        db.rollback()
        try:
            store.delete(object_key)
        except Exception:  # noqa: BLE001
            logger.exception("document_object_rollback_failed", extra={"object_key": object_key})
        raise
    db.refresh(document)
    db.refresh(version)
    return document, version, len(chunks), False


def add_document_from_file(
    db: Session,
    kb: KnowledgeBase,
    filename: str,
    content: bytes,
    title: str | None = None,
    doc_type: str = "file",
    security_level: str = "INTERNAL",
) -> tuple[Document, DocumentVersion, int, bool]:
    text, _page_count, pages = _parse_file(filename, content)
    if not text.strip():
        raise ApiError("INVALID_ARGUMENT", "文件解析结果为空", 400)
    payload = DocumentCreate(
        title=title or filename,
        text=text,
        doc_type=doc_type,
        source="upload",
        source_ref=filename,
        security_level=security_level,
    )
    return add_document(
        db,
        kb,
        payload,
        pages=pages,
        raw_content=content,
        original_filename=filename,
        content_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
    )


def parse_file(filename: str, content: bytes) -> tuple[str, int | None]:
    text, page_count, _pages = _parse_file(filename, content)
    return text, page_count


def _parse_file(filename: str, content: bytes) -> tuple[str, int | None, list[str] | None]:
    lower = filename.lower()
    if lower.endswith(".pdf"):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n\n".join(pages), len(pages), pages
    if lower.endswith(".docx"):
        from docx import Document as DocxDocument

        document = DocxDocument(io.BytesIO(content))
        parts = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
        for table in document.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))
        return "\n\n".join(parts), None, None
    return content.decode("utf-8", errors="ignore"), None, None


def create_kb_version(
    db: Session, kb: KnowledgeBase, version: str, publish: bool = True
) -> KbVersion:
    existing = (
        db.query(KbVersion).filter(KbVersion.kb_id == kb.id, KbVersion.version == version).first()
    )
    if existing:
        raise ApiError("RESOURCE_CONFLICT", "知识库版本已存在", 409, {"version": version})

    snapshot_rows = (
        db.query(Document, ChunkMeta)
        .outerjoin(ChunkMeta, ChunkMeta.document_id == Document.id)
        .filter(Document.kb_id == kb.id, Document.status == "ACTIVE", Document.deleted_at.is_(None))
        .all()
    )
    documents = {document.id: document for document, _ in snapshot_rows}
    chunks = [(document, chunk) for document, chunk in snapshot_rows if chunk is not None]

    record = KbVersion(
        id=new_id(),
        kb_id=kb.id,
        version=version,
        status="PUBLISHED" if publish else "DRAFT",
        parse_config={"parser": "local", "embedding_model": kb.embedding_model},
        chunk_config=kb.chunk_config or {},
        document_count=len(documents),
        chunk_count=len(chunks),
        snapshot_state="PARSED",
        published_at=utcnow() if publish else None,
    )
    db.add(record)
    for document, chunk in chunks:
        db.add(KbVersionChunk(
            kb_version_id=record.id,
            chunk_id=chunk.chunk_id,
            payload={
                "chunk_id": chunk.chunk_id,
                "tenant_id": chunk.tenant_id,
                "kb_id": chunk.kb_id,
                "document_id": chunk.document_id,
                "document_version_id": chunk.document_version_id,
                "chunk_type": chunk.chunk_type,
                "section_path": chunk.section_path,
                "page_from": chunk.page_from,
                "page_to": chunk.page_to,
                "token_count": chunk.token_count,
                "text_hash": chunk.text_hash,
                "text": chunk.text,
                "extra": chunk.extra or {},
            },
            document={"title": document.title, "metadata": document.extra or {}},
        ))
    db.commit()
    db.refresh(record)
    return record


def index_kb(db: Session, kb: KnowledgeBase, version: str | None = None) -> IndexVersion:
    settings = get_settings()
    kb_version = None
    if version:
        kb_version = (
            db.query(KbVersion)
            .filter(KbVersion.kb_id == kb.id, KbVersion.version == version)
            .first()
        )
        if kb_version is None:
            raise ApiError("VERSION_NOT_FOUND", "知识库版本不存在", 404, {"version": version})
    if kb_version is None:
        kb_version = create_kb_version(
            db, kb, f"kb-{new_id()}", publish=not settings.index_require_approval
        )
    if kb_version.snapshot_state != "PARSED":
        raise ApiError(
            "RESOURCE_CONFLICT", "历史知识库版本缺少内容快照，无法可靠重建索引", 409
        )
    snapshot_chunks = (
        db.query(KbVersionChunk).filter(KbVersionChunk.kb_version_id == kb_version.id).all()
    )
    if len(snapshot_chunks) != kb_version.chunk_count:
        raise ApiError("RESOURCE_CONFLICT", "知识库版本快照不完整，无法重建索引", 409)
    embedding_model = (kb_version.parse_config or {}).get("embedding_model", kb.embedding_model)

    index_version = IndexVersion(
        id=new_id(),
        kb_id=kb.id,
        kb_version_id=kb_version.id,
        version=f"idx-{utcnow().strftime('%Y%m%d%H%M%S')}-{new_id() % 10000}",
        milvus_collection=f"kb_{kb.id}_chunks",
        milvus_partition=f"tenant_{kb.tenant_id}",
        embedding_model=embedding_model,
        status="BUILDING",
    )
    db.add(index_version)
    db.commit()
    db.refresh(index_version)

    chunks = [ChunkMeta(**snapshot.payload) for snapshot in snapshot_chunks]
    batch_size = 64
    all_embeddings: list[list[float]] = []
    try:
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            embeddings = get_inference_client().embed(
                embedding_model, [chunk.text for chunk in batch]
            )
            _validate_embeddings(batch, embeddings)
            all_embeddings.extend(embeddings)
        _validate_embeddings(chunks, all_embeddings)
    except Exception:
        db.rollback()
        _fail_index(
            db,
            index_version,
            {"chunk_count": len(chunks), "vector_backend": settings.vector_backend},
            "embedding generation failed",
            cleanup=False,
        )
        raise

    stats: dict[str, Any] = {
        "chunk_count": len(chunks),
        "embedding_dim": len(all_embeddings[0]) if all_embeddings else settings.embedding_dim,
        "vector_backend": settings.vector_backend,
    }
    try:
        vector_info = get_vector_store().index_chunks(
            kb=kb,
            index_version=index_version,
            chunks=chunks,
            embeddings=all_embeddings,
        )
        stats["vector"] = vector_info
        if vector_info.get("collection"):
            index_version.milvus_collection = str(vector_info["collection"])
    except Exception as exc:  # noqa: BLE001 - optional vector backend can degrade to SQL
        if settings.vector_backend != "sql" and not settings.vector_fallback_sql:
            _fail_index(db, index_version, stats, str(exc), cleanup=True)
            raise
        cleanup_result = cleanup_index_artifacts(index_version.id, ("vector",))
        logger.warning("vector_index_degraded", extra={"error": str(exc)[:240]})
        stats["vector"] = {
            "backend": settings.vector_backend,
            "degraded": True,
            "cleanup": cleanup_result,
        }

    if settings.graph_enabled:
        try:
            graph_info = get_graph_store().index_chunks(
                kb=kb, index_version=index_version, chunks=chunks
            )
            stats["graph"] = graph_info
            index_version.neo4j_snapshot = f"{settings.graph_backend}:{index_version.id}"
        except Exception as exc:  # noqa: BLE001 - graph is optional
            cleanup_result = cleanup_index_artifacts(index_version.id, ("graph",))
            logger.warning("graph_index_degraded", extra={"error": str(exc)[:240]})
            stats["graph"] = {
                "backend": settings.graph_backend,
                "degraded": True,
                "cleanup": cleanup_result,
            }

    try:
        for chunk, embedding in zip(chunks, all_embeddings, strict=True):
            db.add(IndexChunkSnapshot(
                index_version_id=index_version.id, chunk_id=chunk.chunk_id, embedding=embedding
            ))
            live_chunk = db.get(ChunkMeta, chunk.chunk_id)
            if live_chunk is not None:
                live_chunk.embedding = embedding
                live_chunk.index_version_id = index_version.id
        index_version.status = (
            "PENDING_APPROVAL" if settings.index_require_approval else "READY"
        )
        if not settings.index_require_approval:
            kb_version.status = "PUBLISHED"
            kb_version.published_at = kb_version.published_at or utcnow()
        index_version.published_at = (
            None if settings.index_require_approval else utcnow()
        )
        index_version.stats = stats
        db.commit()
    except Exception:
        db.rollback()
        _fail_index(
            db,
            index_version,
            stats,
            "index publication failed",
            cleanup=True,
        )
        raise
    db.refresh(index_version)
    return index_version


def set_alias(
    db: Session,
    kb: KnowledgeBase,
    alias: str,
    kb_version: str,
    index_version: str | None = None,
    retrieval_config_id: int | None = None,
) -> KbAlias:
    target_kb_version = (
        db.query(KbVersion)
        .filter(KbVersion.kb_id == kb.id, KbVersion.version == kb_version)
        .first()
    )
    if target_kb_version is None:
        raise ApiError("VERSION_NOT_FOUND", "知识库版本不存在", 404, {"version": kb_version})
    if alias == "prod" and target_kb_version.status != "PUBLISHED":
        raise ApiError("RESOURCE_CONFLICT", "生产别名只能指向已发布知识版本", 409)

    target_index_version = None
    if index_version:
        target_index_version = (
            db.query(IndexVersion)
            .filter(IndexVersion.kb_id == kb.id, IndexVersion.version == index_version)
            .first()
        )
        if target_index_version is None:
            raise ApiError("VERSION_NOT_FOUND", "索引版本不存在", 404, {"version": index_version})

    record = db.query(KbAlias).filter(KbAlias.kb_id == kb.id, KbAlias.alias == alias).first()
    if target_index_version is None and record and record.index_version_id:
        target_index_version = db.get(IndexVersion, record.index_version_id)
    if target_index_version and (
        target_index_version.status != "READY"
        or target_index_version.kb_id != kb.id
        or target_index_version.kb_version_id != target_kb_version.id
    ):
        raise ApiError("RESOURCE_CONFLICT", "索引尚未就绪或不属于指定知识库版本", 409)
    if record is None:
        record = KbAlias(id=new_id(), kb_id=kb.id, alias=alias)
        db.add(record)
    record.kb_version_id = target_kb_version.id
    record.index_version_id = target_index_version.id if target_index_version else None
    record.retrieval_config_id = retrieval_config_id or record.retrieval_config_id
    record.updated_at = utcnow()
    db.commit()
    db.refresh(record)
    return record


def approve_index(
    db: Session, kb: KnowledgeBase, index_version_id: int, approved_by: str
) -> IndexVersion:
    index = db.query(IndexVersion).filter(
        IndexVersion.id == index_version_id, IndexVersion.kb_id == kb.id
    ).first()
    if index is None:
        raise ApiError("VERSION_NOT_FOUND", "索引版本不存在", 404)
    if index.status != "PENDING_APPROVAL":
        raise ApiError("RESOURCE_CONFLICT", "只有待审批索引可以批准", 409)
    version = db.get(KbVersion, index.kb_version_id)
    if version is None:
        raise ApiError("VERSION_NOT_FOUND", "知识版本不存在", 404)
    version.status = "PUBLISHED"
    version.published_at = version.published_at or utcnow()
    index.status = "READY"
    index.approved_by = approved_by
    index.approved_at = utcnow()
    index.rejection_reason = None
    index.published_at = utcnow()
    db.commit()
    db.refresh(index)
    return index


def reject_index(
    db: Session,
    kb: KnowledgeBase,
    index_version_id: int,
    rejected_by: str,
    reason: str,
) -> IndexVersion:
    index = db.query(IndexVersion).filter(
        IndexVersion.id == index_version_id, IndexVersion.kb_id == kb.id
    ).first()
    if index is None:
        raise ApiError("VERSION_NOT_FOUND", "索引版本不存在", 404)
    if index.status != "PENDING_APPROVAL":
        raise ApiError("RESOURCE_CONFLICT", "只有待审批索引可以拒绝", 409)
    cleanup = cleanup_index_artifacts(index.id)
    index.status = "REJECTED"
    index.approved_by = rejected_by
    index.approved_at = utcnow()
    index.rejection_reason = reason
    index.cleanup_status = cleanup["status"]
    index.stats = {**(index.stats or {}), "cleanup": cleanup}
    db.commit()
    db.refresh(index)
    return index


def reconcile_index_artifacts(
    db: Session, *, kb_id: int | None = None, repair: bool = False
) -> dict[str, Any]:
    settings = get_settings()
    query = db.query(IndexVersion)
    if kb_id is not None:
        query = query.filter(IndexVersion.kb_id == kb_id)
    indexes = query.all()
    known_ids = {int(item.id) for item in indexes}
    backend_errors: dict[str, str] = {}
    vector_ids: set[int] = set()
    graph_ids: set[int] = set()
    if settings.vector_backend == "milvus":
        try:
            vector_ids = get_vector_store().list_index_ids()
        except Exception as exc:  # noqa: BLE001
            backend_errors["vector"] = str(exc)[:240]
    if settings.graph_enabled:
        try:
            graph_ids = get_graph_store().list_index_ids()
        except Exception as exc:  # noqa: BLE001
            backend_errors["graph"] = str(exc)[:240]
    issues: list[dict[str, Any]] = []

    for index in indexes:
        stats = index.stats or {}
        expected_chunks = int(stats.get("chunk_count") or 0)
        snapshot_count = db.query(func.count(IndexChunkSnapshot.chunk_id)).filter(
            IndexChunkSnapshot.index_version_id == index.id
        ).scalar() or 0
        vector = stats.get("vector") or {}
        graph = stats.get("graph") or {}
        expected_vector = (
            index.status in {"READY", "PENDING_APPROVAL"}
            and expected_chunks > 0
            and vector.get("backend") == "milvus"
            and not vector.get("degraded")
        )
        expected_graph = (
            index.status in {"READY", "PENDING_APPROVAL"}
            and graph.get("backend") == "neo4j"
            and not graph.get("degraded")
            and int(graph.get("entities") or 0) > 0
        )
        reasons: list[str] = []
        if index.status in {"READY", "PENDING_APPROVAL"} and snapshot_count != expected_chunks:
            reasons.append("snapshot_count_mismatch")
        if expected_vector and index.id not in vector_ids:
            reasons.append("vector_artifact_missing")
        if expected_graph and index.id not in graph_ids:
            reasons.append("graph_artifact_missing")
        if index.status in {"FAILED", "REJECTED", "STALE"} and (
            index.id in vector_ids or index.id in graph_ids
        ):
            reasons.append("failed_index_artifacts_present")
        if not reasons:
            continue
        issue = {
            "index_version_id": index.id,
            "kb_id": index.kb_id,
            "status": index.status,
            "reasons": reasons,
            "snapshot_count": int(snapshot_count),
            "expected_chunks": expected_chunks,
            "repaired": False,
        }
        if repair:
            cleanup = cleanup_index_artifacts(index.id)
            index.cleanup_status = cleanup["status"]
            index.stats = {**stats, "cleanup": cleanup, "reconcile_reasons": reasons}
            if index.status in {"READY", "PENDING_APPROVAL"}:
                index.status = "FAILED"
            db.query(KbAlias).filter(KbAlias.index_version_id == index.id).update(
                {"index_version_id": None}, synchronize_session=False
            )
            issue["cleanup"] = cleanup
            issue["repaired"] = cleanup["status"] == "COMPLETED"
        issues.append(issue)

    orphan_vector = sorted(vector_ids - known_ids)
    orphan_graph = sorted(graph_ids - known_ids)
    orphan_repairs: list[dict[str, Any]] = []
    if repair:
        for index_id in sorted(set(orphan_vector) | set(orphan_graph)):
            stores: list[str] = []
            if index_id in orphan_vector:
                stores.append("vector")
            if index_id in orphan_graph:
                stores.append("graph")
            cleanup = cleanup_index_artifacts(index_id, tuple(stores))
            orphan_repairs.append(
                {"index_version_id": index_id, "stores": stores, "cleanup": cleanup}
            )
        db.commit()
    return {
        "checked": len(indexes),
        "issues": issues,
        "orphan_vector_index_ids": orphan_vector,
        "orphan_graph_index_ids": orphan_graph,
        "orphan_repairs": orphan_repairs,
        "backend_errors": backend_errors,
        "repair": repair,
    }


def recover_index(
    db: Session, kb: KnowledgeBase, index_version_id: int
) -> tuple[IndexVersion, IndexVersion]:
    previous = db.query(IndexVersion).filter(
        IndexVersion.id == index_version_id, IndexVersion.kb_id == kb.id
    ).first()
    if previous is None:
        raise ApiError("VERSION_NOT_FOUND", "索引版本不存在", 404)
    version = db.get(KbVersion, previous.kb_version_id)
    if version is None or version.snapshot_state != "PARSED":
        raise ApiError("RESOURCE_CONFLICT", "索引对应的知识快照不可重建", 409)
    cleanup = cleanup_index_artifacts(previous.id)
    previous.status = "SUPERSEDED"
    previous.cleanup_status = cleanup["status"]
    previous.stats = {**(previous.stats or {}), "cleanup": cleanup}
    db.query(KbAlias).filter(KbAlias.index_version_id == previous.id).update(
        {"index_version_id": None}, synchronize_session=False
    )
    db.commit()
    replacement = index_kb(db, kb, version.version)
    return previous, replacement


def resolve_versions(
    db: Session, kb: KnowledgeBase, kb_version_alias: str, index_version: str | None
) -> tuple[KbVersion, IndexVersion | None]:
    alias = (
        db.query(KbAlias).filter(KbAlias.kb_id == kb.id, KbAlias.alias == kb_version_alias).first()
    )
    if alias:
        kb_version = db.get(KbVersion, alias.kb_version_id)
        index = db.get(IndexVersion, alias.index_version_id) if alias.index_version_id else None
    else:
        kb_version = (
            db.query(KbVersion)
            .filter(KbVersion.kb_id == kb.id, KbVersion.version == kb_version_alias)
            .first()
        )
        if kb_version is None and kb_version_alias == "prod":
            if get_settings().require_prod_alias:
                raise ApiError("VERSION_NOT_FOUND", "生产别名尚未配置", 404, {"alias": "prod"})
            ready_index = (
                db.query(IndexVersion)
                .join(KbVersion, KbVersion.id == IndexVersion.kb_version_id)
                .filter(
                    IndexVersion.kb_id == kb.id,
                    IndexVersion.status == "READY",
                    KbVersion.status == "PUBLISHED",
                )
                .order_by(IndexVersion.published_at.desc(), IndexVersion.created_at.desc())
                .first()
            )
            kb_version = db.get(KbVersion, ready_index.kb_version_id) if ready_index else None
        index = None

    if kb_version is None:
        raise ApiError("VERSION_NOT_FOUND", "知识库版本不存在", 404, {"alias": kb_version_alias})
    if index_version:
        index = (
            db.query(IndexVersion)
            .filter(IndexVersion.kb_id == kb.id, IndexVersion.version == index_version)
            .first()
        )
        if index is None:
            raise ApiError("VERSION_NOT_FOUND", "索引版本不存在", 404, {"version": index_version})
    elif not alias:
        index = (
            db.query(IndexVersion)
            .filter(
                IndexVersion.kb_id == kb.id,
                IndexVersion.kb_version_id == kb_version.id,
                IndexVersion.status == "READY",
            )
            .order_by(IndexVersion.created_at.desc())
            .first()
        )
    if index and (
        index.kb_id != kb.id or index.kb_version_id != kb_version.id or index.status != "READY"
    ):
        raise ApiError("RESOURCE_CONFLICT", "索引尚未就绪或不属于指定知识库版本", 409)
    return kb_version, index


def _match_filters(chunk: ChunkMeta, filters: dict[str, Any]) -> bool:
    metadata = chunk.extra or {}
    for key, expected in filters.items():
        if key == "security_level":
            actual = metadata.get("security_level", "")
        elif key == "doc_type":
            actual = metadata.get("doc_type", "")
        else:
            actual = metadata.get(key)
        if isinstance(expected, list):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


def _graph_payload(
    *,
    db: Session,
    tenant_id: int,
    kb_ids: list[int],
    index_ids: list[int],
    document_ids: set[int],
    query: str,
    with_graph: bool,
) -> dict[str, Any]:
    settings = get_settings()
    if not with_graph:
        return {
            "entities": [],
            "paths": [],
            "degraded": not settings.graph_enabled,
            "backend": settings.graph_backend,
        }
    indexes = db.query(IndexVersion).filter(IndexVersion.id.in_(index_ids)).all()
    graph_index_ids = [
        item.id for item in indexes
        if (item.stats or {}).get("graph")
        and not (item.stats or {})["graph"].get("degraded")
    ]
    missing_graph = len(graph_index_ids) != len(index_ids)
    if not graph_index_ids:
        return {
            "entities": [],
            "paths": [],
            "degraded": True,
            "backend": settings.graph_backend,
        }
    result = graph_search_or_degraded(
        tenant_id=tenant_id,
        kb_ids=kb_ids,
        index_version_ids=graph_index_ids,
        query=query,
        limit=20,
    )
    graph_chunk_ids = {
        item.get("chunk_id") for item in result.entities + result.paths if item.get("chunk_id")
    }
    allowed_chunks = {
        row.chunk_id
        for row in db.query(ChunkMeta.chunk_id)
        .join(IndexChunkSnapshot, IndexChunkSnapshot.chunk_id == ChunkMeta.chunk_id)
        .filter(
            ChunkMeta.tenant_id == tenant_id,
            ChunkMeta.kb_id.in_(kb_ids),
            ChunkMeta.document_id.in_(document_ids),
            ChunkMeta.chunk_id.in_(graph_chunk_ids),
            IndexChunkSnapshot.index_version_id.in_(index_ids),
        )
        .all()
    } if graph_chunk_ids else set()
    return {
        "entities": [item for item in result.entities if item.get("chunk_id") in allowed_chunks],
        "paths": [item for item in result.paths if item.get("chunk_id") in allowed_chunks],
        "degraded": result.degraded or missing_graph,
        "backend": result.backend,
    }


def retrieve(
    db: Session,
    tenant_id: int,
    kb_ids: list[int],
    query: str,
    top_k: int = 10,
    filters: dict[str, Any] | None = None,
    kb_version_alias: str = "prod",
    index_version: str | None = None,
    with_graph: bool = False,
    with_citation: bool = True,
    compress: bool = False,
    context_budget_chars: int | None = None,
    principal: Principal | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    started = time.perf_counter()
    filters = filters or {}

    kbs = (
        db.query(KnowledgeBase)
        .filter(
            KnowledgeBase.id.in_(kb_ids),
            KnowledgeBase.tenant_id == tenant_id,
            KnowledgeBase.deleted_at.is_(None),
        )
        .all()
    )
    if len(kbs) != len(set(kb_ids)):
        raise ApiError("RESOURCE_NOT_FOUND", "知识库不存在或无权访问", 404)
    if settings.resource_acl_enforced:
        if principal is None:
            raise ApiError("FORBIDDEN", "检索缺少授权主体", 403)
        for kb in kbs:
            assert_access(db, principal, "kb", kb.id)
    effective_principal = principal or Principal(kind="admin")
    document_ids = accessible_document_ids(db, effective_principal, tenant_id, kb_ids)

    plan: RetrievalPlan | None = None
    try:
        plan = plan_query(query, default_top_k=top_k)
        if context_budget_chars is not None:
            plan.context_budget_chars = min(
                context_budget_chars, settings.rag_compression_max_chars
            )
    except Exception:
        plan = None

    version_info: dict[str, Any] = {}
    index_ids: list[int] = []
    vector_index_ids: list[int] = []
    index_models: set[str] = set()
    degraded_vector_index = False
    for kb in kbs:
        kb_version, index_version_record = resolve_versions(db, kb, kb_version_alias, index_version)
        version_info[str(kb.id)] = {
            "kb": kb_version.version,
            "index": index_version_record.version if index_version_record else None,
        }
        if index_version_record:
            index_ids.append(index_version_record.id)
            # Empty indexes intentionally do not create a Milvus collection.
            if (index_version_record.stats or {}).get("chunk_count") != 0:
                vector_index_ids.append(index_version_record.id)
            index_models.add(index_version_record.embedding_model)
            vector_stats = (index_version_record.stats or {}).get("vector") or {}
            degraded_vector_index = degraded_vector_index or bool(vector_stats.get("degraded"))

    recall_started = time.perf_counter()
    if len(index_models) > 1:
        raise ApiError("INVALID_ARGUMENT", "跨知识库检索需要使用相同的向量模型", 400)
    embedding_model = next(
        iter(index_models), kbs[0].embedding_model if kbs else settings.embedding_model
    )
    query_embeddings = get_inference_client().embed(embedding_model, [query])
    _validate_embeddings([query], query_embeddings)
    query_embedding = query_embeddings[0]
    scored: list[dict[str, Any]] = []
    recall_mode = "sql-hybrid"
    if degraded_vector_index and settings.vector_backend == "milvus":
        if not settings.vector_fallback_sql:
            raise ApiError("DEPENDENCY_UNAVAILABLE", "索引的向量数据不可用", 503)
        recall_mode = "sql-fallback"
    use_sql = settings.vector_backend != "milvus" or not index_ids or degraded_vector_index
    security_levels = filters.get("security_level")
    if isinstance(security_levels, str):
        security_levels = [security_levels]

    if settings.vector_backend == "milvus" and vector_index_ids and not degraded_vector_index:
        recall_mode = "milvus-hybrid"
        try:
            hits: list[VectorHit] = get_vector_store().search(
                tenant_id=tenant_id,
                kb_ids=[kb.id for kb in kbs],
                index_version_ids=vector_index_ids,
                query_text=query,
                query_embedding=query_embedding,
                limit=max(top_k * 5, 50),
                security_levels=security_levels,
            )
            if hits:
                hit_map = {hit.chunk_id: hit for hit in hits}
                candidates = (
                    db.query(ChunkMeta, KbVersionChunk)
                    .join(IndexChunkSnapshot, IndexChunkSnapshot.chunk_id == ChunkMeta.chunk_id)
                    .join(IndexVersion, IndexVersion.id == IndexChunkSnapshot.index_version_id)
                    .outerjoin(
                        KbVersionChunk,
                        and_(
                            KbVersionChunk.kb_version_id == IndexVersion.kb_version_id,
                            KbVersionChunk.chunk_id == ChunkMeta.chunk_id,
                        ),
                    )
                    .filter(
                        ChunkMeta.tenant_id == tenant_id,
                        ChunkMeta.kb_id.in_([kb.id for kb in kbs]),
                        ChunkMeta.document_id.in_(document_ids),
                        IndexChunkSnapshot.index_version_id.in_(index_ids),
                        ChunkMeta.chunk_id.in_(list(hit_map)),
                    )
                    .all()
                )
                candidate_chunks = [
                    (ChunkMeta(**snapshot.payload) if snapshot else chunk, snapshot)
                    for chunk, snapshot in candidates
                ]
                scored = [
                    {
                        "chunk": chunk,
                        "snapshot_document": snapshot.document if snapshot else None,
                        "snapshot_kb_version_id": snapshot.kb_version_id if snapshot else None,
                        "dense": hit_map[chunk.chunk_id].score,
                        "keyword": 0.0,
                        "rrf": hit_map[chunk.chunk_id].score,
                        "recall_sources": hit_map[chunk.chunk_id].recall_sources,
                    }
                    for chunk, snapshot in candidate_chunks
                    if _match_filters(chunk, filters)
                ]
                recall_mode = "milvus-hybrid"
        except Exception as exc:  # noqa: BLE001 - vector backend can degrade to SQL
            if not settings.vector_fallback_sql:
                raise ApiError(
                    "DEPENDENCY_UNAVAILABLE",
                    "向量检索服务不可用",
                    503,
                    {"dependency": "milvus", "error": str(exc)[:240]},
                ) from exc
            logger.warning("vector_search_degraded", extra={"error": str(exc)[:240]})
            recall_mode = "sql-fallback"
            use_sql = True

    if use_sql and not scored:
        chunk_query = db.query(ChunkMeta, IndexChunkSnapshot.embedding, KbVersionChunk).join(
            IndexChunkSnapshot, IndexChunkSnapshot.chunk_id == ChunkMeta.chunk_id
        ).join(
            IndexVersion, IndexVersion.id == IndexChunkSnapshot.index_version_id
        ).outerjoin(
            KbVersionChunk,
            and_(
                KbVersionChunk.kb_version_id == IndexVersion.kb_version_id,
                KbVersionChunk.chunk_id == ChunkMeta.chunk_id,
            ),
        ).filter(
            ChunkMeta.tenant_id == tenant_id,
            ChunkMeta.kb_id.in_([kb.id for kb in kbs]),
            ChunkMeta.document_id.in_(document_ids),
            IndexChunkSnapshot.index_version_id.in_(index_ids),
        )
        chunks_with_embeddings = chunk_query.all() if index_ids else []
        for live_chunk, embedding, snapshot in chunks_with_embeddings:
            chunk = ChunkMeta(**snapshot.payload) if snapshot else live_chunk
            if not _match_filters(chunk, filters):
                continue
            dense = cosine(query_embedding, embedding or [])
            keyword = keyword_score(query, chunk.text)
            scored.append({
                "chunk": chunk, "dense": dense, "keyword": keyword,
                "snapshot_document": snapshot.document if snapshot else None,
                "snapshot_kb_version_id": snapshot.kb_version_id if snapshot else None,
            })

        dense_sorted = sorted(scored, key=lambda item: item["dense"], reverse=True)
        keyword_sorted = sorted(scored, key=lambda item: item["keyword"], reverse=True)
        dense_rank = {
            item["chunk"].chunk_id: rank for rank, item in enumerate(dense_sorted, start=1)
        }
        keyword_rank = {
            item["chunk"].chunk_id: rank for rank, item in enumerate(keyword_sorted, start=1)
        }
        for item in scored:
            chunk_id = item["chunk"].chunk_id
            item["rrf"] = 1 / (60 + dense_rank[chunk_id]) + 1 / (
                60 + keyword_rank[chunk_id]
            )
            item["recall_sources"] = []
            if dense_rank[chunk_id] <= max(top_k * 3, 20):
                item["recall_sources"].append("dense")
            if keyword_rank[chunk_id] <= max(top_k * 3, 20):
                item["recall_sources"].append("keyword")

    if not scored:
        empty_response: dict[str, Any] = {
            "query": query,
            "results": [],
            "graph": _graph_payload(
                db=db,
                tenant_id=tenant_id,
                kb_ids=[kb.id for kb in kbs],
                index_ids=index_ids,
                document_ids=document_ids,
                query=query,
                with_graph=with_graph,
            ),
            "versions": version_info,
            "usage": {
                "total_ms": int((time.perf_counter() - started) * 1000),
                "recall_mode": recall_mode,
            },
        }
        if plan is not None:
            empty_response["decision"] = plan.to_dict()
        if compress:
            empty_response["compression"] = {"enabled": False, "reason": "no_results"}
        return empty_response

    recall_ms = int((time.perf_counter() - recall_started) * 1000)
    rerank_started = time.perf_counter()
    candidates = sorted(scored, key=lambda item: item["rrf"], reverse=True)[:50]
    rerank_model = settings.rag_rerank_model
    rerank_results = get_inference_client().rerank(
        rerank_model, query, [item["chunk"].text for item in candidates]
    )
    rerank_scores = {
        candidates[result["index"]]["chunk"].chunk_id: float(result.get("relevance_score", 0.0))
        for result in rerank_results
        if 0 <= int(result.get("index", -1)) < len(candidates)
    }
    max_rrf = max((item["rrf"] for item in candidates), default=1.0) or 1.0
    max_rerank = max(rerank_scores.values(), default=1.0) or 1.0
    for item in candidates:
        rerank_score = rerank_scores.get(item["chunk"].chunk_id, 0.0)
        item["final_score"] = 0.6 * (item["rrf"] / max_rrf) + 0.4 * (rerank_score / max_rerank)
        item["rerank_score"] = rerank_score
    ranked = sorted(candidates, key=lambda item: item["final_score"], reverse=True)[:top_k]
    rerank_ms = int((time.perf_counter() - rerank_started) * 1000)

    ranked_document_ids = {item["chunk"].document_id for item in ranked}
    documents = (
        {
            document.id: document
            for document in db.query(Document).filter(Document.id.in_(ranked_document_ids)).all()
        }
        if ranked_document_ids
        else {}
    )

    results: list[dict[str, Any]] = []
    for item in ranked:
        chunk: ChunkMeta = item["chunk"]
        document = documents.get(chunk.document_id)
        citation = None
        if with_citation and document:
            snapshot_document = item.get("snapshot_document") or {}
            snapshot_kb_version_id = item.get("snapshot_kb_version_id")
            document_metadata = snapshot_document.get("metadata", document.extra or {})
            citation = {
                "document_id": document.id,
                "document_version_id": chunk.document_version_id,
                "title": snapshot_document.get("title", document.title),
                "doc_no": document_metadata.get("doc_no"),
                "page": chunk.page_from,
                "section_path": chunk.section_path,
                "chunk_id": chunk.chunk_id,
                "snippet": chunk.text[:300],
                "source_url": (
                    f"/api/v1/documents/{document.id}?version={chunk.document_version_id}"
                    f"&kb_version_id={snapshot_kb_version_id}"
                    if snapshot_kb_version_id is not None else None
                ),
                "source_status": (
                    "SNAPSHOT_AVAILABLE" if snapshot_kb_version_id is not None
                    else "LEGACY_SNAPSHOT_UNAVAILABLE"
                ),
                "score": round(item["final_score"], 6),
            }
        results.append(
            {
                "chunk_id": chunk.chunk_id,
                "doc_id": chunk.document_id,
                "doc_version_id": chunk.document_version_id,
                "text": chunk.text,
                "score": round(item["final_score"], 6),
                "rerank_score": round(item["rerank_score"], 6),
                "recall_sources": item["recall_sources"],
                "metadata": chunk.extra or {},
                "citation": citation,
            }
        )

    compression: dict[str, Any] | None = None
    if compress:
        if settings.rag_compression_enabled and plan is not None:
            results, compression = compress_results(
                results,
                query,
                plan,
                total_budget_chars=plan.context_budget_chars,
            )
        else:
            compression = {"enabled": False, "reason": "disabled"}

    total_ms = int((time.perf_counter() - started) * 1000)
    response: dict[str, Any] = {
        "query": normalize_text(query),
        "results": results,
        "graph": _graph_payload(
            db=db,
            tenant_id=tenant_id,
            kb_ids=[kb.id for kb in kbs],
            index_ids=index_ids,
            document_ids=document_ids,
            query=query,
            with_graph=with_graph,
        ),
        "versions": version_info,
        "usage": {
            "recall_ms": recall_ms,
            "rerank_ms": rerank_ms,
            "total_ms": total_ms,
            "recall_mode": recall_mode,
        },
    }
    if plan is not None:
        response["decision"] = plan.to_dict()
    if compression is not None:
        response["compression"] = compression
    return response


def kb_to_dict(
    kb: KnowledgeBase,
    *,
    chunk_count: int | None = None,
    document_count: int | None = None,
) -> dict[str, Any]:
    return {
        "id": kb.id,
        "tenant_id": kb.tenant_id,
        "code": kb.code,
        "name": kb.name,
        "domain": kb.domain,
        "description": kb.description,
        "security_level": kb.security_level,
        "embedding_model": kb.embedding_model,
        "chunk_config": kb.chunk_config or {},
        "status": kb.status,
        "owner": kb.owner,
        "chunk_count": chunk_count,
        "document_count": document_count,
        "created_at": kb.created_at.isoformat() if kb.created_at else None,
    }


def document_to_dict(document: Document) -> dict[str, Any]:
    return {
        "id": document.id,
        "tenant_id": document.tenant_id,
        "kb_id": document.kb_id,
        "title": document.title,
        "doc_type": document.doc_type,
        "source": document.source,
        "source_ref": document.source_ref,
        "content_hash": document.content_hash,
        "security_level": document.security_level,
        "access_policy": document.access_policy,
        "metadata": document.extra or {},
        "status": document.status,
        "created_at": document.created_at.isoformat() if document.created_at else None,
    }


def index_to_dict(index: IndexVersion) -> dict[str, Any]:
    return {
        "id": index.id,
        "kb_id": index.kb_id,
        "kb_version_id": index.kb_version_id,
        "version": index.version,
        "milvus_collection": index.milvus_collection,
        "embedding_model": index.embedding_model,
        "status": index.status,
        "stats": index.stats or {},
        "approved_by": index.approved_by,
        "approved_at": index.approved_at.isoformat() if index.approved_at else None,
        "rejection_reason": index.rejection_reason,
        "cleanup_status": index.cleanup_status,
        "published_at": index.published_at.isoformat() if index.published_at else None,
    }


def create_retrieval_config(
    db: Session, tenant_id: int, code: str, version: str, config: dict[str, Any]
) -> RetrievalConfig:
    existing = (
        db.query(RetrievalConfig)
        .filter(
            RetrievalConfig.tenant_id == tenant_id,
            RetrievalConfig.code == code,
            RetrievalConfig.version == version,
        )
        .first()
    )
    if existing:
        raise ApiError("RESOURCE_CONFLICT", "检索配置版本已存在", 409)
    record = RetrievalConfig(
        id=new_id(),
        tenant_id=tenant_id,
        code=code,
        version=version,
        config=config,
        status="PUBLISHED",
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record

