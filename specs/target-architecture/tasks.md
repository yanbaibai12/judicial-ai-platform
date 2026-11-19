# 任务清单：真实目标架构（Milvus / vLLM / 身份 / 生产集群）

- change-id：`target-architecture`
- 门禁 G2：任务可独立验证
- 优先级说明见 §design 优先级表

| # | 任务 | 内容 | 验证方式 | 回滚 | 优先级 |
|---|---|---|---|---|---|
| T1 | Milvus 集群部署清单 | `deploy/k8s/milvus/`（含 etcd/MinIO 或 Operator） | 部署到 staging，`health()["status"]=="ok"` | 删命名空间 | P2 |
| T2 | 集合/分区/索引脚本 | 建集合脚本、分区策略、HNSW+SPARSE 索引参数 | 脚本幂等；重复执行无副作用 | 清库脚本 | P2 |
| T3 | 混合检索融合 | 加 RRF(k=60) 与稀疏/稠密双路召回 | 单测：融合顺序、退化单路、空结果 | 回 upsert 旧版本 | P0 |
| T4 | 重排可插拔 | cross-encoder 插槽，超时降级为融合原始顺序 | 单测：超时降级路径 | 摘插槽 | P1 |
| T5 | 引用保真强化 | 结果返回带 `chunk_ref` + `source_offset`；跨 chunk 判定 | AC-3.2 对齐 `biz-quality-eval` | 回旧响应契约（需版本化） | P0 |
| T6 | vLLM SSE 端到端 | `stream=true` 支持、心跳、`[DONE]` | 客户端断开取消测试、首字节延迟采样 | 撤 SSE 分支 | P1 |
| T7 | 分层超时 | 连接/读取/请求总超时三个配置 | 单测：三处分别模拟超时 | 恢复默认配置 | P1 |
| T8 | 网关限流 | 租户级 QPS 覆盖 + 令牌桶 | 压测单测：429 + Retry-After | 关 `rate_limit_enabled` | P1 |
| T9 | 熔断 + 规则兜底 | 5 连失败→OPEN→HALF_OPEN；缺失时规则引擎出口 | AC-3.2/3.3 端到端 | 关熔断开关 | P1 |
| T10 | OIDC 对接 | issuer/jwks/audience、PKCE、角色映射 | 集成测试：正常/过期/错误签名 | 回 admin token 模式 | P1 |
| T11 | LDAP 对接 | 组搜索、组→角色映射、TLS | 集成测试：三类角色 | 回 OIDC-only | P1 |
| T12 | 撤销与刷新 | `revocation_list` + refresh 流程 | AC-3.3 + 过期场景 | 撤路由 | P1 |
| T13 | K8s 基础清单 | Deployment/SA/RBAC/CM/Secret | staging 部署通过 | 清理 YAML | P1 |
| T14 | Ingress TLS | cert-manager + TLS 终止 + 重定向 | `curl https://` 通过；证书过期告警 | 回 HTTP（仅 staging） | P1 |
| T15 | NetworkPolicy | 默认 deny-all + 白名单（入/出/DNS） | AC-5.3 连接拒绝测试 | 撤 policy | P1 |
| T16 | PDB 与 HPA | `minAvailable=1`、CPU/内存指标扩缩容 | 节点 `drain`、CPU 加压演练 | 回静态 replicas | P1 |
| T17 | 滚动升级与迁移流程 | 前向兼容迁移 + `rollout status` | AC-4.1/4.2/4.3 | `rollout undo`；`alembic downgrade` | P1 |
| T18 | 存储主从与备份 | MySQL 半同步、Redis Sentinel、Neo4j 备份、Milvus 备份 | 演练：failover 与恢复 RTO | 还原孤本 | P1 |
| T19 | 监控告警接入 | `judicial_*` 指标接 Alertmanager | 告警触发到运维值班实测 | 撤规则 | P2 |
| T20 | 故障演练脚本 | 每组件中断/恢复自动化脚本 | 与 `nonfunc-acceptance` 剧本对齐执行 | 撤脚本 | P2 |