"""`biz-quality-eval` 指标纯函数单测（T1/T2/T5/T6，设计 §4/§8）。

覆盖设计 §8 点名的混淆矩阵边界、引用幻觉与连续性、拒答双路径、
schema 正反例与脱敏门禁。全部为纯函数测试，不需要数据库。

样本一律使用合成司法语料，不含真实当事人、真实案号或真实裁判材料。
"""

from __future__ import annotations

import pytest

from judicial_ai.services import eval_metrics as em
from judicial_ai.services.governance_pipeline import retrieval_metrics

# ---------------------------------------------------------------------------
# T1：schema 校验
# ---------------------------------------------------------------------------


def _sample(**overrides):
    base = {
        "sample_id": "eval-sample-000123",
        "scenario": "common",
        "question": "民间借贷利率上限如何认定？",
        "expected_answer_type": "answer",
        "abstain_required": False,
        "difficulty_score": 3,
        "annotated_refs": ["synth-doc-1#chunk-3"],
        "expected_citations": ["synth-doc-1#chunk-3", "synth-doc-1#chunk-4"],
        "sensitivity_label": "INTERNAL",
        "redaction_rule_version": "redaction-v1",
    }
    base.update(overrides)
    return base


def test_validate_sample_accepts_wellformed():
    report = em.validate_sample(_sample())
    assert report.ok is True
    assert report.errors == ()


def test_validate_sample_rejects_missing_required_field():
    sample = _sample()
    del sample["sample_id"]
    report = em.validate_sample(sample)
    assert report.ok is False
    assert "missing_field:sample_id" in report.errors


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"scenario": "unknown_scenario"}, "invalid_field:scenario"),
        ({"expected_answer_type": "refuse"}, "invalid_field:expected_answer_type"),
        ({"difficulty_score": 9}, "difficulty_score_out_of_range"),
        ({"difficulty_score": True}, "invalid_field:difficulty_score"),
        ({"sensitivity_label": "TOP_SECRET"}, "sensitivity_label"),
    ],
)
def test_validate_sample_rejects_enum_and_range_violations(overrides, expected):
    report = em.validate_sample(_sample(**overrides))
    assert report.ok is False
    assert any(expected in error for error in report.errors)


def test_validate_sample_flags_abstain_inconsistency():
    # AC-2.2 的前置：expected_answer_type 与 abstain_required 必须自洽
    report = em.validate_sample(_sample(expected_answer_type="abstain", abstain_required=False))
    assert report.ok is False
    assert "inconsistent:expected_answer_type_vs_abstain_required" in report.errors


def test_validate_sample_rejects_overlong_question():
    report = em.validate_sample(_sample(question="问" * 600))
    assert report.ok is False
    assert any(error.startswith("invalid_field:question_too_long") for error in report.errors)


def test_validate_scenario_distribution_requires_all_six():
    samples = [_sample(scenario="common") for _ in range(25)]
    report = em.validate_scenario_distribution(samples, min_per_scenario=20)
    assert report.ok is False
    assert any("scenario_shortage" in error for error in report.errors)


def test_validate_scenario_distribution_passes_when_all_six_covered():
    samples = [
        _sample(sample_id=f"s-{scenario}-{index}", scenario=scenario)
        for scenario in em.SCENARIOS
        for index in range(20)
    ]
    report = em.validate_scenario_distribution(samples, min_per_scenario=20)
    assert report.ok is True


# ---------------------------------------------------------------------------
# T2：脱敏门禁
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "联系人张三，身份证号 410101199001011234，委托代理人李四。",
        "当事人邮箱 contact@example.invalid 已提交证据。",
        "该案号为（2025）京01民终123号，请核对。",
    ],
)
def test_redaction_gate_blocks_sensitive_corpus(text):
    assert em.redaction_violations(text) is True


def test_redaction_gate_allows_clean_synthetic_corpus():
    clean = "本条规范的是合法借贷行为中利率上限的认定方法，仅作合成语料示例。"
    assert em.redaction_violations(clean) is False


# ---------------------------------------------------------------------------
# T6：引用保真（设计 §4.2）
# ---------------------------------------------------------------------------


