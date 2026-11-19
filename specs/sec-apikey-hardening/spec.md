# 规格：API Key 加密存储加固与本地安全缺陷关闭

- change-id：`sec-apikey-hardening`
- 状态：待评审
- 关联：docs/15-企业级加固验收记录.md、docs/03-AI能力注册与服务网关.md（API Key 可轮换、支持双 Key 并存过渡）、docs/09-部署与运维.md（密钥轮换、禁止在日志/异常信息输出密钥）
- 必载 Skills：`security-review`、`judicial-domain`

## 1. 背景与问题

工作区已有未提交改动，将 API Key 存储从裸 SHA-256 改为密钥化的 HMAC-SHA256。该方案只完成了算法替换，未处理上线路径：

1. **旧凭证兼容**：存量 `app_credential.key_hash` 是 64 位裸 SHA-256 十六进制，新 `hash_token` 输出为 `hmac-sha256$<digest>`。鉴权用等值比较，全部存量凭证在部署后立刻不可用。
2. **密钥轮换**：`API_KEY_HASH_SECRET` 发生变更时，所有已存哈希失配且无法批量重算（哈希不可逆），当前缺少"旧 secret 降级校验 + 截明文升级"的轮换通路。
3. **数据迁移**：`key_hash` 的算法来源未被显式记录，运维无法统计"多少凭证尚未完成哈希升级"，回滚时也无法判断该以何种算法校验。
4. **健康接口信息泄露**：`src/judicial_ai/api/health.py` 的 `_check_database/_check_redis/_check_vector/_check_inference/_check_graph` 一律把 `str(exc)[:240]` 直接写入 `/readyz` 响应。异常文本可能携带连接串、`user:pass@host`、SQL 片段、内网主机名与端口。`/readyz` 无认证，属于对外可见的探测面。
5. **种子命令凭证泄露**：`src/judicial_ai/seed.py::main` 把 `api_key` 与 `admin_token` 明文写入 stdout。该命令在本机联调、CI、演示环境中执行，明文会进入终端历史、CI 日志与工单截图。
6. **正则处理开销**：文本处理链路（`text_utils`、`governance_pipeline`、`context_compression`、`contract_review`、`graph_extract`、`decision`）中存在对超长/非良构输入反复扫描的模式，缺少输入长度门限与匹配预算，在高对比度输入下可能造成不可控的 CPU 消耗，进而影响请求线程。需要给出可测量的修复与回归门槛，而不是"看起来已加固"。

## 2. 目标

1. 存量 API Key 在新版本部署后**继续保持可用**，无需客户端重新发证。
2. `API_KEY_HASH_SECRET` 可轮换，轮换期间新旧 secret 均可校验，且明文到手时自动完成哈希升格。
3. 哈希算法来源可查询、可审计、可回滚。
4. `/healthz`、`/readyz`、`/version` 不再向外透露任何异常细节、主机名、连接串或密钥；仅保留可定位问题的稳定错误类别。
5. 种子命令默认不向 stdout 输出任何凭证明文；确需明文时走显式开关 + 落盘文件（权限 0600）或掩码展示。
6. 正则处理对超长输入具备确定性时间上界，并以基准测试与单元测试固定该门槛。

## 3. 非目标

- 不引入外部 KMS/Vault 的在线调用（密钥管理仍由部署侧 Secret 负责）。
- 不变更租户/应用/权限模型，不新增业务能力。
- 不掩盖"凭证确实已失效"的情况；失效凭证仍必须返回 401，只是不说明差异原因。
- 不做 OCR、双 LoRA、ReFRAG、JEV 生产化，也不覆盖 Milvus/vLLM/OIDC 全链路（属其他 change）。
- 不改变 `AppCredential` 的撤销、过期、锁定语义。

## 4. 角色与权限

