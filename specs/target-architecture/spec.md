# 规格：真实目标架构（Milvus 混合检索 / vLLM 推理 / 真实身份 / 生产集群）

- change-id：`target-architecture`
- 状态：待评审
- 关联：docs/02-总体架构.md、docs/04-统一模型推理服务.md、docs/09-部署与运维.md、docs/12-生产就绪矩阵.md
- 必载 Skills：`rag-design`、`security-review`、`api-contract`

## 1. 背景与问题

当前实现依赖本机单实例（MariaDB、Garnet/Redis、Neo4j）与 mock 推理，无法满足生产目标：

1. **向量检索**：默认 `vector_backend="sql"`，Milvus 只到"能开关"的层；混合检索（稠密 + 稀疏）未形成对齐召回、融合、重排、引用溯源的完整验收链路。
2. **推理**：`inference_mode="proxy"` 只是转发，真实 vLLM/GPU、SSE 流式输出、分层超时、限流熔断与降级均未落地；无 GPU 的聚合与排队策略。
3. **身份**：OIDC/LDAP 代码路径存在（`security.py` 的 `_oidc_principal`/`_ldap_principal`），但未对接真实 IdP，角色映射、撤销、过期刷新未经真实验证。
4. **部署**：有 `deploy/k8s/` 清单，但 TLS、NetworkPolicy 最小权限、PDB、HPA、滚动升级与数据库迁移未生产验证。
5. **存储**：Redis 单实例非 Sentinel/Cluster；Neo4j 非高可用；MySQL 主从、备份、迁移回滚缺招式。

## 2. 目标

1. 结论明确的 Milvus 集群 + 混合检索全链路验收（召回→融合→重排→引用保真）。
2. 真实 vLLM/GPU 推理链路：SSE 流式、分层超时、限流、熔断与规则引擎兜底。
3. 真实 OIDC + LDAP 登录：角色映射、会话撤销、过期刷新经验证。
4. Kubernetes 生产部署就绪：Ingress TLS、NetworkPolicy、PDB、HPA、滚动升级、迁移回滚演练通过。
5. Redis Sentinel/Cluster、Neo4j 高可用、MySQL 主从/备份切换演练通过。

## 3. 非目标（本期范围之外，必须在验收报告中标注为"不适用+范围依据"）

- **OCR**：本期不实现，不在对外承诺范围。
- **双 LoRA / MoE 聚合**：本期不做。
- **ReFRAG 完整论文方案**：不承诺为已实现；至多保留现有近似压缩能力，并在文档中注明"非 ReFRAG 论文实现"。
- **JEV**：仅 **shadow 旁路**模式，不进入主链路；必须显式开关（`JEV_SHADOW_ENABLED`）且任何时刻可关闭，模型不可用时不得影响主流程。
- 主链路引入**外部商业模型调用**作为必需路径（违反 CLAUDE.md 规则 3，外部模型只能是旁路或显式降级路径）。

## 4. 角色

| 角色 | 职责 |
|---|---|
| 平台运维 | 部署、备份、配置、变更执行 |
| 平台管理员 | 能力注册、租户与权限管理 |
| SRE | 可用性、告警、演练、值班 |
| 安全负责人 | 身份映射、密钥、外部调用边界审批 |
| 应用接入方 | 调用网关与 SDK 接入 |

## 5. 场景与验收标准

### 5.1 底层资源失效

- **AC-1.1（MySQL 故障转移）** Given MySQL 主节点宕机，When 哨兵/半同步完成切换，Then `readyz` 在 **≤15 分钟**切回 `ready`（整体 RTO 见 `nonfunc-acceptance`裁定），无数据丢失（RPO ≥ 未退在位数据不做承诺，跨 zone 备份以 `nonfunc-acceptance` 为准）。
- **AC-1.2（Redis Sentinel）** Given Redis 主节点下线，When Sentinel 按 `down-after-milliseconds` 选主，Then 平台在 **≤5 分钟**恢复限流与缓存能力，缺失期间相关接口在 `rate_limit_fail_open=true` 下仍然可用并记录 `rate_limit_degraded` 指标。
- **AC-1.3（Neo4j 节点重启）** Given Neo4j 实例重启，When GraphRAG 检索被触发，Then 在 `graph_fail_open=true` 下回退 SQL 图谱并标记 `degraded=true`，引用仍保留原文溯源不受影响。

### 5.2 流式推理

- **AC-2.1（SSE）** Given 客户端发起 `/v1/chat/completions?stream=true`，When vLLM 首 token 返回，Then 首 token 延迟 P95 ≤ 5 秒，每个 SSE 事件含 `id`/`object`/`delta` 断言齐全。
- **AC-2.2（客户端断开）** Given 流式响应中途客户端断连，When 服务端检测到断连，Then GPU 批次内该请求被取消（不拖进程吞吐），且记录 cancellation 事件。
- **AC-2.3（分层超时）** Given 推理慢，When 数据源超时命中，Then 连接超时 3 秒、读取超时 30 秒、请求总超时 120 秒分别触发相应错误码；报错文本不得泄露 base URL 后的路径细节。

