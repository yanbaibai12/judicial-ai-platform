# Changelog

## Unreleased

### Security
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
