from judicial_ai.services.context_compression import _protected_anchors
from judicial_ai.services.graph_extract import extract_entities


def test_amount_extraction_preserves_integer_decimal_and_large_units():
    entities = extract_entities("本金100元，利息12.50元，赔偿2万元及3亿元。")
    amounts = {entity.name for entity in entities if entity.entity_type == "AMOUNT"}
    assert amounts == {"100元", "12.50元", "2万元", "3亿元"}


def test_long_number_without_unit_is_not_an_amount():
    entities = extract_entities("1" * 200_000)
    assert not any(entity.entity_type == "AMOUNT" for entity in entities)


def test_protected_anchors_handle_long_number_without_unit():
    assert _protected_anchors("1" * 200_000) == []
    anchors = _protected_anchors("人民币1,234.50元，利息为基数的4倍。")
    assert "人民币1,234.50元" in anchors
    assert "4倍" in anchors
