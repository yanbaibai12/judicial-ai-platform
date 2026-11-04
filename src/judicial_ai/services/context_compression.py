from __future__ import annotations

import hashlib
import re
import threading
from collections import OrderedDict
from typing import Any

from ..config import get_settings
from .decision import RetrievalPlan

_SENTENCE_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_ASCII_RE = re.compile(r"[A-Za-z0-9_]{2,}")

_PROTECTED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"第[一二三四五六七八九十百千零〇0-9]{1,10}条"),
    re.compile(r"《[^》]{1,80}》"),
    re.compile(r"(?:人民币)?\d[\d,]*(?:\.\d+)?(?:元|万元|亿元|%|‰|个月|年)"),
    re.compile(r"[0-9一二三四五六七八九十百千万]+(?:倍|个月)"),
    re.compile(r"\d{4}年\d{1,2}月\d{1,2}日"),
    re.compile(r"[（(]\d{4}[）)]\S{0,12}\d{1,6}号"),
    re.compile(r"案号|民初|民终|刑初|刑终|行初|再审"),
    re.compile(r"判决如下|裁定如下|本院认为|依照|依据|根据"),
)

_CACHE_MAX = 512
_CACHE: OrderedDict[str, str] = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _terms(text: str) -> set[str]:
    terms: set[str] = set()
    for match in _CJK_RE.finditer(text):
        value = match.group(0)
        terms.update(value[index : index + 2] for index in range(max(0, len(value) - 1)))
    for match in _ASCII_RE.finditer(text):
        terms.add(match.group(0).lower())
    return terms


def _split_sentences(text: str) -> list[str]:
    return [segment.strip() for segment in _SENTENCE_RE.findall(text) if segment.strip()]


def _is_protected(sentence: str) -> bool:
    return any(pattern.search(sentence) for pattern in _PROTECTED_PATTERNS)


