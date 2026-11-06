from __future__ import annotations

from judicial_ai.services.context_compression import (
    CITATION_DEGRADED,
    CITATION_INVALID,
    CITATION_NONE,
    CITATION_VERIFIED,
    citation_status_for,
    compress_results,
    compress_text,
    verify_citation_integrity,
)
from judicial_ai.services.decision import RetrievalPlan

LEGAL_TEXT = (
    "第六百八十条 禁止高利放贷，借款的利率不得违反国家有关规定。\n"
    "借款合同对支付利息没有约定的，视为没有利息。\n"
    "出借人请求借款人按照合同约定利率支付利息的，人民法院应予支持，"
    "但是双方约定的利率超过合同成立时一年期贷款市场报价利率四倍的除外。\n"
    "本条所称一年期贷款市场报价利率，是指中国人民银行授权全国银行间同业拆借中心"
    "自2019年8月20日起每月发布的一年期贷款市场报价利率。\n"
    "出借人与借款人既约定了逾期利率，又约定了违约金或者其他费用，"
    "出借人可以选择主张逾期利息、违约金或者其他费用，也可以一并主张，"
    "但是总计超过合同成立时一年期贷款市场报价利率四倍的部分，人民法院不予支持。"
)


def _plan(**overrides) -> RetrievalPlan:
    values = {
        "intent": "STATUTE_LOOKUP",
        "confidence": 0.8,
        "need_clarify": False,
        "top_k": 3,
        "context_budget_chars": 300,
        "compression_ratio": 0.4,
        "expand_top_n": 0,
        "preserve_verbatim": True,
        "provider": "rule",
        "reason": "test",
    }
    values.update(overrides)
    return RetrievalPlan(**values)


def _result(chunk_id: str = "chk_1", text: str = LEGAL_TEXT, citation: dict | None = None):
    result = {
        "chunk_id": chunk_id,
        "text": text,
        "score": 0.9,
        "metadata": {"doc_type": "law"},
        "citation": citation
        or {
            "chunk_id": chunk_id,
            "title": "民法典借款合同条款",
            "snippet": text[:300],
        },
    }
    return result


def test_compress_text_keeps_legal_anchors() -> None:
    compressed = compress_text(LEGAL_TEXT, "借款利率超过四倍是否受保护", 260)

    assert len(compressed) < len(LEGAL_TEXT)
    assert "第六百八十条" in compressed
    assert "四倍" in compressed
    assert "2019年8月20日" in compressed


def test_compress_results_preserves_citation() -> None:
    results = [_result()]
    compressed, stats = compress_results(
        results, "借款利率超过四倍是否受保护", _plan(), total_budget_chars=260
    )

    assert stats["enabled"] is True
    assert stats["compressed_chars"] < stats["original_chars"]
    assert stats["ratio"] < 1
    assert stats["citation_integrity"] is True
    assert compressed[0]["citation"]["snippet"] == results[0]["citation"]["snippet"]
    assert verify_citation_integrity(results, compressed) is True


def test_expand_top_n_keeps_first_chunk_verbatim() -> None:
    results = [
        _result("chk_1", LEGAL_TEXT + "第一段补充内容。"),
        _result("chk_2", LEGAL_TEXT + "第二段补充内容。"),
    ]
    compressed, _stats = compress_results(
        results,
        "借款利率",
        _plan(expand_top_n=1),
        total_budget_chars=400,
    )

    assert compressed[0]["text"] == results[0]["text"]
    assert len(compressed[1]["text"]) < len(results[1]["text"])


def test_invalid_citation_falls_back_to_original_text() -> None:
    result = _result(citation={"chunk_id": "chk_other", "snippet": "不存在的原文"})
    compressed, stats = compress_results(
        [result], "借款利率", _plan(), total_budget_chars=160
    )

    assert stats["citation_integrity"] is False
    assert compressed[0]["text"] == result["text"]
    assert verify_citation_integrity([result], compressed) is False


def test_citation_status_separates_degraded_from_invalid() -> None:
    verified = _result()
    degraded_citation = {"chunk_id": "chk_1", "snippet": "原文中根本不存在的片段"}
    invalid_citation = {"chunk_id": "chk_other", "snippet": LEGAL_TEXT[:30]}

    assert citation_status_for(verified["text"], "chk_1", verified["citation"]) == CITATION_VERIFIED
    assert (
        citation_status_for(verified["text"], "chk_1", degraded_citation) == CITATION_DEGRADED
    )
    assert citation_status_for(verified["text"], "chk_1", invalid_citation) == CITATION_INVALID
    assert citation_status_for(verified["text"], "chk_1", None) == CITATION_NONE


def test_compress_results_reports_three_state_counts() -> None:
    results = [
        _result(),
        _result("chk_2", LEGAL_TEXT, {"chunk_id": "chk_2", "snippet": "无法在原文定位的片段"}),
    ]
    compressed, stats = compress_results(
        results, "借款利率", _plan(), total_budget_chars=260
    )

    assert stats["citation_counts"][CITATION_VERIFIED] == 1
    assert stats["citation_counts"][CITATION_DEGRADED] == 1
    assert stats["citation_status"] == CITATION_DEGRADED
    assert stats["citation_integrity"] is False
    assert compressed[0]["citation_status"] == CITATION_VERIFIED
    assert compressed[1]["citation_status"] == CITATION_DEGRADED
    # 引用不可信时必须回退原文，不能把改写后的文本当证据返回。
    assert compressed[1]["text"] == results[1]["text"]


def test_compress_results_marks_invalid_when_overall_has_mismatched_chunk() -> None:
    results = [_result("chk_9", LEGAL_TEXT, {"chunk_id": "chk_other", "snippet": LEGAL_TEXT[:20]})]
    compressed, stats = compress_results(
        results, "借款利率", _plan(), total_budget_chars=260
    )

    assert stats["citation_status"] == CITATION_INVALID
    assert compressed[0]["citation"]["status"] == CITATION_INVALID
    assert compressed[0]["text"] == results[0]["text"]


def test_compression_is_deterministic() -> None:
    first = compress_text(LEGAL_TEXT, "借款利率", 220)
    second = compress_text(LEGAL_TEXT, "借款利率", 220)
    assert first == second