### 5.3 限流、熔断与降级

- **AC-3.1（限流）** Given 每秒 QPS 超出 `default_qps_limit`，When 额外请求到达，Then 返回 429 + `Retry-After`；配合租户级 QPS 覆盖值生效。
- **AC-3.2（熔断）** Given vLLM 连续 5 次失败，When 下一次请求到达，Then 熔断打开 30 秒，此后返回 `INFERENCE_CIRCUIT_OPEN` 且不打到模型；之后按半开探测恢复。
- **AC-3.3（规则兜底）** Given 推理不可用但检索可用，When 用户查询触发规则可答场景，Then 由 `decision.py` 的规则引擎给出明确兜底回答或**拒答**，响应中标注 `degraded_provider` 字段以供客户端识别。规则兜底是主链路的默认降级路径；外部模型只允许同路径作为旁路。

### 5.4 滚动升级

- **AC-4.1（零停机）** Given 生产集群部署新版 Deployment，When 滚动升级执行，Then 全程 `5xx` 率 < 0.1%；`readiness` 探针先于 `preStop` sleep 生效。
- **AC-4.2（迁移前向兼容）** Given schema 迁移必须先于应用启动，When 新版应用发布而旧实例仍在，Then 双版本并行期间不报 `ProgrammingError`（迁移需向后兼容 ≤1 个版本）。
- **AC-4.3（回滚）** Given 发布失败，When 一键回滚，Then 5 分钟内恢复上一版本；数据库回滚须单独演练，并在 `docs/15` 留证。

### 5.5 K8s 节点运维

- **AC-5.1（缩容/驱逐）** Given 节点处于 `drain`，When Pod 被驱逐，Then PDB `minAvailable=1`，不存在两个副本同时消失。
- **AC-5.2（HPA）** Given CPU 持续 60 秒 ≥ 70%，When HPA 生效，Then 副本数在 3 分钟内满足 `minReplicas=2, maxReplicas=8`；缩容仍有 PDB 保护。
- **AC-5.3（网络隔离）** Given 启用 NetworkPolicy，When 未在白名单中的命名空间访问数据面，Then 403/连接被拒，`NetworkPolicy` 覆盖三个方向（入、出、DNS）。

### 5.6 外部调用边界

- **AC-6.1** Given JEV shadow 开关关闭，When 发起查询，Then 全程观测不到任何 JEV 调用（`jev_calls_total==0`）；开启时签名流量仅记录 `event_id` 与耗时，不含原始语料。
- **AC-6.2** Given 任意第三方模型（含 JEV）,When 调用前处理语料，Then 必须先过脱敏；失败需熔断并保留 rule engine 兜底。

## 6. 数据、权限与合规

- 所有向量、索引、缓存、对象存储、图谱数据按 `tenant_id` 分区；跨租户检索一律拒绝（跳过且不在 403 中暴露目标存在）。
- 文档删除必须级联清除原文向量、稀疏索引、图节点、缓存；各数据面用同一 `deletion_id` 作为对账锚点。
- 证书：集群 mTLS 或传输加密必须开启；对象存储 SSE 为 AES256。
- 外部模型调用前的脱敏路径与审计路径必须保留日志，日志不含原文。
- 角色映射表（LDAP 组 → 角色）与 OIDC 声明映射受变更管控，调整需走安全评审。

## 7. 依赖与风险

| 风险 | 等级 | 缓解 |
|---|---|---|
| GPU 供给不足导致压测无法复现生产 | 高 | 压测前明确硬件规格与数据量；不许以笔记本显存替代生产 GPU 结论 |
| Milvus 混合检索调优周期长 | 中 | 后端启用旁路 `hybrid_fallback`；先只做功能验收，性能指标单独走 `biz-quality-eval` |
| JWT 撤销依赖 IdP 侧能力 | 中 | 采用短 token + refresh；平台侧再叠加 challenge/deny-list |
| 迁移导致双版本并行期间契约破坏 | 高 | 迁移必须前向兼容 ≤1 版本；契约测试先门禁 |
| 规则兜底覆盖面不足 | 中 | 明确兜底必须拒答而非编造，并统计 `abstain_rate` |

## 8. 开放问题

1. 是否引入服务网格（Istio/Linkerd）做 mTLS 与细粒度限流？本规格默认**不引入**，先走 Ingress + NetworkPolicy；如需引入另立 change。
2. GPU 容量（卡数/型号）与并发上限由硬件负责人提供；在此之前性能门槛仅冻结口径、不冻结数值。
3. HPA 的扩缩容指标是否启用自定义指标（QPS 与 GPU 利用率）？默认先用 CPU/内存；自定义指标单独变更。

以上均不阻断实现，已选定默认口径见"目标"一节的优先级说明。

## 9. 人工确认项

- 由硬件与安全负责人共同签认 GPU 规格、并发上限与网络隔离策略。
- 由安全负责人签认 OIDC/LDAP 映射与撤销配置。
- 明确对外承诺范围（不承诺 OCR、双 LoRA、ReFRAG 完整实现与 JEV 主链路）。