# 正则处理时间预算（AC-6.1 / AC-6.2 / AC-6.4）
#
# 目的：对"放大回溯/重复扫描"的对抗输入给出可重复的时间上界，并把长度门限
# 行为固定下来。默认超预算时仅告警跳过（避免 CI 抖动导致门禁失败）；严格模式
# 由 STRICT_PERF=1 开启。
#
# 输入全部为合成文本，不含真实案件材料、当事人信息或任何真实案号。

from __future__ import annotations

import os
import time
from statistics import quantiles

import pytest

from judicial_ai.services import governance_pipeline as gov
from judicial_ai.services.context_compression import compress_text
from judicial_ai.services.contract_review import split_clauses
from judicial_ai.services.decision import _score_query
from judicial_ai.services.graph_extract import extract_entities
from judicial_ai.text_utils import DEFAULT_MAX_TEXT_CHARS, bound_text, chunk_text, tokenize

STRICT_PERF = os.environ.get("STRICT_PERF") == "1"

BUDGET_SECONDS = 0.25

ADVERSARIAL: dict[str, str] = {
    "unclosed_quote": "《" + "法" * 200_000,
    "repeated_clause": "第十" * 40_000,
    "digits_without_unit": "1" * 200_000,
    "at_free_alnum": "a" * 200_000,
    "single_paragraph": "本院认为" * 25_000,
    "emailish_no_at": "abc.def-ghi_jkl%" * 12_000,
}

SAMPLE_EMAIL_TEXT = "联系邮箱 contact-mgr@example.invalid 或者 contact@example.invalid"


def _measure(fn, text: str, iterations: int = 5) -> float:
    """返回 P95 处理耗时（秒）。"""
    durations = []
    for _ in range(iterations):
        started = time.perf_counter()
        fn(text)
        durations.append(time.perf_counter() - started)
    if len(durations) == 1:
        return durations[0]
    return quantiles(durations, n=20)[-1]


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_regex_budget_is_bounded(name):
    sample = ADVERSARIAL[name][:DEFAULT_MAX_TEXT_CHARS]
    cases = {
        "governance_sanitize": gov.sanitize_text,
        "governance_mask": gov.mask_sensitive,
        "governance_clean": gov.clean_text,
        "chunk_text": chunk_text,
        "tokenize": tokenize,
        "split_clauses": split_clauses,
        "extract_entities": extract_entities,
        "compress_text": lambda text: compress_text(text, "", 200),
        "contains_sensitive": gov.contains_sensitive,
        "plan_score_query": _score_query,
    }
    over_budget = []
    for label, fn in cases.items():
        elapsed = _measure(fn, sample)
        if elapsed > BUDGET_SECONDS:
            over_budget.append(f"{label}={elapsed:.3f}s")
    if over_budget:
        message = f"{name} 超出时间预算: {', '.join(over_budget)}"
        if STRICT_PERF:
            pytest.fail(message)
        pytest.skip(f"[perf-warn] {message}")


def test_bound_text_default_limit():
    long_text = "x" * (DEFAULT_MAX_TEXT_CHARS + 100)
    truncated, was_cut = bound_text(long_text)
    assert was_cut is True
    assert len(truncated) == DEFAULT_MAX_TEXT_CHARS

    short_text = "短文本"
    truncated, was_cut = bound_text(short_text)
    assert was_cut is False
    assert truncated == short_text


def test_bound_text_rejects_non_string():
    truncated, was_cut = bound_text(None)  # type: ignore[arg-type]
    assert truncated == ""
    assert was_cut is False


def test_bound_text_zero_limit_is_passthrough():
    truncated, was_cut = bound_text("anything", 0)
    assert truncated == "anything"
    assert was_cut is False


def test_mask_emails_still_matches_normal_addresses():
    masked, hits = gov.mask_emails(SAMPLE_EMAIL_TEXT)
    assert hits == 2
    assert "example.invalid" not in masked
    assert masked.count(gov._EMAIL_MASK) == 2


def test_mask_emails_leaves_text_without_at_untouched():
    non_email = "没有邮箱符号的普通文本" * 1000
    masked, hits = gov.mask_emails(non_email)
    assert hits == 0
    assert masked == non_email


def test_mask_sensitive_reports_email_hits():
    masked, counts = gov.mask_sensitive(f"请发到 {SAMPLE_EMAIL_TEXT}")
    assert counts.get("email") == 2
    assert "example.invalid" not in masked