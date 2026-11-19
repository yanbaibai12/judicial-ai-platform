# 设计：脱敏司法评测集与检索业务质量量化

- change-id：`biz-quality-eval`
- 对应规格：`spec.md`
- 门禁 G1：设计覆盖权限、数据、失败与回滚

## 1. 总体结构

```
评测集 YAML/JSONL ──> 校验与脱敏扫描 ──> dataset_version 冻结
                                          │
                                          ▼
              eval_runner ──> 检索 API ──> 预测收集
                                          │
                                          ▼
            指标纯函数 (retrieval_metrics / 引用保真 / 拒答)
                                          │
                                          ▼
        数据集分桶汇总 ──> 评分卡 + 件 ──> eval_result 表 ──> 环比对比
```

## 2. 数据集 schema

数据集落盘为 JSONL（每行一个样本），生产入库后由 `dataset_version` 封装哈希。

```jsonc
{
  "sample_id": "eval-sample-000123",
  "scenario": "no_answer",        // common | hard | no_answer | outdated_basis | permission_bounded | conflicting
  "question": "民间借贷利率上限如何认定？",
  "difficulty_score": 3,           // 1-5
  "expected_answer_type": "abstain",  // answer | abstain
  "abstain_required": true,
  "annotated_refs": [
    {"chunk_ref": "doc-12#chunk-3", "crisp_reason": "利率四倍规定"}
  ],
  "expected_citations": ["doc-12#chunk-3", "doc-12#chunk-4"],
  "sensitivity_label": "INTERNAL",
  "redaction_rule_version": "redaction-v1",
  "created_by": "annotator-a",
  "created_at": "2026-11-04T10:00:00Z"
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `sample_id` | 是 | 全局唯一，跨版本保持稳定 |
| `scenario` | 是 | 六类场景枚举 |
| `question` | 是 | 合成/脱敏查询文本，长度 ≤ 500 字符 |
| `expected_answer_type` | 是 | `answer` / `abstain` |
| `abstain_required` | 是 | 与 `expected_answer_type` 保持一致，冗余校验 |
| `annotated_refs` | 是 | 带依据的引用标注 |
| `expected_citations` | 是 | 评测引用准确率的判定集合 |
| `difficulty_score` | 是 | 1–5，难例桶 ≥4 |
| `sensitivity_label` | 是 | `INTERNAL`/`CONFIDENTIAL`，控制导出与留存 |

## 3. 标注 schema 与一致性

`annotation_rules.yaml` 随 `dataset_version` 发布，含：

- 每条规则的 `rule_id`、判定例、反例、版本号；
- 判定口径定义："无答案"="无法从提供的合法引用中找到可支持结论"，"旧版本依据"="存在更新版本的法条/司法解释已替代"，"权限受限"="该引用片段标记为非当前身份可读"，"冲突依据"="多份允许引用彼此矛盾，且需人工权衡"。

`annotation` 表：

| 字段 | 说明 |
|---|---|
| `annotator_id` | 标注员 |
| `sample_id` | 关联样本 |
| `version` | 标注版本 |
| `labelfor_ref` | 引用片段 ID 的相关性判定 |
| `adjudicated` | 是否经法律专家裁决 |
| `adjudicator_id` | 裁决人 |
| `adjudication_note` | 裁决意见，保留原文 |
| `dispute_flag` | 争议标记 |
| `kappa_contribution` | 一致性度量参与标记 |

一致性度量：两名独立标注员对同一 `sample_id` 的 `expected_citations` 集合做参考集一致性判定，采用 Cohen's kappa，门槛 ≥0.70；未达门槛的样本流转至法律专家仲裁，裁决结果作为金标，原始分歧保留在 `dispute_flag` 中不删除。

## 4. 指标计算

### 4.1 公式

| 指标 | 口径 | 目标 |
|---|---|---|
| Recall@10 | 命中 relevant 集的 chunk / \|relevant\|；`governance_pipeline.retrieval_metrics` | ≥ 0.90 |
| 引用准确率 | 有效引用数 / 生成式给出的引用总数。有效 = 引用句命中真实检索片段且分句连续 | ≥ 0.95 |
| 拒答准确率 | TN / (TN + FP)。TN=应拒答且拒答，FP=应拒答却给答案，FN=应答却拒答 | ≥ 0.90 |
| 误报 (FP) | 检索命中但引用无效或与标注无关 | 报告给出 |
| 漏报 (FN) | 标注相关但未进 top-10 | 报告给出 |

### 4.2 匹配算法

1. 生成式回答的每个 `citation_id` 必须入 `index_version` 已知 chunk 的集合，否则记为幻觉；
2. 引用片段的连续性：同文档内两 chunk 起止偏移距离 ≤ 200 字符视为连续；
3. 未回答场景：抽到 `abstain_required=True` 却返回结论，直接 FP；反之给拒答则 TN；
4. 拒答判定由一条预置的 `abstain_pattern` + 模型的 `refuse_flag`（如果存在）双路径。

## 5. 评测流水线

```
1 load_dataset(dataset_version)
2 validate_schema + validate_redaction_scan  （不通过直接失败）
3 for each sample:
    call retrieve(query, top_k=10, kb_ids=joined)
    call chat(prompt) if scenario needs generative
    record: retrieved_ids, citations, latency, refuse_flag
