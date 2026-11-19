# 设计：API Key 加密存储加固与本地安全缺陷关闭

- change-id：`sec-apikey-hardening`
- 对应规格：`spec.md`
- 门禁 G1：设计需覆盖权限、数据、失败与回滚

## 1. 总体方案

四个子问题各自独立，按"最小侵入"原则设计，不引入新服务、不改对外鉴权契约。

| 子项 | 方案核心 | 失败行为 |
|---|---|---|
| 旧凭证兼容 | 哈希值自带算法前缀；鉴权时按前缀生成候选哈希列表逐个比对；命中旧算法后在同事务内升格 | 全部候选不命中 → 401，不改写数据库 |
| 密钥轮换 | 主 secret + 可选 legacy secret 列表，均参与候选哈希计算；升格一律以主 secret 写回 | 主 secret 配置不合规 → 启动期失败 |
| 数据迁移 | Alembic 加列 `hash_algo`（默认由 `key_hash` 前缀推断），不改写 `key_hash` | 迁移幂等，可空则跳过 |
| 健康脱敏 | 异常 → 稳定错误类别（异常类名）+ 长度截断；完整异常只进服务端日志 | 任何情况下响应体都不含原始 `str(exc)` |
| 种子脱敏 | 默认只输出前缀 + 文件路径；显式开关才写 0600 文件并在终端掩码 | 命令退出码不变 |
| 正则开销 | 统一输入长度门限 + 超时预算 + 消除已知高风险模式；基准测试冻结 | 超限截断后仍返回结果并带 `truncated` |

## 2. API Key 哈希与升格

### 2.1 存储格式

`key_hash` 取值形态：

- 新格式：`hmac-sha256$<64 位十六进制>`
- 遗留格式：`<64 位十六进制>`（可用性不依赖格式，有无前缀均按长度与字符集判别）

新增列 `app_credential.hash_algo`：

| 取值 | 含义 |
|---|---|
| `sha256` | 裸哈希（待升格） |
| `hmac-sha256` | 已升格为密钥化 HMAC |

`key_hash` 唯一约束保留不变，用于防止同一明文的重复登记。

### 2.2 候选哈希计算

设 `token` 为请求明文，`secrets = [primary] + legacy`：

```
candidates = []
for s in secrets:
    candidates.append(("hmac-sha256", hmac.new(key=s, msg=token, sha256).hexdigest()))
candidates.append(("sha256", hashlib.sha256(token.encode()).hexdigest()))
```

候选顺序：先 Hmac（主 secret）→ Hmac（各 legacy）→ 裸 sha256。
命中裸 sha256 或 legacy 时，一律以主 secret 的 HMAC 重算后写回。

安全性说明：候选比较使用 `secrets.compare_digest`，不因命中位置不同返回可区分错误；响应对"不存在 / 存在但算法旧"不做语义区分。裸 sha256 仅作为"识别存量数据"的兼容入口，升格完成后自然不再触发。

### 2.3 生命周期：升格写回

`_app_principal` 命中候选后：

```
最近候选算法 != hmac-sha256  或  摘要 != 主 secret 摘要
  → 计算 new_digest（主 secret）
  → 若 key_hash 唯一约束冲突：跳过写入，审计 credential.rehash_conflict，鉴权仍成功
  → 否则：credential.key_hash = "hmac-sha256$" + new_digest
         credential.hash_algo = "hmac-sha256"
         审计 credential.rehash（仅 before.hash_algo / after.hash_algo / key_prefix / app_id）
  → 与 last_used_at 更新同一事务提交
```

写入是渐进式（pull-through），不需要独立的批处理作业或停机窗口。删除 `hash_algo` / `key_hash` 的数据清洗不在此范围内（对应 `<removal-entire-row>`，由数据保留策略另行决策，不在本 change 删除任何行）。

### 2.4 配置与启动校验

`Settings` 新增/沿用：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `api_key_hash_secret` | `dev-api-key-hash-secret` | 主 HMAC 密钥 |
| `api_key_hash_legacy_secrets` | `[]` | 逗号分隔，仅用于校验存量 |

生产启动校验（`app lifespan` 内，与既有 `ALLOW_STATIC_ADMIN` / `CORS_ORIGINS` / `SQLITE` 检查同级）：

