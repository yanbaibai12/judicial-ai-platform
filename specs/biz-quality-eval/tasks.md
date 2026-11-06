# 任务清单：脱敏司法评测集与检索业务质量量化

- change-id：`biz-quality-eval`
- 门禁 G2：任务可独立验证

| # | 任务 | 内容 | 验证方式 | 回滚代价 |
|---|---|---|---|---|
| T1 | 评测集 schema 与校验器 | 实现 JSONL schema 校验（字段齐全、枚举合法、样本长度、scenario 分布）+ 超长门限 | 单测覆盖合法/缺字段/枚举越界/分布不达标四类 | 无 DB 变更，撤模块 |
| T2 | 脱敏扫描门禁 | 封装 `sanitize_text` / `contains_sensitive`，对入库语料扫描，违规即拒入库并审计 | 单测：含身份证号/手机号/邮箱/案号文本被阻断 | 撤校验分支 |
| T3 | 数据集版本冻结 | `dataset_version` 表 + `content_hash` + `redaction_rule_version`；冻结后拒绝写入 | 升迁移、跑 AC-1.1/1.2 | `alembic downgrade` |
| T4 | 标注流程与一致性度量 | `annotation` 表、仲裁记录、Cohen's kappa 计算与 0.70 门槛 | 单测覆盖 kappa、仲裁流转 | 撤表变更 |
| T5 | 指标纯函数 | 扩展 `governance_pipeline.retrieval_metrics` 增加引用准确率、拒答准确率、TP/FP/FN/TN 构造 | 针对设计 §4.1/4.2 的 6 条边界单测 | 纯函数，无回滚代价 |
| T6 | 引用保真匹配 | 生成式引用 → 真实检索片段匹配（含分句连续性、未命中幻觉判定） | AC-3.2 全部样本跑通 | 撤匹配函数 |
| T7 | 评测运行器 | 建 `services/eval_runner.py`，按 dataset_version 批量请求并收集预测 | AC-3.x、AC-4.x 端到端 | 撤流程脚本 |
| T8 | 指标落库 | `eval_result` 表，含 git_sha、各版本号、分桶指标 | 迁移 + AC-4.2 | `alembic downgrade` |
| T9 | 评测集首批入库 | 六类场景各 ≥20 样本合成数据；伴随脱敏授权记录 | AC-2.1 | 撤 fixture |
| T10 | 报告与评分卡生成 | Markdown 报告 + 环比 delta 计算，缺元信息即付费 | AC-3.5/3.6 | 撤 generator |
| T11 | 评测脚本入口 | `scripts/run_eval.py`：`--dataset-version`/`--output`/`--baseline` | CLI 干跑 | 撤脚本 |
| T12 | 评测 CI 集成 | 将核心链路评测接进门禁；仅在人工触发时跑全量 | 门禁脚本新增分支 | 撤 CI 配置 |