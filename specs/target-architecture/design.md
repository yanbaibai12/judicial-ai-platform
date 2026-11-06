# 设计：真实目标架构（Milvus / vLLM / 身份 / 生产集群）

- change-id：`target-architecture`
- 对应规格：`spec.md`
- 门禁 G1：设计覆盖权限、数据、失败与回滚

## 1. Milvus 集群与混合检索

### 1.1 部署形态

- 生产：Milvus Standalone + etcd/MinIO 双实例，或 Operator 管理的分布式集群（Pulsar/MQ 以可替换组件）；开发环境允许单机 docker-compose。
- 集合参数：`consistency_level=Bounded`，开启分区 `tenant_id`（大租户单分区），用 `dynamic field` 存 `kb_id` / `chunk_id` / `document_id` / `acl_scope`。
- 索引：向量稠密 `HNSW`（M=16, efConstruction=200），稀疏 `SPARSE_INVERTED_INDEX`；`num_shards` 按部署规格固定。

### 1.2 混合检索链路

```
query
  -> plan_query (意图识别与预算)
  -> dense_search (top 50)      # 向量
  + sparse_search (top 50)      # 关键词
  -> rrf_fuse(k=60)
  -> rerank (cross-encoder 或本地小模型)
  -> permission_filter (tenant/kb/document ACL)
  -> top_k 截断
  -> compress_results (引用保留不变)
```

RRF 公式：`score = Σ 1 / (k + rank_i)`，`k=60`。

引用保真：返回结果必须带 `chunk_ref = document_id#chunk_seq` 与 `source_offset`，跨 chunk 拼接时保留 offset 距离 ≤ 200 字符的连续性判定；任何生成式引用若无法映射回 `chunk_ref` 判为幻觉引用（对齐 `biz-quality-eval` AC-3.2）。

### 1.3 降级

| 触发 | 降级路径 | 对外标记 |
|---|---|---|
| Milvus 不可用 | `vector_fallback_sql=true` 时走 SQL BM25 内容快照 | `degraded=true`, `degraded_reason=VECTOR_BACKEND_UNAVAILABLE` |
| 向量召回为空 | 自动改走关键词召回 | `recall_strategy=keyword` |
| 重排超时 | 取熔合原始顺序 | `rerank_applied=false` |
| 混合检索单路失败 | 局部降级用另一路权重补 | `recall_strategy` 相应改动 |

## 2. vLLM 推理

### 2.1 服务契约

- `POST /v1/chat/completions`，`stream=true` 时走 SSE：
  - 事件类型：`data: {...}`，结束以 `data: [DONE]` 落定；
  - 每事件含 `id`、`object`、`created`、`model`、`choices[0].delta`;
  - 心跳注释行 `: ping` 每 15 秒一次，防止中间层断连。
- 首字节超时：3 秒连接 + 5 秒首 token；总请求 120 秒；读取 30 秒。
- 取消：客户端断连后通过 `asyncio.cancel` 传递到下游，要求 vLLM 侧终止 batch 中该请求（`aborted=true` 日志）。

### 2.2 限流

- 网关层令牌桶：`default_qps_limit`（默认 20）+ 租户级覆盖；
- 与并发控制联动：`max_concurrent_inferences`（按 GPU 数 × batch 容量冻结）；超限回 429 + `Retry-After`；
- 限流状态在 Sentinel/Cluster 中共享，保证多副本一致。

### 2.3 熔断与兜底

- 失败率连续 5 次、或 30 秒窗口 > 50% → `OPEN` 30 秒 → `HALF_OPEN` 试探；
- 熔断期间**不打模型**，直接走 `decision.py` 的规则引擎：可答场景输出明确规则答案并标注 `degraded_provider="rules"`，不可答场景**拒答**（不编造）。
- 外部模型（含 JEV）仅 shadow 旁路：独立开关 `JEV_SHADOW_ENABLED`（默认 0），默认关；开时只镜像请求用于对比评测，不影响主响应。

## 3. 身份（OIDC + LDAP）

### 3.1 流转

```
前端 -> 地址 Og (OIDC authorization code + PKCE) -> 拿到 id_token/refresh_token
     -> 后端校验 jwks -> claims 映射 -> Principal
```

- `access_token` 建议 ≤ 2 小时，`refresh_token` 7 天且可撤销；
- LDAP：`LDAP_BIND_DN_TEMPLATE` 绑定认证，`LDAP_USER_SEARCH_BASE` + `LDAP_GROUP_ATTRIBUTE` 取组，再按 `LDAP_ADMIN_GROUP`/`LDAP_OPERATOR_GROUP`/`LDAP_READER_GROUP` 映射角色。

### 3.2 映射规则表

| 来源 | 角色 | 权限 |
|---|---|---|
| OIDC `roles ⊇ oidc_admin_roles` 或 LDAP `ldap_admin_group` | `platform-admin` | 全量 |
| OIDC `roles ⊇ oidc_operator_roles` 或 `ldap_operator_group` | `platform-operator` | `OPERATOR_PERMISSIONS` |
| `ldap_reader_group` / 默认 | `platform-reader` | `READER_PERMISSIONS` |

