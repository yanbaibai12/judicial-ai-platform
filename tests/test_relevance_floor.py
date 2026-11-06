from __future__ import annotations

from judicial_ai.services.retrieval import filter_low_relevance


def _item(chunk_id: str, dense: float, keyword: float = 0.0) -> dict:
    return {"chunk": chunk_id, "dense": dense, "keyword": keyword, "rrf": 0.1}


def test_disabled_threshold_keeps_everything() -> None:
    items = [_item("a", 0.0), _item("b", 0.9)]
    kept, filtered = filter_low_relevance(items, 0.0)

    assert kept == items
    assert filtered == 0


def test_low_dense_without_keyword_is_filtered() -> None:
    items = [_item("relevant", 0.8), _item("noise", 0.05)]
    kept, filtered = filter_low_relevance(items, 0.2)

    assert [item["chunk"] for item in kept] == ["relevant"]
    assert filtered == 1


def test_strong_keyword_hit_survives_low_dense() -> None:
    # 中文专有名词可能向量分数偏低，但关键词显著命中（达到阈值）时不应被误杀。
    items = [_item("statute", 0.01, keyword=3.0)]
    kept, filtered = filter_low_relevance(items, 0.5, 0.3)

    assert [item["chunk"] for item in kept] == ["statute"]
    assert filtered == 0


def test_weak_keyword_overlap_does_not_exempt() -> None:
    # 只要有一个字符重叠 keyword_score 就 >0，微弱重叠不能当作相关性证据。
    items = [_item("noise", 0.02, keyword=0.05)]
    kept, filtered = filter_low_relevance(items, 0.5, 0.3)

    assert kept == []
    assert filtered == 1


def test_all_filtered_returns_empty() -> None:
    items = [_item("a", 0.01), _item("b", 0.02)]
    kept, filtered = filter_low_relevance(items, 0.5)

    assert kept == []
    assert filtered == 2


def test_missing_dense_field_is_treated_as_zero() -> None:
    kept, filtered = filter_low_relevance([{"chunk": "x"}], 0.1)

    assert kept == []
    assert filtered == 1
