from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import Counter
from typing import Any

from ..text_utils import keyword_score, tokenize

PIPELINE_VERSION = "gov-pipeline-v1"
REDACTION_RULES_VERSION = "redaction-v1"

_HTML_TAG = re.compile(r"<[^>]{0,200}>")
_CONTROL_CHAR = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
_WHITESPACE = re.compile(r"[ \t\r\f\v]+")
_REPLACEMENT_CHAR = re.compile(r"\ufffd")
_CJK = re.compile(r"[\u4e00-\u9fff]")
_LATIN = re.compile(r"[a-zA-Z0-9]")

SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "id_card",
        re.compile(
            r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
            r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"
        ),
        "[身份证号]",
    ),
    (
        "bank_card",
        re.compile(r"(?<![A-Za-z0-9_])\d{16,19}(?![A-Za-z0-9_])"),
        "[银行账号]",
    ),
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[手机号]"),
    (
        "email",
        re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
        "[邮箱]",
    ),
    (
        "case_no",
        re.compile(r"[（(]\d{4}[）)][\u4e00-\u9fa5]{0,8}\d+[\u4e00-\u9fa5]{0,8}号"),
        "[案号]",
    ),
)

_ROLE_NAME = re.compile(
    r"(被告人|原告|被告|申请人|被申请人|上诉人|被上诉人|证人|"
    r"委托代理人|法定代表人|被害人)([\u4e00-\u9fa5]{2,3})"
)

_QA_MARKERS = ("法", "条", "款", "院", "判决", "裁定", "合同", "证据", "规定", "司法")


def clean_text(text: str) -> str:
    """清洗文本：去 HTML/控制字符、统一全半角、压缩空白。"""
    if not isinstance(text, str):
        text = str(text)
    text = unicodedata.normalize("NFKC", text)
    text = _HTML_TAG.sub(" ", text)
    text = _ZERO_WIDTH.sub("", text)
    text = _CONTROL_CHAR.sub("", text)
    text = _WHITESPACE.sub(" ", text)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def mask_sensitive(text: str) -> tuple[str, dict[str, int]]:
    """按规则脱敏，返回脱敏文本与命中计数。"""
    counts: dict[str, int] = {}
    masked = text
    for name, pattern, replacement in SENSITIVE_PATTERNS:
        masked, hit = pattern.subn(replacement, masked)
        if hit:
            counts[name] = counts.get(name, 0) + hit

    def _replace_name(match: re.Match[str]) -> str:
        return f"{match.group(1)}[姓名]"

    masked, hit = _ROLE_NAME.subn(_replace_name, masked)
    if hit:
        counts["person_name"] = counts.get("person_name", 0) + hit
    return masked, counts


def contains_sensitive(text: str) -> bool:
    if any(pattern.search(text) for _name, pattern, _replacement in SENSITIVE_PATTERNS):
        return True
    return _ROLE_NAME.search(text) is not None


def sanitize_text(
    text: str, *, redact: bool = True, max_chars: int = 8000
) -> tuple[str, dict[str, int]]:
    cleaned = clean_text(text)
    counts: dict[str, int] = {}
    if redact:
        cleaned, counts = mask_sensitive(cleaned)
    if len(cleaned) > max_chars:
        counts["truncated_chars"] = len(cleaned) - max_chars
        cleaned = cleaned[:max_chars]
    return cleaned, counts


def sanitize_payload(
    value: Any, *, redact: bool = True, max_chars: int = 8000
) -> tuple[Any, dict[str, int]]:
    """递归脱敏/清洗任意 JSON 值，返回 (新值, 命中统计)。"""
    if isinstance(value, str):
        return sanitize_text(value, redact=redact, max_chars=max_chars)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        counts: dict[str, int] = {}
        for key, item in value.items():
            sanitized, item_counts = sanitize_payload(
                item, redact=redact, max_chars=max_chars
            )
            result[str(key)] = sanitized
            for name, hit in item_counts.items():
                counts[name] = counts.get(name, 0) + hit
        return result, counts
    if isinstance(value, (list, tuple)):
        items: list[Any] = []
        counts = {}
        for item in value:
            sanitized, item_counts = sanitize_payload(
                item, redact=redact, max_chars=max_chars
            )
            items.append(sanitized)
            for name, hit in item_counts.items():
                counts[name] = counts.get(name, 0) + hit
        return items, counts
    return value, {}


def content_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def simhash(text: str, bits: int = 64) -> int:
    vector = [0] * bits
    for token, weight in Counter(tokenize(text)).items():
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        hashed = int.from_bytes(digest, "big")
        for index in range(bits):
            vector[index] += weight if (hashed >> index) & 1 else -weight
    result = 0
    for index, value in enumerate(vector):
        if value > 0:
            result |= 1 << index
    return result