- `api_key_hash_secret` 不等于默认值 且长度 ≥ 32，否则抛 `RuntimeError`；
- 每个 legacy secret 同样满足长度 ≥ 32 且不等于默认值；
- 所有 secret 互不相同；
- `hash_token` 使用主 secret，不做缓存失效（见 §2.5）。

`docs/03` 要求"API Key 支持双 Key 并存过渡"，本设计通过 `generate_api_key` 与 `rotate_credential` 两处机制共同满足：轮换期可以同时存在两条 `ACTIVE` 凭证（旧的在下一接口调用前仍可用），任一端在窗口期内切换密钥不中断服务。轮换后旧凭据被置 `REVOKED`，与既有语义保持一致。

### 2.5 密钥读取不缓存

`hash_token` 每次调用读取 `get_settings().api_key_hash_secret`。为避免密钥变更后进程内缓存持续使用旧 secret，禁止对该结果做 `lru_cache` 或模块级常量缓存。密钥变更需要进程/配置重载才生效，这是部署侧职责（与 Vault 轮换常规做法一致）。

## 3. 迁移设计

新迁移 `migrations/versions/0010_credential_hash_algo.py`（`down_revision = "0009_enterprise_controls"`）：

- **Upgrade**
  - 检查 `app_credential.hash_algo` 是否已存在（幂等）；
  - 加列 `Column("hash_algo", String(16), nullable=True)`；
  - 按 `key_hash` 中是否带 `hmac-sha256$` 前缀，回填批量 `UPDATE` → `hmac-sha256` / `sha256`；
  - 不修改 `key_hash`，不删除行，不改默认值（列允许 PHP 语义"尚未升格"保留在回填精度范围内）。
- **Rollback**：`drop_column("hash_algo")`，`key_hash` 不受影响。
- 索引：无新增索引，避免拖慢在线写；统计接口按列实时聚合（表规模按万行量级估算可接受）。

迁移可重复执行：以 `inspect(bind).get_columns("app_credential")` 判定列是否已存在（与 `a48e5fe01609_production_hardening.py` 现做法保持一致）。

## 4. 健康接口脱敏

### 4.1 分层

```
HTTP 响应体 (对外):   { "status": "error", "error": "OperationalError" }
HTTP 响应体 (错误码): { "code": "DB_UNAVAILABLE", ... }  // 若需要，不复用 str(exc)
服务端日志 (对内):    logger.warning("readyz component failed", component=..., request_id=..., exc_info=True)
```

### 4.2 实现要点

新增 `src/judicial_ai/errors.py` 的 `sanitize_exception_summary(exc) -> str`：

```
name = type(exc).__name__
return name[:64] if name else "UnknownError"
```

`health.py` 五处 `str(exc)[:240]` 统一替换为 `sanitize_exception_summary(exc)`，同时各通过 `_logger` 输出完整异常（含连接串）到服务端日志。

已有 `forbid_debug_exports`、`include_payloads` 类配置不动；`/readyz` 保持兼容现有探针读取 `components.<name>.status` 与 `critical_failures` 字段，不删除既有字段，仅替换 `error` 的取值语义（原值为自由文本，无外部消费方依赖其内容）。

## 5. 种子命令脱敏

### 5.1 输出契约

默认（无开关）stdout：

```json
{
  "tenant_id": 1,
  "app_id": 1,
  "api_key_prefix": "ak_XXXXXXXX",
  "admin_token_source": "ADMIN_TOKEN",
  "credential_file": "./data/seed-credentials.json",
  "capabilities": [...]
}
```

`api_key` 明文字段从 JSON 中完全移除，`admin_token` 明文同理。

### 5.2 显式开关

`python -m judicial_ai.seed --show-credentials [ --credential-file PATH ]`

- 文件默认路径：`./data/seed-credentials.json`，内容含 `api_key` 明文，Windows 下 `os.chmod(path, 0o600)`（POSIX 一致）；
- stdout 的 `api_key` 字段替换为 `ak_****<后 4 位>` 的掩码；
- 文件写入前 `os.open(path, O_CREAT|O_WRONLY|O_TRUNC, 0o600)`，避免 umask 宽松导致的裸写；
- 兼容旧调用方式 `python -m judicial_ai.seed`（不带参数）→ 仍然可用，输出行为按默认脱敏；为避免破坏现有自动化脚本，JSON 顶层新增字段，不删除 `tenant_id`/`app_id`/`capabilities` 等非敏感字段。

## 6. 正则处理开销

### 6.1 统一门限