def _span(ref, doc="synth-1", start=0, end=80, text="合成正文片段"):
    return em.ChunkSpan(chunk_ref=ref, doc_id=doc, offset_start=start, offset_end=end, text=text)


def test_parse_chunk_ref_handles_document_and_chunk_parts():
    assert em.parse_chunk_ref("doc-12#chunk-3") == ("doc-12", "chunk-3")
    assert em.parse_chunk_ref("not-a-ref") is None
    assert em.parse_chunk_ref("  ") is None


def test_citation_accuracy_counts_all_valid_refs():
    known = {
        "synth-1#chunk-1": _span("synth-1#chunk-1"),
        "synth-1#chunk-2": _span("synth-1#chunk-2", start=200, end=280),
    }
    result = em.citation_fidelity(["synth-1#chunk-1", "synth-1#chunk-2"], known)
    assert result.total == 2
    assert result.valid == 2
    assert result.accuracy == 1.0
    assert result.hallucinated == 0


def test_citation_marks_unknown_chunk_as_hallucination():
    known = {"synth-1#chunk-1": _span("synth-1#chunk-1")}
    result = em.citation_fidelity(["synth-1#chunk-1", "synth-9#chunk-9"], known)
    assert result.total == 2
    assert result.valid == 1
    assert result.hallucinated == 1
    assert result.accuracy == 0.5


def test_citation_refuses_to_credit_unverifiable_span():
    # 引用 ID 存在但正文/位置不可核验 → 不得计为有效（judicial-domain：不得以
    # 生成内容伪装依据，缺失证据即待人工裁决）
    empty = em.ChunkSpan(
        chunk_ref="synth-1#chunk-1", doc_id="synth-1", offset_start=0, offset_end=10, text=""
    )
    inverted = em.ChunkSpan(
        chunk_ref="synth-1#chunk-2", doc_id="synth-1", offset_start=50, offset_end=-5, text="x"
    )
    result = em.citation_fidelity(
        ["synth-1#chunk-1", "synth-1#chunk-2"],
        {"synth-1#chunk-1": empty, "synth-1#chunk-2": inverted},
    )
    assert result.valid == 0
    assert result.unverified == 2
    assert result.accuracy == 0.0


def test_citation_accuracy_zero_when_no_citations_generated():
    assert em.citation_fidelity([], {}).accuracy == 0.0
    assert em.CitationFidelity().accuracy == 0.0


def test_continuity_requires_same_document_and_bounded_gap():
    left = _span("synth-1#chunk-1", doc="synth-1", start=0, end=100)
    adjacent = _span("synth-1#chunk-2", doc="synth-1", start=150, end=240)
    far = _span("synth-1#chunk-3", doc="synth-1", start=1000, end=1100)
    other_doc = _span("synth-2#chunk-1", doc="synth-2", start=0, end=100)

    assert em.is_contiguous(left, adjacent) is True
    assert em.is_contiguous(left, far) is False
    assert em.is_contiguous(left, other_doc) is False


# ---------------------------------------------------------------------------
# 拒答双路径与混淆矩阵（设计 §4.2 第 3/4 条）
# ---------------------------------------------------------------------------


def test_detect_abstain_via_pattern_and_flag_are_independent():
    assert em.detect_abstain("依据不足，建议人工复核。").refused is True
    assert em.detect_abstain("这是一条正常结论。", refuse_flag=True).refused is True
    assert em.detect_abstain("这是一条正常结论。").refused is False
    assert em.detect_abstain("", refuse_flag=None).refused is False


def test_refuse_flag_wins_even_when_text_reads_as_conclusion():
    verdict = em.detect_abstain("因此应予支持。", refuse_flag=True)
    assert verdict.refused is True
    assert verdict.source == "flag"


@pytest.mark.parametrize(
    "abstain_required,refused,expected",
    [
        (True, True, "tn"),
        (True, False, "fp"),
        (False, True, "fn"),
        (False, False, "tp"),
    ],
)
def test_confusion_matrix_covers_all_four_cells(abstain_required, refused, expected):
    confusion = em.AbstainConfusion()
    em.accumulate_abstain(confusion, abstain_required=abstain_required, refused=refused)
    assert getattr(confusion, expected) == 1
    assert confusion.total == 1