def hamming_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def _ratio(pattern: re.Pattern[str], text: str) -> float:
    if not text:
        return 0.0
    return len(pattern.findall(text)) / len(text)


def quality_check(
    input_text: str,
    output_text: str,
    *,
    security_level: str = "INTERNAL",
    excluded_security_levels: tuple[str, ...] = (),
    has_citation: bool = False,
    citation_required: bool = False,
    business_score: float = 0.5,
    min_score: float = 0.6,
) -> tuple[float, bool, dict[str, Any]]:
    """规则 + 启发式质量评分。

    文档中的综合分包含人工复核分；此处为预审阶段，先按
    ``0.40 规则 + 0.35 模型启发式 + 0.15 引用 + 0.10 业务信号``
    计算，人工复核后可通过标注接口回写。
    """
    cjk_ratio = _ratio(_CJK, output_text)
    latin_ratio = _ratio(_LATIN, output_text)
    garbage_ratio = _ratio(_REPLACEMENT_CHAR, output_text)
    output_length = len(output_text.strip())

    rules = {
        "input_not_empty": bool(input_text.strip()),
        "output_not_empty": bool(output_text.strip()),
        "length_in_range": 2 <= output_length <= 20000 and len(input_text) <= 20000,
        "language_ok": bool(output_text) and (cjk_ratio >= 0.2 or latin_ratio >= 0.2),
        "no_garbage": garbage_ratio < 0.02,
        "no_residual_sensitive": not contains_sensitive(output_text),
        "citation_ok": has_citation or not citation_required,
        "security_ok": security_level not in excluded_security_levels,
    }
    passed_rules = sum(1 for value in rules.values() if value)
    rule_ratio = passed_rules / len(rules)

    completeness = min(output_length / 200.0, 1.0) if output_length else 0.0
    relevance = min(keyword_score(input_text, output_text) / 2.0, 1.0) if input_text else 0.0
    professional = 1.0 if any(marker in output_text for marker in _QA_MARKERS) else 0.5
    model_score = 0.45 * completeness + 0.35 * relevance + 0.20 * professional

    citation_score = 1.0 if (has_citation or not citation_required) else 0.0
    business = max(0.0, min(float(business_score), 1.0))
    score = 0.40 * rule_ratio + 0.35 * model_score + 0.15 * citation_score + 0.10 * business
    score = round(min(max(score, 0.0), 1.0), 6)

    detail = {
        "rules": rules,
        "rule_ratio": round(rule_ratio, 6),
        "model_score": round(model_score, 6),
        "citation_score": citation_score,
        "business_score": business,
        "quality_score": score,
        "passed": score >= min_score,
        "pipeline_version": PIPELINE_VERSION,
        "redaction_rules_version": REDACTION_RULES_VERSION,
    }
    return score, score >= min_score, detail


def review_status_for(score: float, auto_accept_score: float = 0.8) -> str:
    if score >= auto_accept_score:
        return "AUTO_ACCEPTED"
    return "NEEDS_REVIEW"


def answer_overlap(reference: str, prediction: str) -> dict[str, float]:
    reference_tokens = Counter(tokenize(reference))
    prediction_tokens = Counter(tokenize(prediction))
    if not reference_tokens or not prediction_tokens:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0}
    common = sum(
        min(count, prediction_tokens[token]) for token, count in reference_tokens.items()
    )
    precision = common / sum(prediction_tokens.values())
    recall = common / sum(reference_tokens.values())
    f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    return {
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
    }


def retrieval_metrics(
    relevant_ids: list[str], retrieved_ids: list[str], k: int
) -> dict[str, float]:
    relevant = set(relevant_ids)
    top_k = retrieved_ids[:k]
    hits = [chunk_id for chunk_id in top_k if chunk_id in relevant]
    recall = len(hits) / len(relevant) if relevant else 0.0
    precision_5 = (
        len([chunk_id for chunk_id in retrieved_ids[:5] if chunk_id in relevant]) / 5.0
    )
    reciprocal_rank = 0.0
    for rank, chunk_id in enumerate(retrieved_ids, start=1):
        if chunk_id in relevant:
            reciprocal_rank = 1.0 / rank
            break

    dcg = 0.0
    for index, chunk_id in enumerate(retrieved_ids[:10], start=1):
        if chunk_id in relevant:
            dcg += 1.0 / _log2(index + 1)
    ideal = sum(1.0 / _log2(index + 1) for index in range(1, min(len(relevant), 10) + 1))
    ndcg = dcg / ideal if ideal else 0.0

    return {
        "recall_at_k": round(recall, 6),
        "precision_at_5": round(precision_5, 6),
        "mrr": round(reciprocal_rank, 6),
        "ndcg_at_10": round(ndcg, 6),
    }


def _log2(value: int) -> float:
    return math.log2(value)
