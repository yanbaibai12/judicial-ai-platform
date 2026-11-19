# 设计：非功能与运维验收（剧本、指标、证据）

- change-id：`nonfunc-acceptance`
- 对应规格：`spec.md`
- 门禁 G1：设计覆盖权限、数据、失败与回滚

## 1. 演练剧本公共结构

每个剧本五段式：

```yaml
scene: xxx
target_component: [mysql|redis|milvus|neo4j|object_store|inference]
preconditions: [...]
steps:
  - action: ...
    observe: [指标名 / 查询命令 / 预期值]
    failure_criterion: ...
    data_retention: 7d
rollback: [...]
evidence: [文件类型清单]
pass_criteria: ["RTO ≤ 30 分钟", "..."]
```

`data_retention` 默认 7 天，敏感快照按脱敏规则后再归档。

## 2. 剧本 A：MySQL 迁移与回滚

**目标**：验证 `alembic upgrade/downgrade` 前向兼容与数据不丢失。

| 步 | 操作 | 期望观察 | 失败判据 |
|---|---|---|---|
| A1 | 制造生产等价数据（脱敏）≥ 100 万行 | `SELECT COUNT(*)` 对齐 | 数量不符即停止 |
| A2 | 双版本应用并行部署 | 旧版本 API 无 5xx | 出现 5xx 即中止 |
| A3 | 记录迁移前 `head` | `alembic current` | — |
| A4 | `alembic upgrade head` | 迁移时间 ≤ 30 min | 超时或锁定超 5 min |
| A5 | 新版本写入 + 回读 | 读写一致 | 报 `ProgrammingError` |
| A6 | `alembic downgrade` | 时间 ≤ 20 min；数据 `COUNT(*)` 不变 | 数量减少即判 P0 |
| A7 | 再 `upgrade` 验幂等 | 无报错 | 报错即失败 |
| A8 | 归档迁移日志 | `alembic-history.log`、耗时表、`git_sha` | 缺其中一项 |

**证据**：`evidence/mysql-migration/<date>/` 下含 `alembic-history.log`、`timing.csv`、`diff-checksum.txt`。

## 3. 剧本 B：单点中断

对每个组件分别执行 B1–B5。

| 步 | 操作 | 观察指标 | 恢复判据 |
|---|---|---|---|
| B1 | 记录 baseline 指标 5 分钟 | `judicial_http_requests_total`、`judicial_http_5xx_total`、P95 | 稳定 |
| B2 | 注入故障（`kill -9` / 断网 / 拔 TTL / 阻塞端口） | 告警触发时间、`degraded_reason` | — |
| B3 | 观察 60 秒 | 状态码分布、熔断开合、`/readyz` | 与当前层规格一致 |
| B4 | 恢复动作 | 从故障到 `readyz=200` 时间 | 满足该组件 RTO |
| B5 | 数据对账 | 以 `upload_ack` 流水 / 索引快照 | 无重复或丢件 |

**各组件注入方法**：

| 组件 | 注入 | 降级路径（应在观察中可见） |
|---|---|---|
| MySQL | `docker stop` 主容器 / 拒绝 3306 | 进入只读或 503，切回后自愈 |
| Redis | `DEBUG SLEEP 30` 或 kill | `rate_limit_degraded` 指标上升 |
| Milvus | kill pod | `vector_fallback_sql`，`degraded_reason=VECTOR_BACKEND_UNAVAILABLE` |
| Neo4j | kill pod | `graph_fail_open`，返回 SQL 图谱 + `degraded=true` |
| 推理 | kill vLLM / 阻塞 8000 | 熔断 → 规则兜底或拒答 |

## 4. 剧本 C：联合恢复

| 步 | 操作 | 判据 |
|---|---|---|
| C1 | 同时中断四路 | 预设告警全触发 |
| C2 | 按 MySQL → 对象存储 → Milvus → Neo4j 顺序恢复 | 每步完成检查后再进下一步 |
| C3 | 反向并行恢复（可选对照） | 记录两种顺序 RTO |
| C4 | 全量对账 | `upload_ack` 全覆盖；索引快照一致；图谱节点引用完整 |
| C5 | 8 小时稳定性回归 | 错误率 < 0.5% 且无任务丢失 |

## 5. 剧本 D：压测

### 5.1 前置

- 生产等价硬件清单（含 GPU 型号/显存、CPU 核数、内存、IOPS、网络带宽）；
- 脱敏/合成语料，规模 ≥ 生产预估 1.2 倍；
- 版本、`dataset_version`、`prompt_version`、`model_version` 全部冻结。

### 5.2 阶段

| 阶段 | 时长 | 目标 QPS | 观察 |
|---|---|---|---|
| 预热 | 5 min | 30% 目标 | 缓存预热、连接建立 |
| 渐进加压 | 10 min | 30%→100% | P95/错误率/资源利用率 |
| 稳态 | ≥ 30 min | 目标 | P95 ≤ 500 ms，错误率 < 0.5% |
| 峰值压 | 5 min | 150% 目标 | 记录熔断触发点 |
| 降温 | 5 min | 20% | 验证无积压 |

