# 本地开发快速开始

本仓库是司法 AI 中台的**可运行 MVP**，优先保证核心链路在无 GPU、无 Docker 的本地环境也能跑通：

- 能力注册、发布、授权、统一调用
- OpenAI 兼容 Chat / Embeddings / Rerank / SSE
- 知识库、文档切分、索引、混合召回、引用溯源
- MCP Server/Tool 注册、授权、`tools/list`、`tools/call`
- 反馈、Bad Case、数据集版本、用量与审计
- AI 数据治理：事件脱敏、质量筛选、去重、血缘、发布导出、标注复核、评测、保留任务

生产依赖（MySQL、Redis、Milvus、Neo4j、vLLM）通过环境变量切换；本地默认使用 SQLite、内存限流和 Mock 推理。

合同审查使用未经法律专业审核的示例规则，只能在非生产环境进行辅助筛查。
生产环境的审查和报告接口固定关闭；非生产环境可通过 `CONTRACT_REVIEW_ENABLED=false`
关闭。审查结果中的 `review_status` 始终要求人工复核，`legal_basis_status=UNVERIFIED`
表示法条线索未经核验；`citation_status` 区分未请求、未找到、检索不可用和已检索但未经核验，
检索命中不代表引用支持结论。无规则命中也不代表合同无风险。

升级已有数据库时须先备份并执行 Alembic 迁移至最新版本。任务按创建主体隔离：
历史应用任务可由对应应用访问；无法确认用户归属的历史任务仅管理员可见。
索引任务还会检查当前知识库及相关文档权限。幂等键按租户、创建主体及任务类型隔离；
对相同主体和幂等键提交不同任务内容会返回 409。

Worker 执行索引或评测任务前重新检查提交时的应用凭证、应用及租户状态；
索引任务还会检查当前知识库写权限和关联文档权限，评测使用重新验证的应用身份。
任务不会保存 API Key。用户、管理员和缺少凭证记录的历史任务目前无法由 Worker
重新验证身份，会失败并要求使用有效应用凭证重新提交；生产启用用户异步任务前需
对接支持实时验证的身份服务。该检查发生在执行开始前，不能中断已经运行的任务。

检索引用的 `source_url` 指向对应知识库内容快照，读取时仍执行当前文档权限检查。
没有可验证内容快照的历史索引返回 `LEGACY_SNAPSHOT_UNAVAILABLE`，不提供可误导的
快照链接。快照保存的是解析文本，不包含文件原件。

PDF 文本按页面切分，引用页码来自对应解析页面。页面未提取到文字时，文档版本
标记为 `PARTIAL` 并列出未提取页码；空白页与扫描件需要人工区分，目前不提供 OCR。
此功能不保证复杂版式、表格或文字提取的完整性，仍需用业务样本验收。

## 1. 环境要求

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- 可选：Docker / Docker Compose（用于完整依赖栈）
- 可选：NVIDIA GPU + vLLM（用于真实推理）

## 2. 启动

```bash
# 安装依赖
uv sync --dev --extra production

# 可选：复制配置
cp .env.example .env

# 初始化数据库并写入演示数据
uv run python -m judicial_ai.seed

# 启动服务（默认 8080；若被占用可换 18080）
uv run uvicorn judicial_ai.main:app --reload --host 0.0.0.0 --port 8080
```

`seed` 会输出演示租户、应用、API Key、能力列表、知识库和 MCP 工具，例如：

```json
{
  "tenant_id": 370752921723207680,
  "app_id": 370752922083917824,
  "api_key": "ak_xxx",
  "admin_token": "dev-admin-token",
  "capabilities": ["llm.qwen3.chat", "embedding.bge-m3", "rag.legal.retrieve"],
  "kb_id": 370752930338308096,
  "kb_version": "v1.0.0",
  "index_version": "idx-...",
  "mcp_tool": "case.search"
}
```

OpenAPI 文档：<http://127.0.0.1:8080/docs>

## 3. 快速调用

> Windows Git Bash 下 curl 直接传中文 JSON 可能因编码报 “error parsing the body”，推荐用 Python/httpx，或把 JSON 写入 UTF-8 文件后用 `--data-binary @payload.json`。

```bash
export BASE=http://127.0.0.1:8080
export API_KEY=ak_xxx
```

### 3.1 健康检查

```bash
curl -s $BASE/healthz
# {"status":"ok"}
```

### 3.2 对话

```bash
curl -s -X POST $BASE/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"llm.qwen3.chat","messages":[{"role":"user","content":"hello"}]}'
```

### 3.3 检索

```bash
curl -s -X POST $BASE/v1/retrieve \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"kb_ids":[<KB_ID>],"query":"民间借贷利率上限","top_k":3}'
```

