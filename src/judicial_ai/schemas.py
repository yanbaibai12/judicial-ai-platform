from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CapabilityType = Literal[
    "MODEL_CHAT",
    "MODEL_EMBEDDING",
    "MODEL_RERANK",
    "MODEL_VISION",
    "RAG_RETRIEVE",
    "GRAPH_RETRIEVE",
    "MCP_TOOL",
    "COMPOSITE",
]


class APIModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class TenantCreate(APIModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    name: str = Field(min_length=1, max_length=128)
    type: Literal["COMPANY", "COURT", "PROJECT"] = "COMPANY"
    contact: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AppCreate(APIModel):
    tenant_id: int
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    name: str = Field(min_length=1, max_length=128)
    type: Literal["SYSTEM", "AGENT", "APP", "JOB"] = "SYSTEM"
    description: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CapabilityVersionCreate(APIModel):
    version: str = Field(pattern=r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
    protocol: Literal["OPENAI", "PLATFORM", "MCP"] = "PLATFORM"
    endpoint: str | None = None
    model_deployment_id: int | None = None
    model_name: str | None = None
    adapter_alias: str | None = None
    request_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    response_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    default_timeout_ms: int = Field(default=60000, ge=1, le=600000)
    max_timeout_ms: int = Field(default=120000, ge=1, le=600000)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    qps_limit: int | None = Field(default=None, ge=1)
    concurrency_limit: int | None = Field(default=None, ge=1)
    retry_policy: dict[str, Any] | None = None
    health_check: dict[str, Any] | None = None
    security_level: str = "INTERNAL"


class CapabilityCreate(CapabilityVersionCreate):
    code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    name: str = Field(min_length=1, max_length=128)
    type: CapabilityType
    scope: Literal["GLOBAL", "TENANT"] = "GLOBAL"
    tenant_id: int | None = None
    owner: str = Field(min_length=1, max_length=64)
    description: str | None = None
    tags: list[str] = Field(default_factory=list)


class GrantCreate(APIModel):
    app_id: int
    version: str | None = None
    quota: dict[str, Any] | None = None
    data_scope: dict[str, Any] | None = None
    env: str = "prod"
    expires_at: str | None = None


class InvokeRequest(APIModel):
    version: str | None = None
    input: dict[str, Any]
    options: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: str
    content: Any = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    stop: str | list[str] | None = None
    user: str | None = None


class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str
    input: str | list[str]
    encoding_format: str | None = None
    user: str | None = None


class RerankRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str
    query: str
    documents: list[str]
    top_n: int | None = None


class RetrieveRequest(APIModel):
    kb_ids: list[int] = Field(min_length=1)
    query: str = Field(min_length=1)
    kb_version: str = "prod"
    index_version: str | None = None
    config_version: str | None = None
    top_k: int = Field(default=10, ge=1, le=100)
    filters: dict[str, Any] = Field(default_factory=dict)
    with_graph: bool = False
    with_citation: bool = True
    compress: bool = False
    context_budget_chars: int | None = Field(default=None, ge=200, le=64000)


class ContractReviewRequest(APIModel):
    text: str = Field(min_length=10, max_length=200_000)
    kb_ids: list[int] = Field(default_factory=list)
    top_k: int = Field(default=3, ge=1, le=10)


class ContractReportRequest(ContractReviewRequest):
    title: str = Field(default="未命名合同", max_length=200)
    format: str = Field(default="markdown", pattern="^(markdown|md|html)$")


class KbCreate(APIModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    name: str = Field(min_length=1, max_length=128)
    domain: str | None = None
    description: str | None = None
    security_level: str = "INTERNAL"
    embedding_model: str | None = None
    chunk_config: dict[str, Any] = Field(default_factory=dict)
    owner: str = "ai-platform"
    tenant_id: int | None = None


class DocumentCreate(APIModel):
    title: str = Field(min_length=1, max_length=512)
    text: str = Field(min_length=1)
    doc_type: str = "text"
    source: str = "manual"
    source_ref: str | None = None
    security_level: str = "INTERNAL"
    metadata: dict[str, Any] = Field(default_factory=dict)


class KbVersionCreate(APIModel):
    version: str = Field(min_length=1, max_length=64)
    publish: bool = True


class KbAliasCreate(APIModel):
    alias: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    kb_version: str
    index_version: str | None = None
    retrieval_config_id: int | None = None


class KbGrantCreate(APIModel):
    subject_kind: Literal["user", "app", "group"]
    subject_id: str = Field(min_length=1, max_length=128)
    level: Literal["read", "write", "owner"] = "read"
    expires_at: datetime | None = None


class DocumentAccessPolicy(APIModel):
    access_policy: Literal["INHERIT", "RESTRICTED"]


class RetrievalConfigCreate(APIModel):
    code: str = Field(min_length=2, max_length=64)
    version: str = Field(min_length=1, max_length=32)
    config: dict[str, Any]


class McpServerCreate(APIModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    name: str = Field(min_length=1, max_length=128)
    transport: Literal["HTTP", "SSE", "STDIO", "MOCK"] = "HTTP"
    endpoint: str | None = None
    auth_type: str | None = None
    secret_ref: str | None = None
    owner: str = "ai-platform"
    tenant_id: int | None = None


class McpToolCreate(APIModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    output_schema: dict[str, Any] | None = None
    risk_level: Literal["LOW", "MEDIUM", "HIGH"] = "LOW"
    idempotent: bool = False
    timeout_ms: int = Field(default=30000, ge=100, le=300000)


class McpToolGrantCreate(APIModel):
    app_id: int


class FeedbackCreate(APIModel):
    request_id: str
    rating: Literal["POSITIVE", "NEGATIVE", "NEUTRAL"]
    reason: str | None = None
    comment: str | None = None
    corrected_output: str | None = None
    tags: list[str] = Field(default_factory=list)


class BadCaseCreate(APIModel):
    request_id: str | None = None
    category: str
    sub_category: str | None = None
    root_cause: str | None = None
    priority: Literal["P0", "P1", "P2", "P3"] = "P2"
    owner: str | None = None


class BadCaseUpdate(APIModel):
    status: Literal["OPEN", "INVESTIGATING", "FIXING", "VERIFYING", "CLOSED"] | None = None
    root_cause: str | None = None
    priority: Literal["P0", "P1", "P2", "P3"] | None = None
    owner: str | None = None
    fix_ref: str | None = None
    add_to_regression: bool = False


class DatasetCreate(APIModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    name: str = Field(min_length=1, max_length=128)
    type: Literal[
        "SFT",
        "PREFERENCE",
        "EVAL",
        "RETRIEVAL_EVAL",
        "BAD_CASE",
        "KNOWLEDGE",
        "SAFETY",
    ]
    description: str | None = None
    owner: str = "ai-platform"
    tenant_id: int | None = None


class DatasetVersionCreate(APIModel):
    version: str = Field(min_length=1, max_length=64)
    source_snapshot: dict[str, Any] = Field(default_factory=dict)
    filter_config: dict[str, Any] = Field(default_factory=dict)
    split: dict[str, float] = Field(
        default_factory=lambda: {"train": 0.8, "validation": 0.1, "test": 0.1}
    )
    publish: bool = False


class DatasetExportRequest(APIModel):
    format: Literal["jsonl", "json"] = "jsonl"
    approved_by: str | None = None


class AnnotationTaskCreate(APIModel):
    name: str = Field(min_length=1, max_length=128)
    task_type: Literal[
        "CLASSIFICATION", "EXTRACTION", "GENERATION", "RETRIEVAL", "QA"
    ] = "CLASSIFICATION"
    dataset_version_id: int | None = None
    total_items: int = Field(default=0, ge=0)
    required_reviewers: int = Field(default=1, ge=1, le=3)
    assignee: str | None = None
    due_at: str | None = None


class AnnotationRecordCreate(APIModel):
    item_id: int
    reviewer_id: int | None = None
    label: dict[str, Any] = Field(default_factory=dict)
    decision: Literal["APPROVED", "REJECTED", "ARBITRATED"] = "APPROVED"
    confidence: float | None = Field(default=None, ge=0, le=1)
    comment: str | None = None


class EvalTaskCreate(APIModel):
    dataset_version_id: int
    target_type: Literal["MODEL", "RAG", "RETRIEVAL", "ADAPTER", "AGENT"] = "MODEL"
    target_version: str = Field(min_length=1, max_length=128)
    config: dict[str, Any] = Field(default_factory=dict)


class RetentionJobCreate(APIModel):
    target: Literal["AI_EVENT", "FEEDBACK", "BAD_CASE"] = "AI_EVENT"
    before: str | None = None
    event_types: list[str] = Field(default_factory=list)
    tenant_id: int | None = None
    dry_run: bool = False
