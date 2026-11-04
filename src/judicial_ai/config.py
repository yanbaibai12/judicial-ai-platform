from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """平台配置。环境变量名与字段名大小写不敏感。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "司法 AI 中台"
    app_version: str = "0.2.0"
    app_env: str = "dev"
    log_level: str = "INFO"
    docs_enabled: bool = True

    database_url: str = "sqlite:///./data/judicial_ai.db"
    data_dir: str = "./data"
    admin_token: str = "dev-admin-token"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout_seconds: int = 30
    auto_create_schema: bool = True
    node_id: int = -1

    object_store_backend: Literal["filesystem", "s3"] = "filesystem"
    object_store_path: str = "./data/objects"
    object_store_bucket: str = "judicial-ai-originals"
    object_store_endpoint_url: str | None = None
    object_store_region: str | None = None
    object_store_access_key: str | None = None
    object_store_secret_key: str | None = None
    object_store_sse: str = "AES256"
    document_retention_days: int = 30

    inference_mode: Literal["mock", "proxy"] = "mock"
    inference_base_url: str = "http://localhost:8000/v1"
    inference_api_key: str | None = None
    inference_timeout_seconds: float = 60.0

    embedding_model: str = "bge-m3"
    embedding_dim: int = 1024

    rate_limit_enabled: bool = True
    default_qps_limit: int = 20
    rate_limit_window_seconds: float = 1.0
    rate_limit_fail_open: bool = True
    redis_url: str | None = None
    redis_key_prefix: str = "jai"
    redis_cache_ttl_seconds: int = 300
    redis_socket_timeout_seconds: float = 1.5

    vector_backend: Literal["sql", "milvus"] = "sql"
    milvus_uri: str | None = None
    milvus_token: str | None = None
    milvus_collection_prefix: str = "jai_chunks"
    milvus_timeout_seconds: float = 10.0
    milvus_consistency_level: str = "Bounded"
    milvus_sparse_enabled: bool = True
    vector_fallback_sql: bool = True

    graph_enabled: bool = False
    graph_backend: Literal["sql", "neo4j"] = "sql"
    graph_fail_open: bool = True
    graph_max_depth: int = 2
    neo4j_uri: str | None = None
    neo4j_user: str = "neo4j"
    neo4j_password: str | None = None
    neo4j_database: str = "neo4j"

    auth_mode: Literal["token", "oidc", "ldap", "hybrid"] = "token"
    allow_static_admin: bool = True
    auth_failure_limit: int = 10
    auth_failure_window_seconds: int = 60
    oidc_timeout_seconds: float = 5.0
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    oidc_algorithms: str = "RS256"
    oidc_admin_roles: str = "platform-admin"
    oidc_operator_roles: str = "platform-operator"
    oidc_reader_roles: str = "platform-reader"
    ldap_server: str | None = None
    ldap_use_tls: bool = True
    ldap_bind_dn_template: str = "uid={username},ou=people,dc=example,dc=com"
    ldap_user_search_base: str | None = None
    ldap_user_search_filter: str = "(uid={username})"
    ldap_group_attribute: str = "memberOf"
    ldap_admin_groups: str = "cn=platform-admin,ou=groups,dc=example,dc=com"
    ldap_operator_groups: str = "cn=platform-operator,ou=groups,dc=example,dc=com"
    ldap_reader_groups: str = "cn=platform-reader,ou=groups,dc=example,dc=com"
    oidc_default_tenant_id: int | None = None
    ldap_default_tenant_id: int | None = None

    # 细粒度资源级 ACL（KB/文档级授权）；False 时仅租户隔离，保持兼容
    resource_acl_enforced: bool = False
    # 文档写入后自动入队 kb_index 索引任务（按文档版本幂等）
    kb_auto_index_after_upload: bool = True
    # 生产要求索引经过审批，并且检索必须使用显式 prod alias。
    index_require_approval: bool = False
    require_prod_alias: bool = False
    # 示例规则未经法律专业审核，不允许在生产环境启用。
    contract_review_enabled: bool = True

    job_mode: Literal["inline", "worker"] = "inline"
    worker_poll_seconds: float = 2.0
    worker_batch_size: int = 10
    worker_heartbeat_seconds: float = 30.0
    worker_stale_seconds: int = 900
    job_max_attempts: int = 3
    job_retention_days: int = 30

    metrics_enabled: bool = True
    access_log_enabled: bool = True
    security_headers_enabled: bool = True
    hsts_enabled: bool = False
    trusted_hosts: str = "*"
    max_request_bytes: int = 10 * 1024 * 1024

    default_top_k: int = 10
    max_top_k: int = 50

    decision_provider: Literal["rule", "jev"] = "rule"
    decision_shadow_enabled: bool = False
    # JEV 灰度切主百分比（0=关闭；provider=rule 时生效，按 query 哈希稳定分流）
    decision_jev_canary_percent: int = 0
    jev_base_url: str | None = None
    jev_api_key: str | None = None
    jev_model: str = "jev-1.14.0"
    jev_timeout_seconds: float = 2.0
    jev_redact_input: bool = True
    jev_max_input_chars: int = 2000

    rag_compression_enabled: bool = True
    rag_compression_max_chars: int = 8000
    rag_rerank_model: str = "bge-reranker-v2-m3"
    rag_compression_provider: Literal["extractive", "llm"] = "extractive"
    # ReFRAG 分段感知压缩：先按段落/章节边界选段，再做句子级精修
    rag_compression_segment_aware: bool = True
    rag_compression_model: str = "llm.qwen3.chat"

    governance_store_event_content: bool = True
    governance_redaction_enabled: bool = True
    governance_event_max_chars: int = 8000
    governance_min_quality_score: float = 0.6
    governance_auto_accept_score: float = 0.8
    governance_dedup_hamming_distance: int = 3
    governance_retention_days: int = 180
    governance_watermark: str = "judicial-ai-platform"

    cors_origins: str = "*"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"prod", "production"}

    @property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def trusted_host_list(self) -> list[str]:
        if self.trusted_hosts.strip() == "*":
            return ["*"]
        return [host.strip() for host in self.trusted_hosts.split(",") if host.strip()]

    @property
    def oidc_algorithm_list(self) -> list[str]:
        return [item.strip() for item in self.oidc_algorithms.split(",") if item.strip()]

    @property
    def oidc_admin_role_set(self) -> set[str]:
        return _csv_set(self.oidc_admin_roles)

    @property
    def oidc_operator_role_set(self) -> set[str]:
        return _csv_set(self.oidc_operator_roles)

    @property
    def oidc_reader_role_set(self) -> set[str]:
        return _csv_set(self.oidc_reader_roles)

    @property
    def ldap_admin_group_set(self) -> set[str]:
        return _csv_set(self.ldap_admin_groups)

    @property
    def ldap_operator_group_set(self) -> set[str]:
        return _csv_set(self.ldap_operator_groups)

    @property
    def ldap_reader_group_set(self) -> set[str]:
        return _csv_set(self.ldap_reader_groups)


def _csv_set(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()





