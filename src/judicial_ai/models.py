from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)

from .db import Base
from .ids import new_id


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Tenant(Base):
    __tablename__ = "tenant"

    id = Column(BigInteger, primary_key=True)
    code = Column(String(64), nullable=False, unique=True)
    name = Column(String(128), nullable=False)
    type = Column(String(32), nullable=False, default="COMPANY")
    status = Column(String(16), nullable=False, default="ACTIVE")
    contact = Column(String(128))
    extra = Column("metadata", JSON, default=dict)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at = Column(DateTime)


class App(Base):
    __tablename__ = "app"

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(BigInteger, nullable=False, index=True)
    code = Column(String(64), nullable=False)
    name = Column(String(128), nullable=False)
    type = Column(String(32), nullable=False, default="SYSTEM")
    owner = Column(String(64), nullable=False, default="")
    status = Column(String(16), nullable=False, default="ACTIVE")
    description = Column(String(512))
    extra = Column("metadata", JSON, default=dict)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at = Column(DateTime)

    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uk_app_tenant_code"),)


class AppCredential(Base):
    __tablename__ = "app_credential"

    id = Column(BigInteger, primary_key=True)
    app_id = Column(BigInteger, nullable=False, index=True)
    credential_type = Column(String(16), nullable=False, default="API_KEY")
    key_hash = Column(String(128), nullable=False, unique=True)
    key_prefix = Column(String(16), nullable=False)
    status = Column(String(16), nullable=False, default="ACTIVE")
    expires_at = Column(DateTime)
    last_used_at = Column(DateTime)
    created_by = Column(BigInteger, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class Capability(Base):
    __tablename__ = "capability"

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(BigInteger, nullable=False, default=0)
    code = Column(String(128), nullable=False)
    name = Column(String(128), nullable=False)
    type = Column(String(32), nullable=False)
    scope = Column(String(16), nullable=False, default="GLOBAL")
    owner = Column(String(64), nullable=False)
    description = Column(String(1024))
    status = Column(String(16), nullable=False, default="DRAFT")
    tags = Column(JSON, default=list)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at = Column(DateTime)

    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uk_capability_tenant_code"),
        Index("idx_capability_type_status", "type", "status"),
    )


class CapabilityVersion(Base):
    __tablename__ = "capability_version"

    id = Column(BigInteger, primary_key=True)
    capability_id = Column(BigInteger, nullable=False, index=True)
    version = Column(String(32), nullable=False)
    status = Column(String(16), nullable=False, default="DRAFT")
    protocol = Column(String(16), nullable=False, default="PLATFORM")
    endpoint = Column(String(512))
    model_deployment_id = Column(BigInteger)
    model_name = Column(String(128))
    adapter_alias = Column(String(128))
    request_schema = Column(JSON, nullable=False, default=dict)
    response_schema = Column(JSON, nullable=False, default=dict)
    default_timeout_ms = Column(Integer, nullable=False, default=60000)
    max_timeout_ms = Column(Integer, nullable=False, default=120000)
    max_input_tokens = Column(Integer)
    max_output_tokens = Column(Integer)
    qps_limit = Column(Integer)
    concurrency_limit = Column(Integer)
    retry_policy = Column(JSON)
    health_check = Column(JSON)
    security_level = Column(String(16), nullable=False, default="INTERNAL")
    is_default = Column(Boolean, nullable=False, default=False)
    published_at = Column(DateTime)
    created_by = Column(BigInteger, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("capability_id", "version", name="uk_cap_version"),
        Index("idx_cap_version_status", "capability_id", "status"),
    )


class CapabilityGrant(Base):
    __tablename__ = "capability_grant"

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(BigInteger, nullable=False)
    app_id = Column(BigInteger, nullable=False, index=True)
    capability_id = Column(BigInteger, nullable=False)
    capability_version_id = Column(BigInteger)
    env = Column(String(16), nullable=False, default="prod")
    quota = Column(JSON)
    data_scope = Column(JSON)
    status = Column(String(16), nullable=False, default="ACTIVE")
    expires_at = Column(DateTime)
    created_by = Column(BigInteger, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "app_id",
            "capability_id",
            "capability_version_id",
            "env",
            name="uk_grant",
        ),
        Index("idx_grant_app", "app_id", "status"),
    )