### 3.4 统一能力调用

```bash
curl -s -X POST $BASE/api/v1/capabilities/llm.qwen3.chat/invoke \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input":{"messages":[{"role":"user","content":"hello"}]}}'
```

### 3.5 MCP 工具

```bash
curl -s -X POST $BASE/mcp \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

### 3.6 Python 调用示例

```python
import httpx

base = "http://127.0.0.1:8080"
headers = {"Authorization": "Bearer ak_xxx"}

resp = httpx.post(
    f"{base}/v1/chat/completions",
    headers=headers,
    json={
        "model": "llm.qwen3.chat",
        "messages": [{"role": "user", "content": "民间借贷利率上限如何认定？"}],
    },
)
print(resp.json())
```

## 4. 切换真实依赖

### 4.1 MySQL

```bash
export DATABASE_URL="mysql+pymysql://ai:ai_password@127.0.0.1:3306/ai_platform?charset=utf8mb4"
uv run python -m alembic upgrade head
```

本地开发仍可用 `AUTO_CREATE_SCHEMA=true`；生产必须设置 `AUTO_CREATE_SCHEMA=false` 并先执行 Alembic。

### 4.2 Redis 限流

```bash
export REDIS_URL="redis://127.0.0.1:6379/0"
```

未配置 Redis 时自动使用进程内滑动窗口，适合单副本开发。

### 4.3 vLLM 真实推理

```bash
export INFERENCE_MODE=proxy
export INFERENCE_BASE_URL="http://127.0.0.1:8000/v1"
```

vLLM 启动参数见 [docs/04-统一模型推理服务.md](docs/04-统一模型推理服务.md)。

### 4.4 Docker Compose

需要 Docker 环境：

```bash
cd deploy/compose
docker compose up -d --build
```

Compose 会启动 MySQL、Redis、etcd、MinIO、Milvus、Neo4j 和应用。默认推理仍为 Mock；如需 GPU 推理：

```bash
docker compose --profile gpu up -d
# 然后把 app 的 INFERENCE_MODE 改为 proxy，INFERENCE_BASE_URL 改为 http://vllm:8000/v1
```

> 当前 Compose 未在本机验证（开发机没有 Docker），首次启动请检查依赖健康状态。

### 4.5 数据治理配置

```bash
export GOVERNANCE_STORE_EVENT_CONTENT=true
export GOVERNANCE_REDACTION_ENABLED=true
export GOVERNANCE_EVENT_MAX_CHARS=8000
export GOVERNANCE_MIN_QUALITY_SCORE=0.6
export GOVERNANCE_AUTO_ACCEPT_SCORE=0.8
export GOVERNANCE_DEDUP_HAMMING_DISTANCE=3
export GOVERNANCE_RETENTION_DAYS=180
export GOVERNANCE_WATERMARK=judicial-ai
```

治理接口统一位于 `/api/v1`：`events`、`dataset-versions/{id}/publish|export|items|lineage`、`annotation-tasks`、`evals`、`governance/metrics`、`retention-jobs`。

### 4.6 意图路由与上下文压缩

```bash
# 默认规则路由；JEV 仅用于 shadow 对比，默认关闭
export DECISION_PROVIDER=rule
export DECISION_SHADOW_ENABLED=false
export JEV_BASE_URL=
export JEV_API_KEY=
export JEV_MODEL=jev-1.14.0
export JEV_TIMEOUT_SECONDS=2
export JEV_REDACT_INPUT=true

# ReFRAG-style 提取式压缩；只有请求显式 compress=true 时生效
export RAG_COMPRESSION_ENABLED=true
export RAG_COMPRESSION_MAX_CHARS=8000
```

`/v1/retrieve` 与网关 `RAG_RETRIEVE` 支持 `compress` 和 `context_budget_chars`。响应会附带 `decision`（意图、置信度、检索计划）和 `compression`（压缩前后字符数、引用完整性）。JEV shadow 结果只记录在 `decision.shadow`，不影响主链路。

## 5. 运行测试与检查

```bash
uv run pytest
uv run ruff check src services tests
uv run python -m compileall -q src services
```

测试使用临时 SQLite 数据库和 Mock 推理，不依赖外部服务。当前覆盖：

- 能力注册、发布、授权、调用
- 未授权调用拒绝
- OpenAI Chat 非流式/流式
- 能力发现与 Schema
- 文档接入、索引、混合召回、引用
- 租户隔离
- MCP 工具列表/调用/参数校验
- 反馈、Bad Case、数据集版本
- 治理闭环：事件脱敏、质量与血缘、发布导出、双人复核、评测、回归集、保留任务
- 规则意图路由、JEV 适配与 shadow 回退
- ReFRAG-style 上下文压缩、引用保真与请求级预算
- 用量与审计
- 种子数据端到端

## 6. 项目结构

```text
src/judicial_ai/
├── api/                 # HTTP 路由：admin/gateway/retrieval/mcp/governance
├── services/            # 领域服务：能力、推理、检索、MCP、治理、限流
│   └── governance_pipeline.py  # 脱敏、质量、去重、评测指标纯函数
├── models.py            # SQLAlchemy 数据模型
├── schemas.py           # Pydantic 请求模型
├── security.py          # API Key / 管理员认证
├── app.py               # 应用装配
├── seed.py              # 演示数据
└── main.py              # 本地单进程入口

