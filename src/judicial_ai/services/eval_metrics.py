"""评测集 schema、脱敏门禁与业务质量指标纯函数（`biz-quality-eval` T1/T2/T5/T6/T7）。

设计口径见 `specs/biz-quality-eval/design.md` §2–§4。本模块全部为纯函数，不含
数据库、网络与第三方依赖，便于独立验证与回归固定。

合规约束（`judicial-domain`）：
- 评测样本只能来自合成或经授权脱敏的语料；
- 任何指标输出禁止携带原文、当事人信息、真实案号；
- 依据缺失时结论必须落在"待人工裁决"，不得静默判为正确。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..text_utils import bound_text
from .governance_pipeline import contains_sensitive

# ---------------------------------------------------------------------------
# 常数与枚举
# ---------------------------------------------------------------------------

SCENARIOS: tuple[str, ...] = (
    "common",
    "hard",
    "no_answer",
    "outdated_basis",
    "permission_bounded",
    "conflicting",
)

ANSWER_TYPES: tuple[str, ...] = ("answer", "abstain")
SENSITIVITY_LABELS: tuple[str, ...] = ("INTERNAL", "CONFIDENTIAL")

KAPPA_FLOOR = 0.70
CONTIGUITY_GAP_CHARS = 200
QUESTION_MAX_CHARS = 500
DIFFICULTY_RANGE = (1, 5)

# 拒答判定：命中任一模式即视为系统选择了拒答（与 refuse_flag 互为独立冗余）
ABSTAIN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"无法提供|不能提供|无法依据|依据不足|需要人工复核|建议人工复核"),
    re.compile(r"无(法|可)(确定|认定|判断)"),
    re.compile(r"^[。．!！?？\s]*(?:抱歉|对不起|很抱歉)[。．!！?？\s]*$"),
)


# ---------------------------------------------------------------------------
# 数据集 schema（T1）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SampleRecord:
    """评测样本的规范形态。字段含义见设计 §2。"""

    sample_id: str
    scenario: str
    question: str
    expected_answer_type: str
    abstain_required: bool
    difficulty_score: int
    annotated_refs: tuple[str, ...] = ()
    expected_citations: tuple[str, ...] = ()
    sensitivity_label: str = "INTERNAL"
    redaction_rule_version: str = ""


@dataclass(frozen=True)
class SchemaReport:
    """schema 校验结果：`errors` 非空即拒绝入库。"""

    ok: bool
    errors: tuple[str, ...] = ()


_REQUIRED_FIELDS = (
    "sample_id",
    "scenario",
    "question",
    "expected_answer_type",
    "abstain_required",
    "annotated_refs",
    "expected_citations",
    "difficulty_score",
    "sensitivity_label",
)


def validate_sample(sample: Mapping[str, Any]) -> SchemaReport:
    """按设计 §2 校验单条样本。返回错误清单，不抛异常。"""
    errors: list[str] = []
    for name in _REQUIRED_FIELDS:
        if name not in sample:
            errors.append(f"missing_field:{name}")
    if errors:
        return SchemaReport(False, tuple(errors))

    sample_id = sample.get("sample_id")
    if not isinstance(sample_id, str) or not sample_id.strip():
        errors.append("invalid_field:sample_id")

    scenario = sample.get("scenario")
    if scenario not in SCENARIOS:
        errors.append(f"invalid_field:scenario:{scenario!r}")

    question = sample.get("question")
    if not isinstance(question, str) or not question.strip():
        errors.append("invalid_field:question")
    elif len(question) > QUESTION_MAX_CHARS:
        errors.append(f"invalid_field:question_too_long:{len(question)}")

    answer_type = sample.get("expected_answer_type")
    if answer_type not in ANSWER_TYPES:
        errors.append(f"invalid_field:expected_answer_type:{answer_type!r}")

    abstain_required = sample.get("abstain_required")
    if not isinstance(abstain_required, bool):
        errors.append("invalid_field:abstain_required")
    elif (answer_type == "abstain") != abstain_required:
        errors.append("inconsistent:expected_answer_type_vs_abstain_required")

    difficulty = sample.get("difficulty_score")
    low, high = DIFFICULTY_RANGE
    if not isinstance(difficulty, int) or isinstance(difficulty, bool):
        errors.append("invalid_field:difficulty_score")
    elif not low <= difficulty <= high:
        errors.append(f"invalid_field:difficulty_score_out_of_range:{difficulty}")

    for name in ("annotated_refs", "expected_citations"):
        value = sample.get(name)
        if not isinstance(value, (list, tuple)):
            errors.append(f"invalid_field:{name}")
        elif any(not isinstance(item, str) or not item.strip() for item in value):
            errors.append(f"invalid_field:{name}_entries")

    label = sample.get("sensitivity_label")
    if label not in SENSITIVITY_LABELS:
        errors.append(f"invalid_field:sensitivity_label:{label!r}")

    return SchemaReport(not errors, tuple(errors))


def validate_scenario_distribution(
    samples: Iterable[Mapping[str, Any]], *, min_per_scenario: int = 20
) -> SchemaReport:
    """六类场景全覆盖且每类样本数达下限。"""
    counts = dict.fromkeys(SCENARIOS, 0)
    for sample in samples:
        scenario = sample.get("scenario")
        if scenario in counts:
            counts[scenario] += 1
    deficits = [f"{name}:{counts[name]}" for name in SCENARIOS if counts[name] < min_per_scenario]
    if deficits:
        return SchemaReport(False, tuple(f"scenario_shortage:{part}" for part in deficits))
    return SchemaReport(True)


def redaction_violations(text: str) -> bool:
    """脱敏门禁（T2）：命中敏感模式或长度越界即拒绝入库。"""
    bounded, truncated = bound_text(text or "")
    if truncated:
        return True
    return contains_sensitive(bounded)


# ---------------------------------------------------------------------------
# 引用保真（T6，设计 §4.2）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkSpan:
    """检索片段及其在原文中的位置，用于连续性判定与幻觉识别。"""

    chunk_ref: str
    doc_id: str
    offset_start: int
    offset_end: int
    text: str = ""


_CITATION_REF = re.compile(r"\A(?P<doc>[^#]+)#(?P<chunk>.+)\Z")


def parse_chunk_ref(citation_id: str) -> tuple[str, str] | None:
    """把 `doc-12#chunk-3` 拆成 `(doc_id, chunk_id)`；无法识别则返 None。"""
    match = _CITATION_REF.match((citation_id or "").strip())
    if not match:
        return None
    return match.group("doc"), match.group("chunk")


def is_contiguous(left: ChunkSpan, right: ChunkSpan, gap: int = CONTIGUITY_GAP_CHARS) -> bool:
    """同文档内两 chunk 起止偏移距离 ≤ gap 视为连续。"""
    if left.doc_id != right.doc_id:
        return False
    gap_left = max(left.offset_start - right.offset_end, right.offset_start - left.offset_end, 0)
    return gap_left <= gap


@dataclass
class CitationFidelity:
    total: int = 0
    valid: int = 0
    hallucinated: int = 0
    """引用指向了 `index_version` 之外不存在的 chunk。"""
    unverified: int = 0
    """引用存在但无法证明其支持结论——不得判为正确。"""

    @property
    def accuracy(self) -> float:
        """引用准确率 = 有效引用数 / 生成式给出的引用总数。"""
        return self.valid / self.total if self.total else 0.0


def citation_fidelity(
    generated_refs: Sequence[str], known_chunks: Mapping[str, ChunkSpan]
) -> CitationFidelity:
    """比对生成式引用与真实检索片段集合。

    - 引用 ID 不在 `known_chunks`：幻觉（`hallucinated`）；
    - 引用 ID 存在但片段正文为空或缺位置：无法核验（`unverified`），**不计为有效**；
    - 其余计 `valid`。
    """
    result = CitationFidelity()
    for citation_id in generated_refs:
        result.total += 1
        span = known_chunks.get(citation_id)
        if span is None:
            result.hallucinated += 1
            continue
        if not span.text or span.offset_end < span.offset_start or span.offset_end < 0:
            result.unverified += 1
            continue
        result.valid += 1
    return result


# ---------------------------------------------------------------------------
# 拒答判定（设计 §4.2 第 3/4 条）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AbstainVerdict:
    refused: bool
    source: str  # "pattern" | "flag" | "none"


def detect_abstain(prediction: str, *, refuse_flag: bool | None = None) -> AbstainVerdict:
    """双路径判定：预置模式 + 模型 `refuse_flag`，任一命中即拒答。"""
    if refuse_flag is True:
        return AbstainVerdict(True, "flag")
    text, _ = bound_text(prediction or "")
    for pattern in ABSTAIN_PATTERNS:
        if pattern.search(text):
            return AbstainVerdict(True, "pattern")
    return AbstainVerdict(False, "none")


@dataclass
class AbstainConfusion:
    tn: int = 0
    """应拒答且拒答。"""
    fp: int = 0
    """应拒答却给答案 → 假阳。"""
    fn: int = 0
    """应答却拒答 → 漏答。"""
    tp: int = 0
    """应答且给答案。"""

    @property
    def abstain_accuracy(self) -> float:
        """拒答准确率 = TN / (TN + FP)。"""
        denominator = self.tn + self.fp
        return self.tn / denominator if denominator else 0.0

    @property
    def answer_accuracy(self) -> float:
        denominator = self.tp + self.fn
        return self.tp / denominator if denominator else 0.0

    @property
    def total(self) -> int:
        return self.tn + self.fp + self.fn + self.tp


def accumulate_abstain(
    confusion: AbstainConfusion, *, abstain_required: bool, refused: bool
) -> None:
    if abstain_required:
        if refused:
            confusion.tn += 1
        else:
            confusion.fp += 1
        return
    if refused:
        confusion.fn += 1
    else:
        confusion.tp += 1


# ---------------------------------------------------------------------------
# 检索误报漏报（设计 §4.1）
# ---------------------------------------------------------------------------


@dataclass
class RetrievalErrors:
    fp: int = 0
    """检索命中但与标注无关或引用无效。"""
    fn: int = 0
    """标注相关但未进 top-k。"""

    @property
    def total(self) -> int:
        return self.fp + self.fn


def retrieval_errors(relevant_ids: Sequence[str], retrieved_ids: Sequence[str], k: int = 10):
    relevant = set(relevant_ids)
    top_k = list(retrieved_ids)[:k]
    hits = [chunk_id for chunk_id in top_k if chunk_id in relevant]
    return RetrievalErrors(
        fp=len([chunk_id for chunk_id in top_k if chunk_id not in relevant]),
        fn=len(relevant) - len(hits),
    )


# ---------------------------------------------------------------------------
# 标注一致性（设计 §3）
# ---------------------------------------------------------------------------


def cohen_kappa(left: Sequence[str], right: Sequence[str]) -> float | None:
    """Cohen's kappa；长度不一致或单侧全空时返回 None（不可比）。"""
    if len(left) != len(right) or not left:
        return None
    labels = sorted(set(left) | set(right))
    if not labels:
        return None
    n = len(left)
    agree = sum(1 for a, b in zip(left, right, strict=True) if a == b)
    observed = agree / n

    expected = 0.0
    for label in labels:
        p_left = sum(1 for item in left if item == label) / n
        p_right = sum(1 for item in right if item == label) / n
        expected += p_left * p_right
    if math.isclose(expected, 1.0):
        return 1.0
    return round((observed - expected) / (1.0 - expected), 6)


