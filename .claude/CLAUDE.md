# Forge SDD · 司法 AI 中台

## 不可违反的规则

1. 任何需求先落到 `specs/<change-id>/spec.md`，再进入设计、任务与实现。
2. 司法数据默认敏感：不得把真实当事人信息、案号、身份证号写入测试、日志、提交记录或提示词示例。
3. 外部模型（含 JEV）只能是旁路或显式降级路径；规则引擎与引用保真必须保留兜底。
4. 所有写接口必须有鉴权、租户范围、审计与幂等设计。
5. 涉及隐私、权限、检索引用、数据删除的改动必须加载 `security-review` 与 `judicial-domain`。
6. 提交前执行 `bash scripts/gates/quality.sh`；门禁失败不得标记完成。

## Skills 路由

| 场景 | Skill |
|---|---|
| 新需求、验收标准 | `sdd-spec` |
| 架构、数据模型、接口设计 | `sdd-design` |
| 拆分实施步骤 | `sdd-tasks` |
| 编码与测试 | `sdd-implement` |
| 代码评审 | `sdd-review` |
| 验收与生产就绪 | `sdd-acceptance` |
| 法律概念、引用、合规语料 | `judicial-domain` |
| OpenAPI、SDK、契约兼容 | `api-contract` |
| 召回、重排、压缩、GraphRAG | `rag-design` |
| 认证、权限、密钥、数据安全 | `security-review` |

## 目录约定

- `src/judicial_ai/`：运行时核心包。
- `services/<name>/`：独立服务入口。
- `packages/contracts/`：OpenAPI 与事件契约。
- `packages/sdk-python/`、`packages/sdk-java/`：SDK 交付物。
- `deploy/compose/`、`deploy/k8s/`、`deploy/scripts/`：部署与运维。
- `tests/{unit,integration,contract,e2e,performance,security}`：分层测试。
- `specs/`：变更规格、设计、任务、评审与验收记录。

## 门禁

- G0：规格完整、验收标准可测试。
- G1：设计覆盖权限、数据、失败与回滚。
- G2：任务可独立验证。
- G3：实现通过 lint/typecheck/unit。
- G4：契约与集成测试通过。
- G5：安全评审通过。
- G6：性能与容量目标有证据。
- G7：验收记录与上线回滚方案齐备。
