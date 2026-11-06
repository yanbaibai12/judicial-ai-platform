# 任务清单：非功能与运维验收（评估、报告、签字）

- change-id：`nonfunc-acceptance`
- 门禁 G2：任务可独立验证
- 演练模板见 `design.md` §剧本 A/B/C/D/E

| # | 任务 | 内容 | 验证方式 | 回滚 | 回归证据 |
|---|---|---|---|---|---|
| T1 | 演练剧本骨架落地 | 按 design.md §2 五段式写 `scripts/ops/drills/drill_*.py` 骨架，含 args 与判定字段 | `python drill_*.py --help`，`--dry-run` 输出 YAML 结构 | 撤脚本 | — |
| T2 | 指标采集器 | `scripts/ops/collect_metrics.py`：从 Prometheus / 进程 / nvidia-smi / Redis 抓，落 `metrics.db` 与 `perf_snapshot.jsonl` | 单测：JSON/CSV 输出格式 | 撤脚本 | `metrics.db` |
| T3 | MySQL 迁移回滚脚本 | `scripts/ops/drill_mysql_migration.py`：封装 A1–A8，记录 `timing.csv` 与 `diff-checksum.txt` | 在 staging 幂等重跑 | 撤脚本/回滚 `alembic downgrade` | `mysql-migration/<date>/` |
| T4 | 单点中断脚本 | `scripts/ops/drill_single_point.py`：`--component mysql|redis|milvus|neo4j|object|inference` | 每个组件干跑 + staging 真跑 | 撤脚本 | `single-point/<date>/` |
| T5 | 联合恢复脚本 | `scripts/ops/drill_joint_recovery.py`：顺序 vs 并行两种模式 | AC-3.1/3.2 全绿 | 撤脚本 | `joint-recovery/<date>/` |
| T6 | `upload_ack` 流水 | 上传路径新增 `upload_ack` 表（`idempotency_key`/`document_id`/`created_at`） | 迁移 + 单测：ACK 与文档创建一致 | `alembic downgrade` | `recon.csv` |
| T7 | 数据对账脚本 | `scripts/ops/reconcile.py`：比对 `upload_ack`、索引快照、图谱节点；输出 `recon.csv` | 单测：构造放对完差错场景 | 撤脚本 | `recon/` |
| T8 | 压测数据生成工具 | `scripts/ops/gen_test_corpus.py`：产脱敏/合成语料，规模 1.2×生产预估 | 输出规模 ≥ 阈值且无敏感特征 | 撤脚本 | `test-corpus/` |
| T9 | 8 小时稳定性脚本 | `scripts/ops/stability_8h.py`：串联压测 + 分钟级采样 + 泄漏判定公式 | 2 小时干跑验证采样连续 | 撤脚本 | `stability/<date>/` |
| T10 | 压测脚本 | `tests/performance/loadtest_prod_equivalent.py`（区别于现有 loadtest.py）：强调硬件指纹与停止条件 | AC-5.x 逐条打点 | 撤脚本 | `perf-prod/<date>/` |
| T11 | 证据归档骨架 | `scripts/ops/prepare_evidence.py`：生成 `meta.json`、目录结构、`SIGNOFF.md` | 干跑产出标准目录树 | 撤脚本 | `evidence/<date>/` |
| T12 | 脱敏导出门禁 | 证据入库前跑 `sanitize_text` + 二次扫描；违规即拒绝 | 单测：含身份证/邮箱/案号的文件被阻断 | 撤校验 | — |
| T13 | 告警验证脚本 | `scripts/ops/drill_alerts.py`：注入告警 → 记录到达时间与计数 | AC-7.1 实测 | 撤脚本 | `alerts.jsonl` |
| T14 | 值班交接 checklist | `docs/ops-oncall-checklist.md`（五环节：告警认领→判断→升级→恢复→复盘） | 人工走查一次并留档 | 无回滚 | `SUPPORTING/oncall-checklist-signoff.md` |
| T15 | 签字验收表模板 | `specs/nonfunc-acceptance/evidence/SIGNOFF.md` 五方模板 | 人工会签 | 无回滚 | 正式签字页 |
| T16 | docs/12 矩阵补填 | 演练完成后回填 `docs/12-生产就绪矩阵.md` 的非功能列，链接证据路径 | 人工核对并链到 `evidence/<date>/reports/report.md` | 恢复原值 | — |
| T17 | RTO/RPO 会签记录 | 记录最终冻结的 RTO/RPO 数值与签认日期 | 附到 `SIGNOFF.md` + `docs/09-部署与运维.md` | 无回滚 | — |
| T18 | 复盘模板与流程 | `docs/ops-postmortem-template.md`，含事件、时间线、根因、修复、是否影响承诺、是否调剧本 | 复盘一次真实演练 | 撤文件 | `reports/postmortem-*.md` |

## 依赖关系

- T1–T3 可独立并行；
- T6 需在 T3 之前（迁移 + ACK 一起做）；
- T7 依赖 T6 上线；
- T4/T5 依赖 T2/T7；
- T8–T10 依赖 T1；
- T11–T18 依赖前述演练出数据。

## 独立验证矩阵

| 任务 | 可独立验证 | 回滚代价 | 风险 |
|---|---|---|---|
| T1–T5 | 是（脚本） | 撤脚本 | 低 |
| T6 | 是（DB） | `alembic downgrade` | 中（触及上传路径） |
| T7 | 是（SQL 读） | 撤脚本 | 低 |
| T8–T12 | 是（CLI） | 撤脚本/校验 | 低 |
| T13–T15 | 是（人工核对） | 撤文件 | 低 |
| T16–T18 | 是（文档） | 文件版本控制 | 低 |

## 未决与会签

- **RTO/RPO 数值** 未冻结（设计建议：MySQL RTO ≤30 min、Redis RTO ≤5 min、原件与元数据 RPO ≤5 min）；须由运维+业务负责人共同会签于 T17。
- **压测硬件规格** 未定，故 T10 只冻结口径，性能数值待规格批复后另立验收记录。
- **8h vs 24h** 停留门槛默认 8 小时，业务方若要求 24 小时，需在 T9 与 T15 里调整。
- 以上不阻断实现，已选定默认口径。