def kappa_requires_adjudication(kappa: float | None, threshold: float = KAPPA_FLOOR) -> bool:
    """未达门槛即流转法律专家仲裁。"""
    if kappa is None:
        return True
    return kappa < threshold


# ---------------------------------------------------------------------------
# 版本冻结与对比（T3，设计 AC-1.1 / AC-1.2 / AC-1.3）
# ---------------------------------------------------------------------------


def compute_content_hash(
    samples: Sequence[Mapping[str, Any]], *, redaction_rule_version: str = ""
) -> str:
    """对样本集计算确定性内容哈希，顺序无关。

    哈希只覆盖规范化的字段值，不落原文到任何持久化位置以外；换人重标后
    若字段值不变则哈希一致，可据此判断两个版本是否同源（AC-1.1）。
    """
    rows = []
    for sample in samples:
        sample_id = str(sample.get("sample_id") or sample.get("id") or "")
        rows.append(
            (
                sample_id,
                str(sample.get("scenario") or ""),
                str(sample.get("question") or sample.get("query") or ""),
                str(sample.get("expected_answer_type") or ""),
                bool(sample.get("abstain_required", False)),
                int(sample.get("difficulty_score") or 0),
                tuple(
                    sorted(
                        str(item)
                        for item in sample.get("expected_citations")
                        or sample.get("relevant_chunk_ids")
                        or ()
                    )
                ),
            )
        )
    rows.sort(key=lambda row: row[0])
    payload = {"rule": redaction_rule_version, "rows": rows}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class VersionDiff:
    """两个冻结版本之间的增量（AC-1.3）。"""

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    redaction_rule_version_changed: bool = False

    @property
    def empty(self) -> bool:
        return not (
            self.added or self.removed or self.modified or self.redaction_rule_version_changed
        )


