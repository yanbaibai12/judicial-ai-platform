# Changelog

## Unreleased

### Fixed
- Parameter validation errors returned HTTP 500 when pydantic put a raw `ValueError` in the
  error `ctx`. Error details are now sanitized to JSON-safe values before rendering.

### Security
- Store application credential digests as keyed HMAC-SHA256 (`hmac-sha256$<digest>`)
  instead of bare SHA-256, with a candidate-list lookup so existing bare-SHA-256
  credentials keep authenticating and are rewritten to the current primary secret on
  first successful use (`credential.rehash`). API key rotation uses
  `API_KEY_HASH_SECRET` plus `API_KEY_HASH_LEGACY_SECRETS`; production startup rejects
  the default or sub-30-character secret and duplicate secrets. Review migration
  `0010_credential_hash_algo`.
- Stop leaking dependency exception text into unauthenticated probe surfaces.
  `/readyz` now reports only a stable error class via `sanitize_exception_summary`;
  full traces go to server logs with the failing component. Same treatment applies to
  Redis cache health.
- Stop printing plaintext `api_key` and `admin_token` from the seed command. Output is
  masked by default (prefix + source only); `--show-credentials` writes full material to
  a 0600 credential file and prints an `ak_****…` mask.
- Bound all regex-driven text entry points with `bound_text` (200k chars by default):
  `normalize_text`/`chunk_text`/`tokenize`, governance clean/scrub/probe, clause
  splitting, entity extraction, intent scoring and ReFRAG-style compression. Email
  redaction anchors on `@` instead of scanning unbounded fixed-string input.
  `tests/performance/test_regex_budget.py` freezes the per-call time budget so this
  cannot silently regress.
- Revalidate queued indexing and evaluation jobs against the submitting application,
  tenant and credential, and check current resource grants before indexing. User,
  administrator and legacy jobs without a verifiable application credential fail closed
  in the worker; already running jobs are not interrupted.
- Scope job access to its creator and current knowledge-base/document permissions. Legacy
  app-owned jobs remain accessible to that app; legacy jobs without provable ownership
  require an administrator. Apply migration `0007_job_creator` before upgrading.
- Scope job idempotency keys by tenant, creator and job type; reject conflicting payloads.
- Make knowledge-base grant changes atomic with their audit records and limit grant-list
  access to knowledge-base owners.
- Point retrieval citations to their actual parsed-content snapshot and recheck current
  document permissions when following them; legacy indexes without a snapshot expose no
  misleading snapshot URL.
- Disable the unreviewed example contract-review and report endpoints in production.
  In non-production, `CONTRACT_REVIEW_ENABLED=false` also disables them.
- Treat interest and penalty matches as review leads, not fixed legal thresholds.
  Mark legal and retrieval evidence as unverified and require human review even when
  no rule matches. Retrieval authorization errors are no longer silently suppressed.
- Contract review clients must handle `overall_risk=REVIEW_REQUIRED` instead of `PASS`
  for a result with no findings, and inspect `review_status` and `citation_status`.

### Added
- Container-free validation path for machines without Docker/WSL: `.env.local-staging`
  (SQLite + SQL vector + mock inference, governance switches on) and
  `scripts/acceptance/fault_injection_local.py`, which injects unreachable dependency
  endpoints (redis/graph/inference/vector) in-process, rebuilds the app with cleared
  singletons, and asserts explicit failure instead of silent degradation. Covers only
  "dependency unreachable"; not a substitute for real-node interruption and recovery.
- Relevance floor for retrieval (`RAG_MIN_DENSE_SCORE`, `RAG_MIN_KEYWORD_SCORE`, both off by
  default). Candidates are dropped only when neither the dense score nor the keyword score
  reaches its threshold, and `usage.filtered_low_relevance` reports how many were dropped.
  Without this, an unrelated query still returned top_k chunks labelled VERIFIED.
- staging acceptance tooling: `.env.staging.example` (production-equivalent switches),
  `scripts/check_env.py` (pre-flight validation of 30 settings), and
  `scripts/acceptance/run_staging_acceptance.py` plus `fault_injection.ps1` covering
  A01/A03/A05/A08 with JSON evidence and request_id traceability.
