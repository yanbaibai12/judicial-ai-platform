---
name: api-contract
description: OpenAPI 契约、SDK 生成、版本兼容、错误码与契约测试。
---

# API Contract

## 使用时机
新增或修改接口、事件、SDK、错误码、分页或鉴权方式时。

## 步骤
1. 先改 `packages/contracts/openapi/openapi.json` 或生成源。
2. 检查请求/响应、错误模型、幂等键、限流头和追踪头。
3. 运行契约测试与 `openapi-diff`，确认破坏性变更已版本化。
4. 更新 Python SDK 与 Java SDK 骨架，保留旧字段至少一个主版本。
5. 记录迁移说明与弃用时间。

## 门禁
- 新接口有示例、错误码和权限说明。
- 破坏性变更不得静默发布。
- SDK 与契约版本一致。