def dataset_version_diff(
    left: Sequence[Mapping[str, Any]],
    right: Sequence[Mapping[str, Any]],
    *,
    left_rule_version: str = "",
    right_rule_version: str = "",
) -> VersionDiff:
    """输出增量样本清单与脱敏规则版本差异，只输出 ID 不输出原文。"""
    left_index = {str(item.get("sample_id") or item.get("id") or ""): item for item in left}
    right_index = {str(item.get("sample_id") or item.get("id") or ""): item for item in right}

    added = tuple(sorted(set(right_index) - set(left_index)))
    removed = tuple(sorted(set(left_index) - set(right_index)))
    modified = []
    for sample_id in sorted(set(left_index) & set(right_index)):
        left_hash = compute_content_hash(
            [left_index[sample_id]], redaction_rule_version=left_rule_version
        )
        right_hash = compute_content_hash(
            [right_index[sample_id]], redaction_rule_version=left_rule_version
        )
        if left_hash != right_hash:
            modified.append(sample_id)

    return VersionDiff(
        added=added,
        removed=removed,
        modified=tuple(modified),
        redaction_rule_version_changed=left_rule_version != right_rule_version,
    )


# ---------------------------------------------------------------------------
# 分桶汇总
# ---------------------------------------------------------------------------


@dataclass
class SampleOutcome:
    """单样本评测结果。严禁携带原文（设计 §7）。"""

    sample_id: str
    scenario: str
    difficulty_score: int
    recall_at_k: float = 0.0
    citation_total: int = 0
    citation_valid: int = 0
    abstain_required: bool = False
    refused: bool = False
    execution_failed: bool = False
    latency_ms: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


