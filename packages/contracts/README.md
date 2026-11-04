# API 契约

`openapi/openapi.json` 由 `judicial_ai.app.create_app().openapi()` 生成，是 SDK 与契约测试的唯一来源。

```bash
uv run python scripts/generate_openapi.py
```

发布规则：

- 主版本一致才能保证 SDK 兼容；破坏性变更必须升级主版本并保留弃用期。
- 新增字段默认可选；删除字段、修改类型、收紧枚举视为破坏性变更。
- CI 应执行 `openapi-diff` 与契约测试后才允许发布。