| 角色 | 影响面 | 约束 |
|---|---|---|
| 平台管理员 | 创建/轮换/撤销凭证，执行迁移统计接口 | 审计 `credential.create` / `credential.rotate` / `credential.revoke` / `credential.rehash`，不得记录明文 |
| 应用（API Key 调用方） | 使用既有 Key 调用网关 | 轮换与哈希升格都是服务端行为，客户端无需改造 |
| 平台运维 | 配置 `API_KEY_HASH_SECRET` 与 legacy secrets，执行数据库迁移与回滚 | 变更 secret 前须完成本规格的轮换通路部署 |
| 未认证探测者 | 访问 `/healthz`、`/readyz` | 只能得到运行状态与组件健康码，不能得到异常文本 |
| SOC / 审计人员 | 查询审计日志、哈希升级统计 | 可见 `key_prefix`，永远不可见明文与完整哈希 |

数据敏感级别：`app_credential.key_hash` 为敏感凭据派生物，视同密钥；不得进入测试夹具、日志、提交记录或提示词示例。

## 5. 场景与验收标准

### 5.1 旧凭证兼容

- **AC-1.1（旧哈希仍可鉴权）**
  Given 存在一条 `key_hash` 为 64 位裸 SHA-256 十六进制的 `ACTIVE` 令牌，
  And 服务以新代码启动，
  When 客户端用该 Key 的明文调用任一受保护接口，
  Then 返回 2xx，Principal 中的 `credential_id`、`app_id`、`tenant_id` 全部正确。

- **AC-1.2（升格写回）**
  Given 上述场景成功鉴权，
  When 该次请求结束，
  Then 该凭证的 `key_hash` 已被更新为带算法前缀的新哈希，
  And `key_hash` 不等于原裸 SHA-256 值，
  And 审计日志新增 `credential.rehash` 事件，且不含明文与完整哈希（仅 `key_prefix` 与 `credential_id`）。

- **AC-1.3（新哈希仍可鉴权）**
  Given 凭证已使用新算法存储，
  When 用其明文再次调用，
  Then 正常鉴权，且 `key_hash` 不再被重复改写（不产生额外 `credential.rehash`）。

- **AC-1.4（伪造/无效 Key 行为不变）**
  Given 任意不属于库内记录的 Key 明文，
  When 认证，
  Then 一律 401，错误码为 `AUTH_INVALID`，响应不含哈希、算法、是否"存在该 Key"等差异信息，且与 AC-1.1 的合法流量在语义上不可区分（错误码一致）。

- **AC-1.5（撤销与过期语义不变）**
  Given 一条被撤销（`REVOKED`）或已过期（`expires_at` 已过）的凭证，
  When 用其明文认证，
  Then 分别按既有语义拒绝（撤销等价于未命中 `ACTIVE`；过期返回 `TOKEN_EXPIRED`），
  And 不触发哈希升格写回。

### 5.2 密钥轮换

- **AC-2.1（轮换期间旧 secret 可用）**
  Given 配置 `API_KEY_HASH_SECRET=secret-b`，并把 `secret-a` 登记到 `API_KEY_HASH_LEGACY_SECRETS`，
  And 一条 `key_hash` 由 `secret-a` 计算得到，
  When 用其明文认证，
  Then 2xx 鉴权成功。

- **AC-2.2（轮换触发自动升格）**
  Given AC-2.1 鉴权成功，
  When 该次请求结束，
  Then `key_hash` 已由 `secret-b` 重算并写回，`hash_algo` 标识为 `hmac-sha256`，
  And `key_hash` 与用 `secret-b` 的重算结果一致。

- **AC-2.3（升格后旧 secret 移除）**
  Given 已升格为 `secret-b` 的凭证，
  When 从 `API_KEY_HASH_LEGACY_SECRETS` 移除 `secret-a` 并重启服务后用该 Key 认证，
  Then 仍 2xx 鉴权成功（已不依赖旧 secret）。

- **AC-2.4（配置约束）**
  Given `APP_ENV=prod|production|staging`，
  When 应用启动，
  Then 若 `API_KEY_HASH_SECRET` 为默认值或长度小于 32，则启动失败并抛出与本规格一致的错误信息；
  And `API_KEY_HASH_LEGACY_SECRETS` 中不允许出现默认值或长度小于 32 的项；
  And 任意两个 secret 不允许完全相同（避免轮换未生效误判）。