class ModelDeployment(Base):
    __tablename__ = "model_deployment"

    id = Column(BigInteger, primary_key=True)
    code = Column(String(64), nullable=False, unique=True)
    name = Column(String(128), nullable=False)
    engine = Column(String(32), nullable=False, default="VLLM")
    base_model = Column(String(128), nullable=False)
    base_model_version = Column(String(64), nullable=False)
    endpoint = Column(String(512), nullable=False)
    gpu_pool = Column(String(64))
    replicas = Column(Integer, nullable=False, default=1)
    max_concurrency = Column(Integer)
    max_model_len = Column(Integer)
    status = Column(String(16), nullable=False, default="ACTIVE")
    health_status = Column(String(16), nullable=False, default="UNKNOWN")
    extra = Column("metadata", JSON)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class LoraAdapter(Base):
    __tablename__ = "lora_adapter"

    id = Column(BigInteger, primary_key=True)
    alias = Column(String(128), nullable=False)
    base_model = Column(String(128), nullable=False)
    base_model_version = Column(String(64), nullable=False)
    path = Column(String(512), nullable=False)
    rank = Column(Integer, nullable=False, default=16)
    target_modules = Column(JSON)
    training_dataset_version = Column(String(64))
    eval_report_id = Column(BigInteger)
    checksum = Column(String(128))
    status = Column(String(16), nullable=False, default="REGISTERED")
    owner = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("alias", "base_model_version", name="uk_adapter_alias_base"),
    )


class UsageRecord(Base):
    __tablename__ = "usage_record"

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(BigInteger, nullable=False, index=True)
    app_id = Column(BigInteger, nullable=False, index=True)
    capability_id = Column(BigInteger, nullable=False)
    capability_version_id = Column(BigInteger, nullable=False)
    request_id = Column(String(64), nullable=False)
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    total_tokens = Column(Integer, nullable=False, default=0)
    request_count = Column(Integer, nullable=False, default=1)
    latency_ms = Column(Integer, nullable=False, default=0)
    status = Column(String(16), nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(BigInteger, nullable=False, default=0)
    operator_id = Column(BigInteger, nullable=False, default=0)
    operator_type = Column(String(16), nullable=False, default="SYSTEM")
    action = Column(String(64), nullable=False)
    resource_type = Column(String(64), nullable=False)
    resource_id = Column(String(128))
    before = Column(JSON)
    after = Column(JSON)
    request_id = Column(String(64), index=True)
    trace_id = Column(String(64), index=True)
    ip = Column(String(64))
    user_agent = Column(String(512))
    result = Column(String(16), nullable=False, default="SUCCESS")
    created_at = Column(DateTime, nullable=False, default=utcnow)


class AiEvent(Base):
    __tablename__ = "ai_event"

    id = Column(BigInteger, primary_key=True)
    event_id = Column(String(64), nullable=False, unique=True)
    event_type = Column(String(64), nullable=False, index=True)
    tenant_id = Column(BigInteger, nullable=False, index=True)
    app_id = Column(BigInteger)
    request_id = Column(String(64))
    trace_id = Column(String(64))
    payload = Column(JSON)
    object_ref = Column(String(512))
    status = Column(String(16), nullable=False, default="NEW")
    created_at = Column(DateTime, nullable=False, default=utcnow)


class Feedback(Base):
    __tablename__ = "feedback"

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(BigInteger, nullable=False, index=True)
    app_id = Column(BigInteger, nullable=False)
    request_id = Column(String(64), nullable=False, index=True)
    rating = Column(String(16), nullable=False)
    reason = Column(String(64))
    comment = Column(String(2048))
    corrected_output_ref = Column(String(512))
    tags = Column(JSON)
    created_by = Column(BigInteger)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class BadCase(Base):
    __tablename__ = "bad_case"

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(BigInteger, nullable=False, index=True)
    request_id = Column(String(64))
    category = Column(String(32), nullable=False)
    sub_category = Column(String(64))
    root_cause = Column(String(1024))
    priority = Column(String(8), nullable=False, default="P2")
    status = Column(String(16), nullable=False, default="OPEN")
    owner = Column(String(64))
    fix_ref = Column(String(512))
    regression_case_id = Column(BigInteger)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    closed_at = Column(DateTime)


class Dataset(Base):
    __tablename__ = "dataset"

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(BigInteger, nullable=False)
    code = Column(String(64), nullable=False)
    name = Column(String(128), nullable=False)
    type = Column(String(32), nullable=False)
    description = Column(String(1024))
    status = Column(String(16), nullable=False, default="ACTIVE")
    owner = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uk_dataset_code"),)


class DatasetVersion(Base):
    __tablename__ = "dataset_version"

    id = Column(BigInteger, primary_key=True)
    dataset_id = Column(BigInteger, nullable=False, index=True)
    version = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False, default="DRAFT")
    source_snapshot = Column(JSON)
    filter_config = Column(JSON)
    prelabel_model = Column(String(128))
    stats = Column(JSON)
    code_commit = Column(String(64))
    created_by = Column(BigInteger, nullable=False, default=0)
    approved_by = Column(BigInteger)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    published_at = Column(DateTime)

    __table_args__ = (UniqueConstraint("dataset_id", "version", name="uk_dataset_version"),)