4 convert to metrics record
5 bucket by scenario / difficulty_score
6 compare with baseline
7 generate report (Markdown) + persist eval_result
```

### 评估请求身份

走平台管理员（默认），并在 `eval_result` 记 `runner_identity`；后续可切换专用 `eval-runner` 账号（属开放问题 #1）。

### 报告结构

| 节 | 内容 |
|---|---|
| 元信息 | git SHA、`dataset_version`、`index_version`、`prompt_version`、`model_version`、`migrations_head`、执行人、执行时间 |
| 总览 | 全局 Recall@10 / 引用准确率 / 拒答准确率 / 执行失败率 |
| 分场景 | 六类场景各自四项指标 + 样本数 |
| 分难度 | 5 档桶各指标，难例桶单独结论 |
| 性能 | P50/P95 延迟、QPS 有则计 |
| 环比 | 与基线 delta，超出阈值变红，未超则标记 PASS |

## 6. 失败与回滚

| 阶段 | 失败 | 行为 | 回滚 |
|---|---|---|---|
| 装载 | schema 校验失败 | 评测中止 | 修数据集后重跑版本 |
| 脱敏扫描 | 发现违规模式 | 评测中止，报告现象但不落原始出处 | 清洗后新建 dataset_version |
| 检索调用 | 超时/异常 | 该样本判 `execution_failed`，不计入指标分母 | 重跑该样本子集 |
| 指标 | 引用摸不到 chunk | 该项记幻觉；引用准确率下降 | 回溯 index_version 是否重建 |
| 基线比对 | 基线 git SHA 缺失 | 评分卡判不合格 | 补齐元信息再跑 |

回归对齐：任何调参后新跑评测，基线必须是上一已冻结 `eval_result`，且 `dataset_version` 必须相同；否则输出的 delta 标记 `uncomparable`。

## 7. 隐私与权限

- 评测请求落 `AiEvent`，不含完整 prompt，仅记 `sample_id`、耗时、状态；
- 指标 `record` 只含指标，不含原文；
- 若要排查看具体错例，需检查 `sample_id` -> 本地受限目录查看，需评测负责人授权；
- 报告导出走 `audit("eval.report.export")`，内容过二次脱敏扫描；
- 评测集对研发隐藏 `expected_*` 字段副本，防止调参过度拟合。

## 8. 测试与验证

- schema 校验的正反测试；
- 指标纯函数针对 TP/FP/FN/TN 边界的单元测试；
- 六类场景各 1 条样例端到端跑通，锁定核心路径；
- 混淆矩阵手工核对样本（3 条）确保判定一致；
- 报告生成在缺元信息时付费，保持"评分卡判不合格"行为。