### 5.3 数据迁移与回滚

- **AC-3.1（迁移前向兼容）**
  Given 一个只含旧式裸 SHA-256 哈希的数据库，
  When 执行本次 Alembic 升级，
  Then 迁移完成且所有存量行保持 `ACTIVE` 状态与既有 `key_hash` 值不变（不为空、不被截断）。

- **AC-3.2（算法可观测）**
  Given 迁移完成后任意时点，
  When 管理员查询凭证哈希统计接口，
  Then 可得到按 `hash_algo` 分组的计数（`sha256`（待升格）/ `hmac-sha256`），
  And 响应不含任何 `key_hash` 原值。

- **AC-3.3（回滚安全）**
  Given 已完成升格（既有 `hmac-sha256$…`）的数据库，且回滚到不支持新格式的旧版本代码，
  When 旧代码执行等值鉴权，
  Then 旧版本下该凭证无法鉴权（预期行为），
  But 数据库行不得丢失、损坏或被旧代码覆写；
  And 回滚前须由运维以"轮换为新 Key"方式重新发证，此项作为上线检查项在验收记录中标明，不作为代码路径要求。

  说明：代码层不提供"删除前缀回写"的降级路径，因为降级等于削弱哈希强度，属于禁止项。

- **AC-3.4（唯一性保持）**
  Given 同一明文被不同 secret 加密得到不同哈希，
  When 执行升格写回，
  Then 不得产生重复 `key_hash`（唯一约束保持），若冲突则拒绝写回并记录 `credential.rehash_conflict` 审计事件，鉴权结果不受影响。

### 5.4 健康接口异常脱敏

- **AC-4.1（不泄露异常原文）**
  Given 任一依赖（数据库/Redis/向量库/推理/图库）抛出含敏感信息的异常（示例：`OperationalError: unable to open database file at postgres://admin:p@ssw0rd@10.0.0.8:5432/judicial`），
  When 请求 `/readyz`，
  Then 响应体中不出现 `p@ssw0rd`、`10.0.0.8`、`5432`、`postgres://`、`admin` 等任何形式的连接串片段或凭据，
  And `components.<name>.error` 仅包含稳定错误类别（如 `OperationalError`）。

- **AC-4.2（可运维性不倒退）**
  Given 上述失败场景，
  When 请求 `/readyz`，
  Then `critical_failures` 仍正确列出失败的组件名，`status` 为 `not_ready`，HTTP 状态码为 503，
  And 服务端日志（不含响应体的部分）保留可排障的完整异常与 `request_id`。

- **AC-4.3（探测面状态不变）**
  Given `/healthz`、`/version`、`/metrics`，
  When 未认证访问，
  Then 其响应体中不含异常文本、配置项、连接串、密钥或令牌。

### 5.5 种子命令凭证脱敏

- **AC-5.1（默认不输出明文）**
  Given 默认执行种子命令（不带任何显式开关），
  When 命令结束，
  Then stdout 与 stderr 中不出现 API Key 明文（`ak_` 前缀完整串）与管理员令牌明文，
  And 输出中保留 `api_key_prefix`（前 10 字符）与 `api_key_file`，以及"`admin_token` 来自环境变量 `ADMIN_TOKEN`"这类来源说明，不含值本身。

- **AC-5.2（显式开关才展示掩码）**
  Given 显式指定"展示凭据"开关执行种子命令，
  When 命令结束，
  Then 输出中的明文被掩码为 `ak_****…`（保留前 4 与后 4），
  And 完整明文仅写入输出目录下的文件，文件权限为 0600（POSIX）或等价受限 ACL（Windows），
  And 终端回显不含完整明文。

- **AC-5.3（日志与退出码不变）**
  Given 任意种子执行路径，
  When 命令执行成功或失败，
  Then 退出码语义保持不变，且任何日志、审计与异常栈中不出现 `api_key`、`admin_token` 的明文字段值。