def test_abstain_accuracy_formula_matches_spec():
    # 设计 §4.1：拒答准确率 = TN / (TN + FP)
    confusion = em.AbstainConfusion(tn=9, fp=1, fn=2, tp=8)
    assert confusion.abstain_accuracy == pytest.approx(0.9)
    assert confusion.answer_accuracy == pytest.approx(0.8)


def test_abstain_accuracy_is_zero_when_no_relevant_denominator():
    assert em.AbstainConfusion().abstain_accuracy == 0.0
    assert em.AbstainConfusion(fn=3, tp=2).abstain_accuracy == 0.0


def test_false_positive_is_unanswered_sample_getting_an_answer():
    # AC-2.2：无答案样本被判为拒答以外的任何结论都算拒答错误（假阳）
    confusion = em.AbstainConfusion()
    em.accumulate_abstain(confusion, abstain_required=True, refused=False)
    assert confusion.fp == 1
    assert confusion.abstain_accuracy == 0.0


# ---------------------------------------------------------------------------
# 检索误报漏报
# ---------------------------------------------------------------------------


def test_retrieval_errors_counts_fp_and_fn():
    relevant = ["c1", "c2", "c3"]
    retrieved = ["c1", "c4", "c5"]
    errors = em.retrieval_errors(relevant, retrieved, k=10)
    assert errors.fn == 2
    assert errors.fp == 2


def test_retrieval_errors_perfect_match():
    errors = em.retrieval_errors(["c1"], ["c1", "c2"], k=1)
    assert errors.fn == 0
    assert errors.fp == 0


def test_retrieval_errors_respects_top_k_window():
    # 标注相关但落在 top-10 之外 → 漏报
    relevant = ["c-far"]
    retrieved = [f"c-{index}" for index in range(10)] + ["c-far"]
    errors = em.retrieval_errors(relevant, retrieved, k=10)
    assert errors.fn == 1


# ---------------------------------------------------------------------------
# 标注一致性（设计 §3 §8）
# ---------------------------------------------------------------------------


def test_kappa_perfect_agreement_is_one():
    labels = ["relevant", "relevant", "irrelevant", "irrelevant"]
    assert em.cohen_kappa(labels, labels) == pytest.approx(1.0)


def test_kappa_chance_level_agreement_near_zero():
    left = ["a", "a", "b", "b"]
    right = ["a", "b", "a", "b"]
    kappa = em.cohen_kappa(left, right)
    assert kappa is not None
    assert kappa == pytest.approx(0.0, abs=1e-6)


def test_kappa_returns_none_when_incomparable():
    assert em.cohen_kappa([], []) is None
    assert em.cohen_kappa(["a"], ["a", "b"]) is None


def test_kappa_below_floor_triggers_adjudication():
    left = ["a"] * 50 + ["b"] * 50
    right = ["a"] * 40 + ["b"] * 10 + ["a"] * 10 + ["b"] * 40
    kappa = em.cohen_kappa(left, right)
    assert kappa is not None
    assert em.kappa_requires_adjudication(kappa) is (kappa < em.KAPPA_FLOOR)


def test_kappa_none_always_requires_adjudication():
    assert em.kappa_requires_adjudication(None) is True


# ---------------------------------------------------------------------------
# 版本冻结（AC-1.1 / AC-1.2 / AC-1.3）
# ---------------------------------------------------------------------------

_FULL_META = {
    "git_sha": "abc1234",
    "dataset_version": "ds-v1",
    "index_version": "idx-1",
    "prompt_version": "prompt-1",
    "model_version": "model-1",
    "migrations_head": "0011",
    "runner_identity": "admin",
    "executed_at": "2026-11-05T10:00:00Z",
}


def _draft_sample(**overrides):
    data = {
        "sample_id": "eval-sample-000001",
        "scenario": "common",
        "question": "民间借贷利率上限如何认定？",
        "expected_answer_type": "answer",
        "abstain_required": False,
        "difficulty_score": 3,
        "expected_citations": ["synth-doc-1#chunk-3", "synth-doc-1#chunk-4"],
    }
    data.update(overrides)
    return data