**停止条件**（任一即停在记录点）：

- 错误率 ≥ 1% 连续 60 秒；
- P95 > 1000 ms 连续 60 秒；
- GPU 显存 > 95%；
- 系统 OOM/重启次数 ≥ 1。

### 5.3 报告字段

QPS 曲线、P50/P95/P99、状态码分布、重试数、熔断次数、各组件资源曲线、`git_sha`、`dataset_version`、`spec_fingerprint`。

## 6. 剧本 E：8 小时稳定性

| 项 | 采集 | 判据 |
|---|---|---|
| 错误率 | 每 5 分钟 | 全程 < 0.5% |
| 内存 RSS | 每 5 分钟 | 斜率 ≤ 1 MB/h（最小二乘拟合） |
| 任务丢失 | 以 `task_id`/`job_id` 对账 | 0 |
| 任务重复 | 以 `idempotency_key` 检查 | 0 |
| 数据污染 | 抽样比对输入输出 hash | 0 |
| GPU 显存 | 每 5 分钟 | 无泄漏趋势 |
| 队列长度 | 每 5 分钟 | 无无界增长 |
| GC/线程 | 每 5 分钟 | 无线程死锁 |

**泄漏判定公式**（不靠肉眼）：

```
linear_regression(rss_ts).slope * 3600  <=  1.0 MB/h
```

## 7. 指标采集设计

| 指标 | 来源 | 频率 | 存放 |
|---|---|---|---|
| HTTP QPS / 状态码 | Prometheus `/metrics` | 15 s | `metrics.db` |
| P50/P95/P99 | 直方图聚合 | 15 s | `metrics.db` |
| 内存 / CPU / GPU | 进程 + nvidia-smi | 5 min | `perf_snapshot.jsonl` |
| 熔断与降级事件 | 日志 grep（`degraded`/`circuit`） | 实时 | `events.jsonl` |
| 任务对账 | DB 查询 | 演练结束一次 | `recon.csv` |
| 告警触发 | Alertmanager / 通知日志 | 实时 | `alerts.jsonl` |

**阈值公式**：

```
pass_rto = (t_app_ready - t_detect) <= rto_target
pass_rpo = count(missing_after_restore) <= rpo_window_allowed
pass_error_rate = errors / total < 0.005
```

## 8. 证据归档

```
specs/nonfunc-acceptance/evidence/<YYYYMMDD>-<scene>/
├── meta.json               # git_sha, migrations_head, dataset_version, 全角色列表
├── logs/                   # 应用日志（脱敏后）
├── metrics/                # 指标 CSV/JSON
├── reports/
│   ├── report.md           # 结论页
│   └── timings.csv
├── alerts/                 # 告警触发记录
├── recon/                  # 数据对账
└── SIGNOFF.md              # 五方签字表
```

`meta.json` 必须含：`git_sha`、`migrations_head`、`dataset_version`、`model_version`、`prompt_version`、`hardware_blueprint`、`executors`、`reviewers`、`timestamp`。

**脱敏要求**：所有日志/报告进入 `evidence/` 前必须通过 `governance_pipeline.sanitize_text` 扫描；发现违规即打回，不得入库。

## 9. 失败处置与降级

| 失败 | 处置 | 降级 |
|---|---|---|
| 演练中硬件异常 | 暂停记录，保留现场 | 修硬件后重跑；本轮结论作废 |
| 对账发现数据缺失 | 标 P0，立即rollback修订 | 回到上一版本，重新评审 |
| 压测期间 OOM | 记 OOM 时刻与上下文 | 调整资源上限后复测 |
| 告警未达 | 记告警缺陷 | 修告警路由后重演练 |
| 演练本身撞生产 | 立刻暂停，保留日志 | 按 runbook 复盘 |

## 10. 签字与复盘

**五方签字表（`SIGNOFF.md`）**

| 角色 | 姓名 | 结论 | 日期 | 备注 |
|---|---|---|---|---|
| 技术负责人 | | | | |
| 安全负责人 | | | | |
| 法律负责人 | | | | |
| 业务负责人 | | | | |
| 运维负责人 | | | | |

**复盘模板**：
1. 事件描述（一句话）;
2. 时间线（`t_detect` → `t_recover`）;
3. 根因;
4. 修复与跟踪;
5. 是否影响 RTO/RPO 承诺;
6. 是否需要调整剧本。

## 11. 与 docs/12、docs/15 对齐

- `docs/12-生产就绪矩阵.md` 中"非功能验收"列为待验收；本 change 产出的证据可直接补填矩阵状态并附 `evidence/` 链接。
- `docs/15-企业级加固验收记录.md` 保留安全加固结论；本 change 记录的非功能结论**追加**到末尾章节，不覆盖原安全结论。
- 两份文档都不复制报告正文，只链接 `evidence/<date>/reports/report.md`。