### 3.3 撤销与过期

- 失败次数锁定：`auth_failure_limit` + 滑动窗口（已实现）；
- 平台侧拒绝列表：`revocation_list` 表按 `jti` + 过期时间；refresh 时校验；
- 短 token + refresh 组合降低撤销窗口；OIDC 侧吊销由 IdP 管控，不做本地伪造。

## 4. Kubernetes 生产部署

### 4.1 清单

| 资源 | 要求 |
|---|---|
| Deployment | `replicas ≥ 2`; `strategy.rollingUpdate.maxUnavailable=0`，`maxSurge=1` |
| Service | ClusterIP，端口规范 |
| Ingress | TLS 由 cert-manager 或外部 CA；`tls.termination=reencrypt` |
| NetworkPolicy | 三向隔离（入+出+DNS）；默认 deny-all，白名单放行 |
| PDB | `minAvailable=1` |
| HPA | `minReplicas=2, maxReplicas=8`；CPU 70% / 内存 80%；指标→KEDA/自定义后续接入 |
| ConfigMap/Secret | 敏感项走 KMS/Vault 注入；启用静态加密 |
| ServiceAccount | 最小 RBAC，禁挂 DIND |

### 4.2 探针与优雅终止

- `readiness`: 走 `/readyz`（含 DB/vector/inference 判据）；
- `liveness`: `/healthz`，`periodSeconds=10`，`failureThreshold=3`；
- `preStop`: `sleep 10` 摘流量;
- 停止序列：SIGTERM → 等待 in-flight 完成（≤ `terminationGracePeriodSeconds=60`）→ SIGKILL。

### 4.3 滚动升级与迁移

- 数据库迁移在应用滚动前执行（迁移需要前向兼容 ≤1 个版本：新增列可空、不删列）；
- 契约测试在 CI 中把关；K8s 发布采用 `kubectl rollout status` 聚合判断，失败即 `kubectl rollout undo`。

## 5. 存储与数据库

| 组件 | 目标形态 | 备份 | 切换 |
|---|---|---|---|
| MySQL | 主从半同步 + 一台延迟从 | 每日全量 + binlog 归档，多 AZ 异地 | 每季度演练 failover |
| Redis | Sentinel(3 node) 或 Cluster | RDB + AOF 每小时 | 自动选主，5 分钟 RTO |
| Neo4j | 要么企业版集群，要么双实例 JVM + on-prem 备份 | `dump` 每日 + 事务日志 | 手动引导；`graph_fail_open` 兜底 |
| Milvus | Standalone + etcd/MinIO/元数据备份 | `milvus-backup` 每日 | 元数据重建 + SQL 降级兜底 |
| 对象存储 | S3/MinIO，SSE=Bash256 | 版本化 + 跨区复制 | 双端点读取半径 |

## 6. 失败与回滚

| 场景 | 处置 | 回滚 | RTO 目标 |
|---|---|---|---|
| 发布异常 | 自动 `rollout undo` | 上一镜像版本 | 5 分钟 |
| 迁移失败 | 迁移脚本带 `downgrade`，业务只读兜底 | `alembic downgrade` | 30 分钟 |
| Milvus 故障 | SQL 降级 + 告警 | 手工恢复集合快照 | 15 分钟 |
| vLLM 不可用 | 熔断 + 规则兜底；外部模型仅旁路 | 恢复副本 | 5 分钟 |
| Redis 不可用 | 限流降级 fail-open | Sentinel 切换 | 5 分钟 |
| Neo4j 不可用 | 降级 SQL 图谱 | 集群重建 | 15 分钟 |
| MySQL 不可用 | 半同步切换 | 从升主 | 30 分钟（与 `nonfunc-acceptance` 一致） |

## 7. 优先级（实现顺序与门槛）

| 层级 | 能力 | 必须真实现 | 可先 Mock |
|---|---|---|---|
| P0 | 网关鉴权/租户隔离/审计/幂等 | 是 | — |
| P0 | MySQL 主从与迁移回滚 | 是 | — |
| P0 | 混合检索引用保真 | 是 | 重排模型可降级简单算法 |
| P1 | vLLM SSE + 超时限流熔断 | 是 | 具体模型权重可 Mock |
| P1 | OIDC/LDAP 对接 | 是 | 双 IdP 并行过渡 |
| P1 | K8s 清单全套（TLS/NetPol/PDB/HPA） | 是 | 多集群联邦不做 |
| P2 | Milvus 分布式集群 | 是 | 单机 Milvus 可先跑功能验收 |
| P2 | Redis Cluster / Neo4j 集群 | 是 | 本地 Sentinel 可先跑验证 |
| 旁路 | JEV / 外部模型 | 仅 shadow，可关 | 无对外承诺 |

## 8. 验证清单要点

- 契约测试保持向后兼容；
- 压测硬件必须与生产照片特性一致；
- 演练脚本见 `specs/nonfunc-acceptance/design.md`（互补而非替代）；
- 监控告警：`judicial_*` 指标已有 Prometheus 暴露，需接真实 `Alertmanager`。