from __future__ import annotations

import re
from dataclasses import dataclass

from ..text_utils import normalize_text

_LAW = re.compile(
    r"《?[\u4e00-\u9fa5]{2,32}(?:法|条例|规定|解释|纪要|办法|细则)》?"
    r"(?:第[\d一二三四五六七八九十百千]+条)?"
)
_CASE = re.compile(r"[（(]\d{4}[）)][\u4e00-\u9fa5]{0,8}\d+号")
_AMOUNT = re.compile(r"\d+(?:\.\d+)?(?:万|亿)?元")
_DATE = re.compile(r"\d{4}年\d{1,2}月\d{1,2}日")
_CONCEPT = re.compile(
    r"(民间借贷|高利放贷|一年期贷款市场报价利率|LPR|合同效力|诉讼时效|管辖|"
    r"执行异议|虚假诉讼|非法集资|套路贷)"
)


@dataclass(frozen=True)
class Entity:
    name: str
    normalized: str
    entity_type: str


def _add(entities: list[Entity], seen: set[str], name: str, entity_type: str) -> None:
    cleaned = normalize_text(name).strip("《》（）()，,。；;：:")
    if not cleaned or len(cleaned) > 128:
        return
    normalized = cleaned.lower().replace(" ", "")
    if normalized in seen:
        return
    seen.add(normalized)
    entities.append(Entity(cleaned, normalized, entity_type))


def extract_entities(text: str, limit: int = 24) -> list[Entity]:
    entities: list[Entity] = []
    seen: set[str] = set()
    for match in _LAW.findall(text):
        _add(entities, seen, match, "LAW")
    for match in _CASE.findall(text):
        _add(entities, seen, match, "CASE")
    for match in _AMOUNT.findall(text):
        _add(entities, seen, match, "AMOUNT")
    for match in _DATE.findall(text):
        _add(entities, seen, match, "DATE")
    for match in _CONCEPT.findall(text):
        _add(entities, seen, match, "CONCEPT")
    return entities[:limit]


def entity_terms(query: str) -> list[str]:
    entities = extract_entities(query)
    terms = [item.normalized for item in entities]
    if not terms:
        terms = [
            token
            for token in re.findall(r"[\u4e00-\u9fa5]{2,8}", query)
            if len(token) >= 2
        ][:8]
    return terms[:8]