services/                # 独立服务入口（可按部署拆分）
tests/                   # 端到端与单元测试
deploy/compose/          # Docker Compose
docs/                    # 设计文档
```

## 7. 当前实现边界

已实现（v0.2.0）：

- 统一能力注册、版本、发布、回滚、授权
- 网关鉴权、QPS 限流、OpenAI 兼容接口、SSE
- Mock 推理与 vLLM 代理切换
- 知识库、文档解析（PDF/DOCX/TXT）、切分、索引、混合召回、Rerank、引用
- Milvus 稠密/稀疏混合检索适配器；SQL 兜底
- Neo4j GraphRAG 适配器；SQL 图谱兜底与实体关系抽取
- Redis 分布式限流、缓存；失败关闭策略
- 异步任务模型、Worker、任务查询/取消/重试
- Alembic 基线与生产加固迁移
- OIDC/LDAP、角色权限、认证失败锁定、租户状态校验
- 安全头、traceparent、访问日志、Prometheus 指标、依赖探测 `/readyz`
- 管理控制台：能力/授权/知识库/文档/评测/标注/保留任务写操作
- Python/Java SDK、OpenAPI 契约、压测脚本、CI、K8s/Kustomize、备份恢复
- Forge SDD Skills、规格模板与质量门禁

仍需外部环境验收：

- 真实 Milvus/Neo4j/Redis/vLLM 集群联调与故障注入
- 真实 IdP/LDAP 联调、MySQL 迁移回滚、K8s 部署与 TLS
- 生产等价硬件压测、安全扫描、恢复演练
- JEV 仅 shadow；ReFRAG 为应用层压缩，不含论文完整训练/decoder 改造

## 8. 常见问题

**Q: 启动时报端口占用？**  
A: 换一个端口，例如 `--port 18080`。

**Q: `/v1/chat/completions` 返回 “error parsing the body”？**  
A: Windows 终端中文编码问题，改用 Python/httpx，或使用 UTF-8 文件 + `--data-binary`。

**Q: 为什么检索结果是 Mock 向量？**  
A: 本地默认 `INFERENCE_MODE=mock` 且 `VECTOR_BACKEND=sql`；生产设置 `VECTOR_BACKEND=milvus`、`MILVUS_URI` 和 `INFERENCE_MODE=proxy`。

**Q: 管理员令牌是什么？**  
A: 默认 `dev-admin-token`，由 `ADMIN_TOKEN` 环境变量控制；生产必须替换。

# 文档访问与知识版本

部署新代码前执行 `uv run alembic upgrade head`。迁移会将已有文档设为
`INHERIT`，保留原有知识库级访问规则。开启 `RESOURCE_ACL_ENFORCED` 后：

- 文档默认为 `INHERIT`，继承知识库权限；`RESTRICTED` 文档还要求显式文档授权。
- 知识库所有者和平台管理员可管理受限文档。其他主体必须同时具有知识库读取权限和
  文档授权；文档授权本身不会授予知识库访问权。
- 知识库所有者可通过 `PUT /api/v1/documents/{id}/access-policy` 设置
  `{"access_policy":"RESTRICTED"}` 或 `{"access_policy":"INHERIT"}`，
  通过 `/api/v1/documents/{id}/grants` 管理文档授权。切回继承时会撤销已有文档授权，
  再次设为受限时需要重新授权。
- 列表、详情、检索、图谱、合同审查和评测使用当前文档权限；授权变更记入审计。

新知识库版本保存创建时的解析文本块、检索元数据和引用标题，状态为 `PARSED`。
这些快照不包含 PDF/DOCX 原始文件，也不代表原件归档。自动索引为当前内容创建新版本；
显式重建版本只使用该版本的解析内容。旧版本标记为 `LEGACY`，原有可用索引仍可检索，
但无法凭不存在的历史快照重建；应创建新版本并重新索引。切换或指定索引时，必须使用
所属的知识库版本。未配置 `prod` 别名时，默认使用最近成功就绪的索引。