def bucket_key(scenario: str) -> str:
    return scenario if scenario in SCENARIOS else "unknown"


def bucket_by_scenario(outcomes: Sequence[SampleOutcome]) -> dict[str, dict[str, float]]:
    buckets: dict[str, dict[str, float]] = {}
    grouped: dict[str, list[SampleOutcome]] = {}
    for outcome in outcomes:
        grouped.setdefault(bucket_key(outcome.scenario), []).append(outcome)
    for name, rows in grouped.items():
        buckets[name] = _bucket_metrics(rows)
    return buckets


def bucket_by_difficulty(outcomes: Sequence[SampleOutcome]) -> dict[str, dict[str, float]]:
    buckets: dict[str, dict[str, float]] = {}
    grouped: dict[str, list[SampleOutcome]] = {}
    for outcome in outcomes:
        score = outcome.difficulty_score
        label = str(score) if 1 <= score <= 5 else "unknown"
        grouped.setdefault(label, []).append(outcome)
    for name, rows in grouped.items():
        buckets[name] = _bucket_metrics(rows)
    return buckets


def _bucket_metrics(rows: Sequence[SampleOutcome]) -> dict[str, float]:
    scored = [row for row in rows if not row.execution_failed]
    confusion = AbstainConfusion()
    citations_total = 0
    citations_valid = 0
    recall_sum = 0.0
    latency_sum = 0.0
    for row in scored:
        accumulate_abstain(confusion, abstain_required=row.abstain_required, refused=row.refused)
        citations_total += row.citation_total
        citations_valid += row.citation_valid
        recall_sum += row.recall_at_k
        latency_sum += row.latency_ms
    count = len(scored)
    return {
        "sample_count": float(len(rows)),
        "execution_failed_count": float(len(rows) - count),
        "recall_at_k": round(recall_sum / count, 6) if count else 0.0,
        "citation_accuracy": (
            round(citations_valid / citations_total, 6) if citations_total else 0.0
        ),
        "abstain_accuracy": round(confusion.abstain_accuracy, 6),
        "tn": float(confusion.tn),
        "fp": float(confusion.fp),
        "fn": float(confusion.fn),
        "tp": float(confusion.tp),
        "latency_mean_ms": round(latency_sum / count, 6) if count else 0.0,
    }