`src/judicial_ai/text_utils.py` 新增：

```python
DEFAULT_MAX_TEXT_CHARS = 200_000

def bound_text(text: str, limit: int = DEFAULT_MAX_TEXT_CHARS) -> tuple[str, bool]:
    """截断超长文本，返回 (处理后文本, 是否截断)。"""
```

所有对外入口（`chunk_text`、`tokenize`、治理清洗/脱敏、合同条款切分、图谱抽取、学科意图识别）在进入正则扫描前调用该函数。被截断时结果元数据中带 `truncated: true`。

### 6.2 已确认的高风险模式与处置

重排审查结论（暂以静态审查为主，实测基准门槛见 AC-6.1）：

| 位置 | 模式 | 风险 | 处置 |
|---|---|---|---|
| `context_compression.py` `《[^》]{1,80}》` | 未闭合书名号、超长匹配 | 中：在长段无 `》` 输入下不停尝试 | 限界 `bound_text` 后恒定上界；匹配结果不因回溯爆炸 |
| `governance_pipeline.py` `[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}` | 双重 `[…]+\.[…]+` 交替 | 低-中：典型长无空格串有回溯 | 加 `bound_text` + 无 `@` 时短路跳过，避免长串全串扫描 |
| `contract_review.py` `[^。；\n]{0,40}` 各类 | 无界否定匹配 | 低：`{0,40}` 已限界 | 保留；统一 `bound_text` |
| `context_compression.py` `第[一二三四五六七八九十百千零〇0-9]{1,10}条` | 重复"第十" | 低：`{1,10}` 限界 | 保留 |
| `decision.py` 意图关键词 | 眨眼通配 `|` 多重分支 | 低 | 保留（具名字典，输入即用户查询，短文本） |

唯一被判定"需改结构"的是治理脱敏邮件正则：改为先以 `@` 字符索引定位候选位置，再在候选位置做受限窗口匹配，避免对无 `@` 的超长文本逐字符回溯。

### 6.3 基准测试

`tests/performance/test_regex_budget.py`：

- 固定随机种子构造对抗输入（无终止长 `《`、连续汉字、连续数字、长无空格字母数字串、超长单段落等）；
- 对每个被测入口执行 N 次，报告 P95；
- 门槛采用告警阈值（默认 250ms/次），避免 CI 抖动导致门禁故障；若超出告警，测试仅在 `STRICT_PERF=1` 时 fork 失败。

## 7. 失败与回滚

| 阶段 | 失败场景 | 行为 | 回滚路径 |
|---|---|---|---|
| 部署前 | 生产 secret 未配置/长度不足 | 应用拒绝启动，`prism` 日志明确提示 | 配好 secret 后重启即可，无数据变更 |
| 部署运行中 | 旧 Key 命中裸哈希但写回撞唯一键 | 鉴权成功，跳过写回，审计 `credential.rehash_conflict` | 无需回滚；下一次轮换登记新 Key |
| 部署运行中 | 只配了 legacy 未配主 secret（非法） | 启动期校验直接拒绝 | — |
| 升格完成后 | 代码回退到旧版本 | 已升格 Key 无法鉴权 | 运维轮换发新 Key（属运维检查项，非代码路径）；或回退数据库列时保留 `key_hash` 原值 |
| 脱敏后 | 运维定位困难 | 服务端日志仍完整，`request_id` 串联 | 增加 log level 即可回溯，不需要回滚版本 |
| 正则门限生效 | 超长文档处理被截断 | 结果带 `truncated`，调用方可降级 | 将 `DEFAULT_MAX_TEXT_CHARS` 调高重启，无迁移代价 |

回滚命令（数据库）：

```
alembic downgrade 0009_enterprise_controls
```

降级只drop `hash_algo` 列，`key_hash` 保留；已经升格的 `hmac-sha256$…` 字符串不会被清理（保持非破坏）。

## 8. 测试与验证

- 单元：候选哈希发生/不同 secret/升格写回/冲突跳过/启动校验/脱敏函数/种子默认输出、掩码、文件权限、超长截断后的正则时间预算。
- 契约：`/readyz` 在注入敏感异常后字段级断言（`"p@ssw0rd" not in response.text` 等）。
- 迁移：空表 + 含裸哈希表 + 混合表三种初始态，`upgrade` → `downgrade` → `upgrade` 幂等。
- 性能：`tests/performance/test_regex_budget.py` 固定种子基准。