def test_content_hash_is_order_independent():
    samples = [
        _draft_sample(sample_id=f"s-{index}", question=f"问题 {index}") for index in range(3)
    ]
    reversed_samples = list(reversed(samples))
    assert em.compute_content_hash(samples) == em.compute_content_hash(reversed_samples)


def test_content_hash_changes_when_any_field_changes():
    original = em.compute_content_hash([_draft_sample()])
    modified = em.compute_content_hash([_draft_sample(question="另一个问题")])
    assert original != modified


def test_content_hash_varies_with_redaction_rule_version():
    samples = [_draft_sample()]
    assert em.compute_content_hash(samples, redaction_rule_version="v1") != em.compute_content_hash(
        samples, redaction_rule_version="v2"
    )


def test_content_hash_ignores_expected_citations_order():
    left = em.compute_content_hash([_draft_sample(expected_citations=["a", "b"])])
    right = em.compute_content_hash([_draft_sample(expected_citations=["b", "a"])])
    assert left == right


def test_version_diff_reports_adds_removes_modifies():
    left = [_draft_sample(sample_id="s-1"), _draft_sample(sample_id="s-2")]
    right = [
        _draft_sample(sample_id="s-2", difficulty_score=5),
        _draft_sample(sample_id="s-3"),
    ]
    diff = em.dataset_version_diff(left, right, left_rule_version="v1", right_rule_version="v1")
    assert diff.added == ("s-3",)
    assert diff.removed == ("s-1",)
    assert diff.modified == ("s-2",)
    assert diff.empty is False
    assert diff.redaction_rule_version_changed is False


def test_version_diff_with_same_content_is_empty():
    original = [_draft_sample()]
    diff = em.dataset_version_diff(original, list(original))
    assert diff.empty is True


def test_version_diff_reports_redaction_rule_version_change():
    diff = em.dataset_version_diff(
        [_draft_sample()],
        [_draft_sample()],
        left_rule_version="redaction-v1",
        right_rule_version="redaction-v2",
    )
    assert diff.redaction_rule_version_changed is True


# ---------------------------------------------------------------------------
# 评分卡与环比（T10）
# ---------------------------------------------------------------------------


def test_scorecard_passes_when_all_frozen_thresholds_met():
    metrics = {"recall_at_k": 0.91, "citation_accuracy": 0.96, "abstain_accuracy": 0.92}
    card = em.evaluate_scorecard(metrics, _FULL_META)
    assert card.grade == "PASS"
    assert card.passed is True
    assert card.missing_meta == ()


def test_scorecard_fails_when_any_gate_missed():
    metrics = {"recall_at_k": 0.99, "citation_accuracy": 0.99, "abstain_accuracy": 0.80}
    card = em.evaluate_scorecard(metrics, _FULL_META)
    assert card.grade == "FAIL"
    assert card.passed is False
    assert [name for name, _obs, _thr, ok in card.gates if not ok] == ["abstain_accuracy"]


def test_scorecard_fails_when_meta_incomplete_even_if_metrics_high():
    incomplete = {key: value for key, value in _FULL_META.items() if key != "git_sha"}
    card = em.evaluate_scorecard(
        {"recall_at_k": 0.99, "citation_accuracy": 0.99, "abstain_accuracy": 0.99}, incomplete
    )
    assert card.grade == "FAIL"
    assert card.missing_meta == ("git_sha",)


@pytest.mark.parametrize("name", tuple(em.REQUIRED_META))
def test_each_missing_meta_field_blocks_pass(name):
    incomplete = {key: value for key, value in _FULL_META.items() if key != name}
    card = em.evaluate_scorecard(
        {"recall_at_k": 1.0, "citation_accuracy": 1.0, "abstain_accuracy": 1.0}, incomplete
    )
    assert card.passed is False
    assert name in card.missing_meta


def test_baseline_compare_marks_mismatched_dataset_uncomparable():
    delta = em.compare_to_baseline(
        {"recall_at_k": 0.95}, {"recall_at_k": 0.80}, meta_same_dataset=False
    )
    assert delta["uncomparable"] is True
    assert delta["deltas"] == {}