### 5.6 正则处理开销

- **AC-6.1（对恶意输入的时间上界）**
  Given 构造用于放大回溯/重复扫描的输入（长段无终止符的假案号、连续汉字、连续数字、超长单段落无 `。`、超长引号未闭合等），
  When 逐个调用文本处理入口（分句、条文识别、案号识别、脱敏、规则抽取、合同条款切分），
  Then 单次处理耗时在既定基准上限内（默认：单次 ≤ 250ms，输入 20 万字符，Python 3.11、CI 标准 runner）。

- **AC-6.2（输入长度门限）**
  Given 输入文本长度超过配置的最大处理字符数（默认 20 万字符），
  When 调用任一文本处理入口，
  Then 按设计截断到上限后再执行，且不再执行超限部分的正则扫描，
  And 返回结果中带 `truncated` 标记。

- **AC-6.3（无致命回溯模式）**
  Given 全量正则模式清单，
  When 静态审查与基准测试联合验证，
  Then 不存在嵌套无界量词、重叠交替回溯（`(a+)+`、`(.*)*`、`\s*\s*` 一类），
  Or 若确需保留该模式，必须绑定输入长度门限并在规格说明中记录理由。

- **AC-6.4（回归固定）**
  Given 上述基准测试，
  When 纳入 `tests/performance` 层，
  Then 以固定输入种子可重复运行，且结果作为门禁的一部分可复核（发布时间以时间阈值告警，不因 CI 抖动导致门禁失败）。

## 6. 数据与合规

- `app_credential.key_hash`：迁移后保留原值并新增算法标识列；不得回写明文；统计接口只输出计数与 `key_prefix`。
- 审计事件字段 `before`/`after` 只允许 `key_prefix`、`credential_id`、`app_id`、`hash_algo`、`source_secret_label`（如 `primary`/`legacy`），禁止出现 `api_key`、`hash` 明文值。
- 健康接口与种子输出不记录真实当事人信息、案号、身份证号；示例异常一律使用合成占位符（`db.example.invalid`、`u*****@example.invalid`）。
- 涉及司法语料的正则基准输入使用合成文本，不得引入真实案件材料（`judicial-domain` 约束）。

## 7. 依赖与风险

| 风险 | 等级 | 缓解 |
|---|---|---|
| 首次部署后旧凭证全部失效 | 高 | AC-1.1/1.2 强制在线升格，不依赖离线批量迁移 |
| 轮换期间双 secret 校验引入时序差异 | 中 | 采用等值比较不变量；升格为后台同事务写回；不向客户端反馈命中哪把 secret |
| 升格写回撞唯一索引 | 低 | AC-3.4 拒绝写回并审计；鉴权结果不受影响 |
| 脱敏过度导致运维无法排障 | 中 | AC-4.2 要求服务端日志保留完整异常与 `request_id`，仅响应体脱敏 |
| 正则门限截断影响长文档质量 | 中 | 门限默认 20 万字符；超过阈值时打 `truncated` 标记供上游降级 |
| Windows 下文件权限语义与 POSIX 不一致 | 低 | AC-5.2 允许等价受限 ACL；测试按平台分支断言 |

## 8. 开放问题（未决问题不得进入实现阶段的代码路径变更）

1. `/readyz` 在生产是否对未认证暴露？当前按既有约定保持开放，仅做响应体脱敏。若安全评审要求改为内网/鉴权访问，属独立 change，不在本规格内做出改变。
2. `hash_algo` 是否需要额外的独立审计表？本规格采用加列方案；若审计团队要求独立表，另行开 change。

以上两项不阻断本规格的实现路径（均已选定实现口径并在设计中固定）。

## 9. 手工确认项

- 由平台运维确认生产 `API_KEY_HASH_SECRET` 来自 Secret 管理器且长度 ≥ 32。
- 由安全负责人确认把 `/readyz` 异常文本收敛为类别可接受，并同意保留服务端完整日志的方案。