class DatasetItem(Base):
    __tablename__ = "dataset_item"

    id = Column(BigInteger, primary_key=True)
    dataset_version_id = Column(BigInteger, nullable=False, index=True)
    item_type = Column(String(32), nullable=False)
    input = Column(JSON, nullable=False)
    output = Column(JSON)
    label = Column(JSON)
    extra = Column("metadata", JSON)
    source_event_id = Column(String(64))
    quality_score = Column(Numeric(5, 4))
    review_status = Column(String(16), nullable=False, default="PENDING")
    split = Column(String(16), nullable=False, default="train")
    created_at = Column(DateTime, nullable=False, default=utcnow)


class KnowledgeBase(Base):
    __tablename__ = "knowledge_base"

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(BigInteger, nullable=False)
    code = Column(String(64), nullable=False)
    name = Column(String(128), nullable=False)
    domain = Column(String(64))
    description = Column(String(1024))
    security_level = Column(String(16), nullable=False, default="INTERNAL")
    embedding_model = Column(String(128), nullable=False)
    chunk_config = Column(JSON)
    status = Column(String(16), nullable=False, default="ACTIVE")
    owner = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at = Column(DateTime)

    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uk_kb_tenant_code"),)


class KbVersion(Base):
    __tablename__ = "kb_version"

    id = Column(BigInteger, primary_key=True)
    kb_id = Column(BigInteger, nullable=False, index=True)
    version = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False, default="DRAFT")
    parse_config = Column(JSON)
    chunk_config = Column(JSON)
    document_count = Column(Integer, nullable=False, default=0)
    chunk_count = Column(BigInteger, nullable=False, default=0)
    snapshot_state = Column(String(16), nullable=False, default="LEGACY", server_default="LEGACY")
    created_by = Column(BigInteger, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    published_at = Column(DateTime)

    __table_args__ = (UniqueConstraint("kb_id", "version", name="uk_kb_version"),)


class KbVersionChunk(Base):
    __tablename__ = "kb_version_chunk"

    kb_version_id = Column(BigInteger, primary_key=True)
    chunk_id = Column(String(64), primary_key=True)
    payload = Column(JSON, nullable=False)
    document = Column(JSON, nullable=False)


class KbAlias(Base):
    __tablename__ = "kb_alias"

    id = Column(BigInteger, primary_key=True)
    kb_id = Column(BigInteger, nullable=False, index=True)
    alias = Column(String(32), nullable=False)
    kb_version_id = Column(BigInteger, nullable=False)
    index_version_id = Column(BigInteger)
    retrieval_config_id = Column(BigInteger)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (UniqueConstraint("kb_id", "alias", name="uk_kb_alias"),)


class Document(Base):
    __tablename__ = "document"

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(BigInteger, nullable=False)
    kb_id = Column(BigInteger, nullable=False, index=True)
    title = Column(String(512), nullable=False)
    doc_type = Column(String(32), nullable=False, default="text")
    source = Column(String(64))
    source_ref = Column(String(512))
    content_hash = Column(String(128), nullable=False, index=True)
    security_level = Column(String(16), nullable=False, default="INTERNAL")
    access_policy = Column(String(16), nullable=False, default="INHERIT", server_default="INHERIT")
    extra = Column("metadata", JSON)
    status = Column(String(16), nullable=False, default="ACTIVE")
    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    deleted_at = Column(DateTime)


class DocumentVersion(Base):
    __tablename__ = "document_version"

    id = Column(BigInteger, primary_key=True)
    document_id = Column(BigInteger, nullable=False, index=True)
    version_no = Column(Integer, nullable=False, default=1)
    object_key = Column(String(512), nullable=False, default="")
    content_hash = Column(String(128), nullable=False)
    page_count = Column(Integer)
    parse_status = Column(String(16), nullable=False, default="PARSED")
    parse_error = Column(String(1024))
    created_at = Column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (UniqueConstraint("document_id", "version_no", name="uk_doc_version"),)


class ChunkMeta(Base):
    __tablename__ = "chunk_meta"

    chunk_id = Column(String(64), primary_key=True)
    tenant_id = Column(BigInteger, nullable=False)
    kb_id = Column(BigInteger, nullable=False, index=True)
    document_id = Column(BigInteger, nullable=False, index=True)
    document_version_id = Column(BigInteger, nullable=False)
    index_version_id = Column(BigInteger)
    chunk_type = Column(String(32), nullable=False, default="paragraph")
    section_path = Column(String(512))
    page_from = Column(Integer)
    page_to = Column(Integer)
    token_count = Column(Integer)
    text_hash = Column(String(128), nullable=False)
    text = Column(Text, nullable=False)
    extra = Column("metadata", JSON)
    embedding = Column(JSON)
    created_at = Column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (Index("idx_chunk_kb_version", "kb_id", "index_version_id"),)


class IndexVersion(Base):
    __tablename__ = "index_version"

    id = Column(BigInteger, primary_key=True)
    kb_id = Column(BigInteger, nullable=False, index=True)
    kb_version_id = Column(BigInteger, nullable=False)
    version = Column(String(64), nullable=False)
    milvus_collection = Column(String(128), nullable=False, default="kb_chunks")
    milvus_partition = Column(String(128))
    neo4j_snapshot = Column(String(128))
    embedding_model = Column(String(128), nullable=False)
    status = Column(String(16), nullable=False, default="BUILDING")
    stats = Column(JSON)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    published_at = Column(DateTime)

    __table_args__ = (UniqueConstraint("kb_id", "version", name="uk_index_version"),)


class IndexChunkSnapshot(Base):
    __tablename__ = "index_chunk_snapshot"

    index_version_id = Column(BigInteger, primary_key=True)
    chunk_id = Column(String(64), primary_key=True)
    embedding = Column(JSON)

    __table_args__ = (Index("idx_index_chunk_snapshot_chunk", "chunk_id"),)


class RetrievalConfig(Base):
    __tablename__ = "retrieval_config"

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(BigInteger, nullable=False)
    code = Column(String(64), nullable=False)
    version = Column(String(32), nullable=False)
    config = Column(JSON, nullable=False)
    status = Column(String(16), nullable=False, default="DRAFT")
    created_by = Column(BigInteger, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (UniqueConstraint("tenant_id", "code", "version", name="uk_retrieval_config"),)


class McpServer(Base):
    __tablename__ = "mcp_server"

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(BigInteger, nullable=False, default=0)
    code = Column(String(64), nullable=False)
    name = Column(String(128), nullable=False)
    transport = Column(String(16), nullable=False, default="HTTP")
    endpoint = Column(String(512))
    auth_type = Column(String(32))
    secret_ref = Column(String(256))
    status = Column(String(16), nullable=False, default="ACTIVE")
    owner = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uk_mcp_server"),)


class McpTool(Base):
    __tablename__ = "mcp_tool"

    id = Column(BigInteger, primary_key=True)
    server_id = Column(BigInteger, nullable=False, index=True)
    name = Column(String(128), nullable=False)
    description = Column(String(1024))
    input_schema = Column(JSON, nullable=False, default=dict)
    output_schema = Column(JSON)
    risk_level = Column(String(16), nullable=False, default="LOW")
    idempotent = Column(Boolean, nullable=False, default=False)
    timeout_ms = Column(Integer, nullable=False, default=30000)
    retry_policy = Column(JSON)
    status = Column(String(16), nullable=False, default="ACTIVE")
    created_at = Column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (UniqueConstraint("server_id", "name", name="uk_mcp_tool"),)


class McpToolGrant(Base):
    __tablename__ = "mcp_tool_grant"

    id = Column(BigInteger, primary_key=True)
    app_id = Column(BigInteger, nullable=False, index=True)
    tool_id = Column(BigInteger, nullable=False)
    status = Column(String(16), nullable=False, default="ACTIVE")
    created_at = Column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (UniqueConstraint("app_id", "tool_id", name="uk_tool_grant"),)


class ToolCallLog(Base):
    __tablename__ = "tool_call_log"

    id = Column(BigInteger, primary_key=True)
    request_id = Column(String(64), nullable=False, index=True)
    tenant_id = Column(BigInteger, nullable=False)
    app_id = Column(BigInteger, nullable=False)
    tool_id = Column(BigInteger, nullable=False)
    params_hash = Column(String(128))
    status = Column(String(16), nullable=False)
    error_code = Column(String(64))
    latency_ms = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class DatasetLineage(Base):
    __tablename__ = "dataset_lineage"

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(BigInteger, nullable=False, index=True)
    dataset_version_id = Column(BigInteger, nullable=False, index=True)
    stage = Column(String(32), nullable=False)
    input_ref = Column(JSON)
    output_ref = Column(JSON)
    config = Column(JSON)
    stats = Column(JSON)
    operator_id = Column(BigInteger, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (Index("idx_lineage_version_stage", "dataset_version_id", "stage"),)


class AnnotationTask(Base):
    __tablename__ = "annotation_task"

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(BigInteger, nullable=False, index=True)
    dataset_version_id = Column(BigInteger)
    name = Column(String(128), nullable=False)
    task_type = Column(String(32), nullable=False, default="CLASSIFICATION")
    total_items = Column(Integer, nullable=False, default=0)
    required_reviewers = Column(Integer, nullable=False, default=1)
    status = Column(String(16), nullable=False, default="PENDING")
    assignee = Column(String(64))
    due_at = Column(DateTime)
    created_by = Column(BigInteger, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (Index("idx_annotation_status", "tenant_id", "status"),)


class AnnotationRecord(Base):
    __tablename__ = "annotation_record"

    id = Column(BigInteger, primary_key=True)
    task_id = Column(BigInteger, nullable=False, index=True)
    item_id = Column(BigInteger, nullable=False)
    reviewer_id = Column(BigInteger, nullable=False)
    label = Column(JSON, nullable=False, default=dict)
    decision = Column(String(16), nullable=False, default="APPROVED")
    confidence = Column(Numeric(5, 4))
    comment = Column(String(2048))
    created_at = Column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("task_id", "item_id", "reviewer_id", name="uk_annotation_item_reviewer"),
        Index("idx_annotation_task", "task_id"),
    )


class EvalTask(Base):
    __tablename__ = "eval_task"

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(BigInteger, nullable=False, index=True)
    dataset_version_id = Column(BigInteger, nullable=False, index=True)
    target_type = Column(String(32), nullable=False)
    target_version = Column(String(128), nullable=False)
    config = Column(JSON)
    metrics = Column(JSON)
    status = Column(String(16), nullable=False, default="PENDING")
    error = Column(String(1024))
    created_at = Column(DateTime, nullable=False, default=utcnow)
    finished_at = Column(DateTime)

    __table_args__ = (Index("idx_eval_status", "tenant_id", "status"),)


class EvalResult(Base):
    __tablename__ = "eval_result"

    id = Column(BigInteger, primary_key=True)
    eval_task_id = Column(BigInteger, nullable=False, index=True)
    metric = Column(String(64), nullable=False)
    value = Column(Numeric(12, 6), nullable=False)
    detail = Column(JSON)
    created_at = Column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (Index("idx_eval_result_task", "eval_task_id", "metric"),)


class RetentionJob(Base):
    __tablename__ = "retention_job"

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(BigInteger, nullable=False, index=True)
    job_type = Column(String(32), nullable=False, default="AI_EVENT")
    target = Column(String(64), nullable=False)
    policy = Column(JSON)
    status = Column(String(16), nullable=False, default="PENDING")
    deleted_count = Column(Integer, nullable=False, default=0)
    detail = Column(JSON)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    finished_at = Column(DateTime)


class Job(Base):
    __tablename__ = "job"

    id = Column(String(64), primary_key=True)
    job_type = Column(String(64), nullable=False, index=True)
    status = Column(String(16), nullable=False, default="PENDING", index=True)
    tenant_id = Column(BigInteger, nullable=False, default=0, index=True)
    app_id = Column(BigInteger)
    creator_kind = Column(String(16))
    creator_id = Column(String(256))
    creator_credential_id = Column(BigInteger)
    request_id = Column(String(64))
    trace_id = Column(String(64))
    idempotency_key = Column(String(128), unique=True)
    payload = Column(JSON, nullable=False, default=dict)
    result = Column(JSON)
    error = Column(String(2048))
    attempts = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    priority = Column(Integer, nullable=False, default=100)
    scheduled_at = Column(DateTime, nullable=False, default=utcnow)
    started_at = Column(DateTime)
    heartbeat_at = Column(DateTime)
    finished_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("idx_job_status_schedule", "status", "scheduled_at"),
        Index("idx_job_tenant_created", "tenant_id", "created_at"),
    )


class GraphEntity(Base):
    __tablename__ = "graph_entity"

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(BigInteger, nullable=False, index=True)
    kb_id = Column(BigInteger, nullable=False, index=True)
    index_version_id = Column(BigInteger, nullable=False, index=True)
    chunk_id = Column(String(64), nullable=False, index=True)
    document_id = Column(BigInteger, nullable=False, index=True)
    name = Column(String(256), nullable=False)
    normalized = Column(String(256), nullable=False, index=True)
    entity_type = Column(String(32), nullable=False, default="LEGAL_TERM")
    extra = Column("metadata", JSON)
    created_at = Column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "index_version_id", "chunk_id", "normalized", "entity_type", name="uk_graph_entity"
        ),
        Index("idx_graph_entity_tenant_name", "tenant_id", "normalized"),
    )