- Multi-format document parsing (`services/document_parser.py`): native HTML, Markdown, CSV/TSV
  and XLSX extraction, plus GB18030/Big5 decode fallback for legacy Chinese documents.
  Legacy Office (.doc/.xls/.ppt), RTF, OFD and images are rejected with 415 and an explicit OCR
  hint instead of being silently decoded into garbage chunks. Parse outcome is persisted as
  `parse_status` (`PARSED`/`PARTIAL`/`FAILED`) with the parser name in document metadata.
- Citation integrity states on retrieval: each result carries
  `citation_status` (`VERIFIED`/`DEGRADED`/`INVALID`/`NONE`) and the response carries
  `citation.status`, `citation.counts` and `citation.review_required`. Non-verified citations
  fall back to the original chunk text and raise a warning log rather than silently degrading.
- OIDC token revocation (`services/revocation.py`, migration `0012_token_revocation`): revoke by
  `jti` or by `subject` before a cutoff time, admin endpoints
  `POST/GET /api/v1/admin/token-revocations` and `DELETE /api/v1/admin/token-revocations/{id}`,
  audit records, and `TOKEN_REVOCATION_CACHE_SECONDS` to bound revocation propagation delay.
- Preserve extracted PDF page numbers in chunks and citations; mark documents with pages
  lacking extracted text as partially parsed. This does not provide OCR or original-file storage.
- Fine-grained resource ACL: `resource_grant` table (migration 0002), grant/revoke/list API on `/api/v1/kbs/{kb_id}/grants`, KB read/write enforcement wired into retrieval API (opt-in via `RESOURCE_ACL_ENFORCED`).
- JEV canary rollout: `CanaryDecisionModel` with stable query-hash routing (`DECISION_JEV_CANARY_PERCENT`), automatic rule fallback, enabling shadow → canary → primary progression.
- ReFRAG segment-aware compression: paragraph/section-boundary segmentation with relevance-scored segment selection before sentence-level refinement (`RAG_COMPRESSION_SEGMENT_AWARE`).
- Backup script (`deploy/backup/backup.sh`, pg_dump + Neo4j hook + MinIO mirror, SHA256 manifest, retention cleanup), restore script and backup image Dockerfile wired to the K8s CronJob.
- `scripts/export_openapi.py` exporting `packages/contracts/openapi.json` (67 paths).
- Auto index trigger: document create/upload now enqueues a `kb_index` job inline (per document-version idempotent, response field `index_job_id`), configurable via `KB_AUTO_INDEX_AFTER_UPLOAD` (default on).
- Local backend integration environment: `deploy/docker-compose.backends.yml` (Milvus standalone / Neo4j 5 / Redis 7), `.env.example` template and `scripts/run-integration.ps1` one-click integration test runner.
- Contract consultation report: `POST /api/v1/contract/report` renders review findings into structured Markdown/HTML reports (severity-sorted, HTML-escaped, with legal basis and KB citations), audited as `CONTRACT_REPORT_GENERATED`.
- Docker-less Windows setup guide in `.env` + `scripts/install-milvus-remote.sh` one-click Milvus standalone installer for remote Linux servers (docker compose or embedded binary).
- Contract risk review engine (`services/contract_review.py` + `POST /api/v1/contract/review`, `GET /api/v1/contract/rules`): clause segmentation, 7 risk rules (interest cap, penalty, unfair exemption/jurisdiction, unilateral termination, unlimited liability, auto-renewal) + required-clause completeness check, each finding annotated with legal basis and KB citations via RAG.

## 0.2.0

### Added
- Milvus dense/sparse hybrid adapter with SQL fallback.
- Neo4j GraphRAG adapter and SQL graph fallback with entity extraction.
- Redis-backed cache and configurable rate limits.
- Database-backed async jobs, worker, job API, retry and stale-job recovery.
- Alembic baseline and production hardening migration.
- OIDC and LDAP authentication, role/permission mapping, auth lockout, tenant status checks.
- Security headers, traceparent propagation, access logging, Prometheus metrics, readiness probes.
- Management console write operations for capabilities, grants, KBs, documents, evals, annotations and retention.
- Python/Java SDKs, OpenAPI contract, load test, CI, K8s/Kustomize, backup/restore assets, Forge SDD skills and templates.
- Audit request/trace correlation and broader request context.

### Security
- Production startup guard for default admin token and wildcard CORS.
- Grant env/quota/data-scope enforcement and expired-grant filtering.
- Tenant status enforcement during app authentication.