def aggregate_metrics(outcomes: Sequence[SampleOutcome]) -> dict[str, float]:
    return _bucket_metrics([row for row in outcomes])


# ---------------------------------------------------------------------------
# 评分卡与环比（T10，设计 §5 报告结构 / §6 回归对齐）
# ---------------------------------------------------------------------------

# 冻结门槛（`judicial-ai-hardening-gap-2026-11` 用户口径，签字依据）
FROZEN_GATES: dict[str, float] = {
    "recall_at_k": 0.90,
    "citation_accuracy": 0.95,
    "abstain_accuracy": 0.90,
}

# 缺元信息即判不合格的必填字段（§6「基线 git SHA 缺失→评分卡判不合格」）
REQUIRED_META: tuple[str, ...] = (
    "git_sha",
    "dataset_version",
    "index_version",
    "prompt_version",
    "model_version",
    "migrations_head",
    "runner_identity",
    "executed_at",
)


@dataclass(frozen=True)
class ScoreCard:
    grade: str  # PASS | FAIL | INCOMPARABLE
    passed: bool
    gates: tuple[tuple[str, float, float, bool], ...] = ()
    """(指标名, 实测值, 门槛, 是否达标)"""
    missing_meta: tuple[str, ...] = ()
    score: float = 0.0


def evaluate_scorecard(
    metrics: Mapping[str, float],
    meta: Mapping[str, Any],
    *,
    gates: Mapping[str, float] | None = None,
) -> ScoreCard:
    """对齐冻结门槛出评分卡；缺元信息必判不合格（不做「凑合过」）。"""
    thresholds = dict(gates or FROZEN_GATES)
    missing = tuple(name for name in REQUIRED_META if not meta.get(name))
    rows: list[tuple[str, float, float, bool]] = []
    ok = True
    for name, threshold in thresholds.items():
        observed = float(metrics.get(name, 0.0))
        passed = observed >= threshold
        ok = ok and passed
        rows.append((name, observed, threshold, passed))
    grade = "PASS" if (ok and not missing) else "FAIL"
    total = sum(observed for _name, observed, _threshold, _ok in rows)
    return ScoreCard(
        grade=grade,
        passed=grade == "PASS",
        gates=tuple(rows),
        missing_meta=missing,
        score=round(total / len(rows), 6) if rows else 0.0,
    )


def compare_to_baseline(
    metrics: Mapping[str, float],
    baseline: Mapping[str, float],
    *,
    meta_same_dataset: bool,
    delta_threshold: float = 0.0,
) -> dict[str, Any]:
    """环比对比。基线必须同 `dataset_version`，否则标 `uncomparable`（§6）。"""
    if not meta_same_dataset:
        return {
            "uncomparable": True,
            "reason": "dataset_version_mismatch",
            "deltas": {},
        }
    deltas: dict[str, dict[str, float | bool]] = {}
    regressed = False
    for name, value in metrics.items():
        if name not in baseline:
            continue
        delta = float(value) - float(baseline[name])
        worse = bool(delta < 0 and abs(delta) > delta_threshold)
        regressed = regressed or worse
        deltas[name] = {
            "current": round(float(value), 6),
            "baseline": round(float(baseline[name]), 6),
            "delta": round(delta, 6),
            "regressed": worse,
        }
    return {"uncomparable": False, "regressed": regressed, "deltas": deltas}