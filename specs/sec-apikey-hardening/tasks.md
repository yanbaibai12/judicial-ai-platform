# 任务清单：API Key 加密存储加固与本地安全缺陷关闭

- change-id：`sec-apikey-hardening`
- 门禁 G2：任务可独立验证
- 回滚：`alembic downgrade 0009_enterprise_controls`

## T1. 哈希基础与候选计算（纯函数）

**内容**（`src/judicial_ai/security.py`）：
- `hash_token(token)`：保持返回 `hmac-sha256$<hex>`（不变，已存在）。
- `hash_token_with_secret(token, secret)`：可复用的纯函数。
- `sha256_token(token)`：裸 sha256（仅识别存量）。
- `token_digest_candidates(token) -> list[tuple[str, str]]`：按设计 §2.2 生成 `(hash_algo, digest_hex)` 候选列表，顺序为 primary-hmac、legacy-hmac…、legacy-sha256。
- 增加 `HASH_ALGO_HMAC = "hmac-sha256"`、`HASH_ALGO_LEGACY_SHA256 = "sha256"` 常量。
- 禁止对 secret / digest 做模块级缓存。

**验证**：`tests/test_api_key_hardening.py` 新增候选顺序、算法前缀、确定性、legacy 注入位置测试。

## T2. 配置与启动校验

**内容**：
- `config.py`：新增 `api_key_hash_legacy_secrets: str = ""`（逗号分隔），并新增 `api_key_hash_secret_list` 辅助解析属性；`api_key_hash_secret` 仍为默认 `dev-api-key-hash-secret`。
- `app.py` 生产校验：主 secret 非默认且 ≥32 字符；legacy 逐项校验；所有 secret 互不相同。

**验证**：`tests/test_api_key_hardening.py` 覆盖合法/默认值/过短/含重复 secret 的启动行为。

## T3. 老凭证兼容与升格

**内容**（`security.py::_app_principal`）：
- 取 credential 不再 `key_hash == hash_token(token)` 而改为：按 `key_prefix`（必有索引）先筛选候选行，再用候选摘要逐个 `compare_digest` 定位命中行。
- 命中后若算法非主 HMAC 或值不同于 primary 摘要，则写回升格；唯一键冲突时跳过写入并审计 `credential.rehash_conflict`。
- 过期/撤销语义保持不变。

**约束**：所有 `key_hash` 比较使用 `secrets.compare_digest`，不因错误情况泄漏命中位置。

**验证**：`tests/test_api_key_hardening.py` 覆盖 AC-1.1–AC-1.5、AC-2.1–AC-2.3、AC-3.4。

## T4. 密钥轮换与 legacy 兼容

**内容**：同 T1/T2 的候选列表消费方；额外测试轮换后移除 legacy 仍可用。

**验证**：AC-2.1–AC-2.4 与 AC-2.3（升格后断开旧 secret）。

## T5. 数据库迁移

**内容**：新增 `migrations/versions/0010_credential_hash_algo.py`（`down_revision="0009_enterprise_controls"`）：
- 幂等加列 `hash_algo` String(16) nullable；
- 按 `key_hash` 前缀回填 `sha256` / `hmac-sha256`；
- `downgrade` drop 列。

**验证**：`tests/test_api_key_hardening.py` 三态表（空/全旧/混合）`upgrade → downgrade → upgrade` 幂等；升格写回后 `hash_algo` 正确。

## T6. 统计可观测（只读）

**内容**：`api/admin.py` 新增 `GET /credentials/hash-stats`（`require_admin`），返回 `{ "by_algo": {"sha256": N, "hmac-sha256": M}, "total": X }`，不暴露 `key_hash` 原值。含审计事件 `credential.hash_stats.read`。

**验证**：AC-3.2；越权访问返回 403。

## T7. 健康接口脱敏

**内容**：
- `errors.py` 新增 `sanitize_exception_summary(exc) -> str`（只保异常类型名，截 64）。
- `api/health.py` 五处 `str(exc)[:240]` → `sanitize_exception_summary(exc)`；额外 `logger.warning(..., exc_info=True)` 并携带 `component`。

**验证**：AC-4.1–AC-4.3。注入含 `postgres://admin:p@ssw0rd@10.0.0.8:5432/...` 的异常，断言响应体不含上述片段，同时 `critical_failures`/状态码正确。

## T8. 种子命令脱敏

**内容**：`seed.py`：
- 移除 stdout JSON 中 `api_key`、`admin_token` 明文；
- 默认输出 `api_key_prefix`、`admin_token_source`、`credential_file`；
- 新增 `--show-credentials` 与 `--credential-file` 选项；仅显式开启时写 0600 权限文件并在 stdout 输出掩码。

**验证**：AC-5.1–AC-5.3。默认执行后对 stdout 正则断言无 `ak_[A-Za-z0-9_-]{20,}` 完整串与 `admin_token` 明文。

## T9. 正则输入门限与高风险模式修复

**内容**：
- `text_utils.py` 新增 `DEFAULT_MAX_TEXT_CHARS = 200_000` 与 `bound_text`；
- 各文本入口（`chunk_text`、`tokenize`、治理清洗与脱敏、合同条款切分、图谱抽取）调用 `bound_text`；
- `governance_pipeline.py` 邮箱脱敏改为 `@` 索引定位 + 受限窗口匹配，避免超长无 `@` 文本回溯。

**验证**：AC-6.2。

## T10. 正则性能基准

**内容**：`tests/performance/test_regex_budget.py`，固定种子对抗输入，P95 告警阈值 250ms，`STRICT_PERF=1` 时才失败。

**验证**：AC-6.1、AC-6.4。

## T11. 门禁与文档

**内容**：
- `bash scripts/gates/quality.sh` 全绿（ruff / mypy / pytest≥75% / alembic heads）；
- `docs/15-企业级加固验收记录.md` 追加本 change 结果；
- `CHANGELOG.md` 补条目。

**验证**：本地完整跑门禁。

## 独立验证矩阵

| 任务 | 独立可验证 | 回滚代价 |
|---|---|---|
| T1/T2 | 是（纯函数+配置） | 无 DB 变更 |
| T3/T4 | 是（鉴权路径） | 无 DB 变更 |
| T5 | 是（迁移） | `alembic downgrade` |
| T6 | 是（只读接口） | 撤路由 |
| T7 | 是（响应体） | 撤脱敏函数 |
| T8 | 是（CLI 输出） | 恢复默认参数 |
| T9 | 是（算法函数） | 提高门限 |
| T10 | 是（测试） | 删除测试 |
| T11 | — | — |