def test_baseline_compare_flag_regression_on_drop():
    delta = em.compare_to_baseline(
        {"recall_at_k": 0.85, "citation_accuracy": 1.0},
        {"recall_at_k": 0.90, "citation_accuracy": 0.99},
        meta_same_dataset=True,
    )
    assert delta["deltas"]["recall_at_k"]["regressed"] is True
    assert delta["deltas"]["citation_accuracy"]["regressed"] is False
    assert delta["regressed"] is True


def test_baseline_compare_improvement_not_flagged():
    delta = em.compare_to_baseline(
        {"recall_at_k": 0.95}, {"recall_at_k": 0.90}, meta_same_dataset=True
    )
    assert delta["regressed"] is False
    assert delta["deltas"]["recall_at_k"]["delta"] == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# 分桶汇总（设计 §5 报告结构）
# ---------------------------------------------------------------------------


def _outcome(**overrides):
    defaults = em.SampleOutcome(
        sample_id="s-1",
        scenario="common",
        difficulty_score=3,
        recall_at_k=1.0,
        citation_total=2,
        citation_valid=2,
        abstain_required=False,
        refused=False,
    )
    for name, value in overrides.items():
        setattr(defaults, name, value)
    return defaults


def test_execution_failed_samples_excluded_from_metric_denominator():
    rows = [
        _outcome(sample_id="ok-1"),
        _outcome(sample_id="bug-1", execution_failed=True, recall_at_k=0.0),
    ]
    aggregate = em.aggregate_metrics(rows)
    assert aggregate["sample_count"] == 2.0
    assert aggregate["execution_failed_count"] == 1.0
    # 只有 1 条计入 recall 分母，recall 必须是满分
    assert aggregate["recall_at_k"] == 1.0


def test_bucket_by_scenario_groups_only_known_scenarios():
    rows = [
        _outcome(scenario="common"),
        _outcome(scenario="no_answer", abstain_required=True, refused=True),
        _outcome(scenario="mystery"),
    ]
    buckets = em.bucket_by_scenario(rows)
    assert set(buckets) == {"common", "no_answer", "unknown"}
    assert buckets["no_answer"]["tn"] == 1.0


def test_bucket_by_difficulty_labels_five_buckets():
    rows = [_outcome(difficulty_score=score) for score in range(1, 6)]
    buckets = em.bucket_by_difficulty(rows)
    assert set(buckets) == {"1", "2", "3", "4", "5"}
    assert buckets["4"]["sample_count"] == 1.0


def test_aggregate_citation_accuracy_sums_over_all_citations():
    rows = [
        _outcome(sample_id="a", citation_total=3, citation_valid=3),
        _outcome(sample_id="b", citation_total=1, citation_valid=0),
    ]
    aggregate = em.aggregate_metrics(rows)
    assert aggregate["citation_accuracy"] == pytest.approx(0.75)


def test_aggregate_metrics_on_empty_set_is_safe():
    aggregate = em.aggregate_metrics([])
    assert aggregate["sample_count"] == 0.0
    assert aggregate["recall_at_k"] == 0.0
    assert aggregate["citation_accuracy"] == 0.0
    assert aggregate["abstain_accuracy"] == 0.0


# ---------------------------------------------------------------------------
# 与既有检索指标口径对齐（Recall@10 目标 ≥0.90）
# ---------------------------------------------------------------------------


def test_recall_at_k_from_existing_pipeline_matches_eval_expectation():
    metrics = retrieval_metrics(
        ["synth-1#chunk-3", "synth-1#chunk-4"],
        ["synth-1#chunk-3", "synth-1#chunk-4", "other#a"],
        k=10,
    )
    assert metrics["recall_at_k"] == 1.0


def test_sample_outcome_never_carries_corpus_text():
    outcome = _outcome()
    # 设计 §7：指标记录只含指标，不含原文
    assert all(
        isinstance(value, (str, int, float, bool, dict))
        for value in outcome.__dict__.values()
    )
    assert not any("当事人" in str(getattr(outcome, name, "")) for name in outcome.__dict__)