class GraphRelation(Base):
    __tablename__ = "graph_relation"

    id = Column(BigInteger, primary_key=True)
    tenant_id = Column(BigInteger, nullable=False, index=True)
    index_version_id = Column(BigInteger, nullable=False, index=True)
    source_id = Column(BigInteger, nullable=False, index=True)
    target_id = Column(BigInteger, nullable=False, index=True)
    relation = Column(String(64), nullable=False, default="CO_OCCURS")
    chunk_id = Column(String(64), nullable=False)
    weight = Column(Numeric(6, 4), nullable=False, default=1.0)
    created_at = Column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "index_version_id", "source_id", "target_id", "relation", "chunk_id",
            name="uk_graph_relation",
        ),
        Index("idx_graph_relation_tenant", "tenant_id", "source_id"),
    )


class ResourceGrant(Base):
    """细粒度资源级授权（KB/文档等，subject 可为用户、应用或组）。"""

    __tablename__ = "resource_grant"

    id = Column(BigInteger, primary_key=True, default=new_id)
    tenant_id = Column(BigInteger, nullable=False, index=True)
    resource_type = Column(String(32), nullable=False)  # kb | document
    resource_id = Column(BigInteger, nullable=False, index=True)
    subject_kind = Column(String(16), nullable=False)  # user | app | group
    subject_id = Column(String(128), nullable=False)
    level = Column(String(16), nullable=False, default="read")  # read | write | owner
    granted_by = Column(String(128), nullable=False, default="")
    status = Column(String(16), nullable=False, default="ACTIVE")
    expires_at = Column(DateTime)
    created_at = Column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "resource_type",
            "resource_id",
            "subject_kind",
            "subject_id",
            name="uk_resource_grant_subject",
        ),
        Index("idx_resource_grant_subject", "subject_kind", "subject_id"),
    )