def _cache_key(text: str, query: str, budget: int, provider: str) -> str:
    digest = hashlib.sha256()
    digest.update(text.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(query.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(str(budget).encode("ascii"))
    digest.update(b"\x00")
    digest.update(provider.encode("ascii"))
    return digest.hexdigest()


def _cache_get(key: str) -> str | None:
    with _CACHE_LOCK:
        value = _CACHE.get(key)
        if value is not None:
            _CACHE.move_to_end(key)
            return value
    try:
        from .cache import get_cache

        shared = get_cache().get(f"compression:{key}")
    except Exception:  # noqa: BLE001 - cache is optional
        return None
    if shared is not None:
        _cache_set_local(key, shared)
    return shared


def _cache_set_local(key: str, value: str) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = value
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)


def _cache_set(key: str, value: str) -> None:
    _cache_set_local(key, value)
    try:
        from ..config import get_settings
        from .cache import get_cache

        get_cache().set(
            f"compression:{key}",
            value,
            ttl_seconds=get_settings().redis_cache_ttl_seconds,
        )
    except Exception:  # noqa: BLE001 - cache is optional
        return


def _split_segments(text: str) -> list[str]:
    """按段落（空行）/章节边界切分逻辑段，段内保持连贯。"""
    raw = re.split(r"\n\s*\n|\n(?=第[一二三四五六七八九十百千零〇0-9]+[章节条])", text)
    return [segment.strip() for segment in raw if segment.strip()]


def _segment_score(segment: str, query_terms: set[str]) -> float:
    terms = _terms(segment)
    overlap = len(query_terms & terms)
    protected = 1.0 if _is_protected(segment) else 0.0
    density = overlap / max(1, len(terms)) if terms else 0.0
    return overlap * 2.0 + density * len(segment) + protected * 3.0


def compress_segments(text: str, query: str, budget: int) -> str:
    """ReFRAG 式分段感知压缩：先选段，再在预算内做段内句子级精修。"""
    segments = _split_segments(text)
    if len(segments) <= 1:
        return compress_text(text, query, budget)
    query_terms = _terms(query)
    scored = sorted(
        (
            (-_segment_score(segment, query_terms), index)
            for index, segment in enumerate(segments)
        ),
    )
    selected: set[int] = set()
    used = 0
    for _neg_score, index in scored:
        length = len(segments[index])
        if used + length <= budget:
            selected.add(index)
            used += length
    if not selected:
        return compress_text(segments[0] if segments else text, query, budget)
    merged = "\n\n".join(segments[index] for index in sorted(selected))
    if len(merged) > budget:
        merged = compress_text(merged, query, budget)
    return merged


def compress_text(text: str, query: str, budget: int) -> str:
    if budget <= 0 or len(text) <= budget:
        return text
    if get_settings().rag_compression_segment_aware:
        segments = _split_segments(text)
        if len(segments) > 1:
            return compress_segments(text, query, budget)
    sentences = _split_sentences(text)
    if not sentences:
        return text[:budget]

    selected: set[int] = {
        index for index, sentence in enumerate(sentences) if index == 0 or _is_protected(sentence)
    }
    query_terms = _terms(query)
    scored: list[tuple[float, int]] = []
    for index, sentence in enumerate(sentences):
        if index in selected:
            continue
        score = float(len(query_terms & _terms(sentence)))
        if index < 2:
            score += 0.5
        scored.append((score, index))
    scored.sort(key=lambda item: (-item[0], item[1]))

    used = sum(len(sentences[index]) for index in selected)
    for score, index in scored:
        if score <= 0 and used >= budget // 2:
            continue
        length = len(sentences[index])
        if used + length > budget:
            continue
        selected.add(index)
        used += length

    if not selected:
        selected.add(0)
    return "".join(sentences[index] for index in sorted(selected))


def _protected_anchors(text: str) -> list[str]:
    return [
        match.group(0)
        for pattern in _PROTECTED_PATTERNS
        for match in pattern.finditer(text)
    ]


def _compress_with_llm(text: str, query: str, budget: int) -> str:
    from .inference import get_inference_client

    settings = get_settings()
    prompt = (
        "你是司法文档压缩器。只保留与问题直接相关的句子，不得新增事实、"
        "不得改写法条条号、金额、日期、案号；无法确定时保留原文。"
        f"问题：{query}\n原文：{text}\n输出压缩后的纯文本，不超过 {budget} 字符。"
    )
    try:
        output = get_inference_client().chat(
            settings.rag_compression_model,
            [{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=max(128, min(512, budget // 2)),
        )
        choices = output.get("choices") or []
        content = ""
        if choices:
            content = str((choices[0].get("message") or {}).get("content") or "").strip()
        anchors = _protected_anchors(text)
        if content and all(anchor in content for anchor in anchors):
            return content[:budget]
    except Exception:  # noqa: BLE001 - LLM compression must fall back to extractive
        pass
    return compress_text(text, query, budget)


def _compress_with_cache(text: str, query: str, budget: int) -> str:
    provider = get_settings().rag_compression_provider
    key = _cache_key(text, query, budget, provider)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    compressed = (
        _compress_with_llm(text, query, budget)
        if provider == "llm"
        else compress_text(text, query, budget)
    )
    _cache_set(key, compressed)
    return compressed


def verify_citation_integrity(
    original_results: list[dict[str, Any]],
    compressed_results: list[dict[str, Any]],
) -> bool:
    originals = {item.get("chunk_id"): item for item in original_results}
    for item in compressed_results:
        citation = item.get("citation")
        if not isinstance(citation, dict):
            continue
        source = originals.get(item.get("chunk_id"))
        if source is None:
            return False
        if citation.get("chunk_id") != item.get("chunk_id"):
            return False
        snippet = citation.get("snippet")
        source_text = str(source.get("text") or "")
        if isinstance(snippet, str) and snippet and snippet[:80] not in source_text:
            return False
    return True


def compress_results(
    results: list[dict[str, Any]],
    query: str,
    plan: RetrievalPlan,
    *,
    total_budget_chars: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not results:
        return [], {
            "enabled": True,
            "provider": plan.provider,
            "intent": plan.intent,
            "original_chars": 0,
            "compressed_chars": 0,
            "saved_chars": 0,
            "ratio": 1.0,
            "verbatim_chunks": 0,
            "compressed_chunks": 0,
            "citation_integrity": True,
            "budget_chars": total_budget_chars or plan.context_budget_chars,
        }

    budget = total_budget_chars or plan.context_budget_chars
    per_chunk = max(120, budget // len(results))
    items: list[dict[str, Any]] = []
    original_total = 0
    compressed_total = 0
    verbatim_chunks = 0
    compressed_chunks = 0
    integrity = True

    for index, item in enumerate(results):
        original_text = str(item.get("text") or "")
        new_text = original_text
        if index >= plan.expand_top_n and len(original_text) > per_chunk:
            new_text = _compress_with_cache(original_text, query, per_chunk)

        citation = item.get("citation")
        new_citation = dict(citation) if isinstance(citation, dict) else None
        valid_citation = True
        if new_citation is not None:
            if new_citation.get("chunk_id") != item.get("chunk_id"):
                valid_citation = False
            snippet = new_citation.get("snippet")
            if isinstance(snippet, str) and snippet and snippet[:80] not in original_text:
                valid_citation = False
        if not valid_citation:
            integrity = False
            new_text = original_text
            if new_citation is not None:
                new_citation["chunk_id"] = item.get("chunk_id")

        original_total += len(original_text)
        compressed_total += len(new_text)
        if new_text == original_text:
            verbatim_chunks += 1
        else:
            compressed_chunks += 1

        new_item = dict(item)
        new_item["text"] = new_text
        if new_citation is not None:
            new_item["citation"] = new_citation
        new_item["original_chars"] = len(original_text)
        new_item["compressed_text"] = new_text != original_text
        items.append(new_item)

    stats = {
        "enabled": True,
        "provider": plan.provider,
        "intent": plan.intent,
        "original_chars": original_total,
        "compressed_chars": compressed_total,
        "saved_chars": max(0, original_total - compressed_total),
        "ratio": round(compressed_total / original_total, 4) if original_total else 1.0,
        "verbatim_chunks": verbatim_chunks,
        "compressed_chunks": compressed_chunks,
        "citation_integrity": integrity,
        "budget_chars": budget,
    }
    return items, stats


