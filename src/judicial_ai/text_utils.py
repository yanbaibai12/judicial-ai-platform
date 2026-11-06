from __future__ import annotations

import hashlib
import math
import re
from collections import Counter

_ASCII_WORD = re.compile(r"[a-zA-Z0-9_]+")
_CHINESE_CHAR = re.compile(r"[\u4e00-\u9fff]")
_WHITESPACE = re.compile(r"[ \t\r\f\v]+")

DEFAULT_MAX_TEXT_CHARS = 200_000


def bound_text(text: str, limit: int = DEFAULT_MAX_TEXT_CHARS) -> tuple[str, bool]:
    """\u622a\u65ad\u8d85\u957f\u6587\u672c\u540e\u518d\u626b\u63cf\u3002

    \u8fd4\u56de `(\u5904\u7406\u540e\u6587\u672c, \u662f\u5426\u622a\u65ad)`\u3002
    \u6240\u6709\u5bf9\u5916\u6587\u672c\u5165\u53e3\u90fd\u5e94\u5148\u8fc7\u8fd9\u9053\u95e8\u9650\uff0c
    \u907f\u514d\u5bf9\u8d85\u957f\u8f93\u5165\u53cd\u590d\u56de\u6eaf
    \u9020\u6210\u4e0d\u53ef\u63a7\u7684 CPU \u6d88\u8017\u3002
    """
    if not isinstance(text, str):
        return "", False
    if limit <= 0:
        return text, False
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def normalize_text(text: str) -> str:
    """\u7edf\u4e00\u7a7a\u767d\u524d\u5148\u5957\u957f\u5ea6\u95e8\u9650\uff0c\u7ed9\u4e0b\u6e38\u6b63\u5219\u626b\u63cf\u4e00\u4e2a\u786e\u5b9a\u7684\u65f6\u95f4\u4e0a\u754c\u3002"""
    text, _ = bound_text(text)
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = _WHITESPACE.sub(" ", text)
    return text.strip()


def tokenize(text: str) -> list[str]:
    text, _ = bound_text(text)
    lowered = text.lower()
    tokens = _ASCII_WORD.findall(lowered)
    chars = _CHINESE_CHAR.findall(lowered)
    tokens.extend(chars)
    tokens.extend("".join(chars[index : index + 2]) for index in range(len(chars) - 1))
    return tokens


def keyword_score(query: str, text: str) -> float:
    query_tokens = Counter(tokenize(query))
    text_tokens = Counter(tokenize(text))
    if not query_tokens:
        return 0.0
    overlap = sum(min(count, text_tokens[token]) for token, count in query_tokens.items())
    return overlap / math.sqrt(sum(query_tokens.values()) + 1e-6)


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def chunk_text(text: str, max_chars: int = 1000, overlap: int = 100) -> list[str]:
    """结构感知的轻量切分：优先按段落，超长段落按字符窗口切分。"""
    normalized = normalize_text(text)
    if not normalized:
        return []
    if max_chars <= overlap:
        overlap = max(0, max_chars // 5)

    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", normalized) if part.strip()]
    chunks: list[str] = []
    current = ""

    for paragraph in paragraphs:
        if len(paragraph) <= max_chars:
            candidate = f"{current}\n{paragraph}" if current else paragraph
            if len(candidate) <= max_chars:
                current = candidate
                continue
            if current:
                chunks.append(current)
            current = paragraph
            continue

        if current:
            chunks.append(current)
            current = ""
        start = 0
        while start < len(paragraph):
            end = min(start + max_chars, len(paragraph))
            chunks.append(paragraph[start:end])
            if end >= len(paragraph):
                break
            start = max(0, end - overlap)

    if current:
        chunks.append(current)
    return chunks


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def deterministic_embedding(text: str, dim: int) -> list[float]:
    """本地开发用的词袋哈希向量。

    生产环境应替换为 BGE-M3 等真实 Embedding 服务；该实现只用于无 GPU
    环境下跑通检索链路和测试。
    """
    vector = [0.0] * dim
    for token in tokenize(text):
        digest = hashlib.md5(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dim
        weight = 1.0 + (digest[4] / 255.0)
        vector[index] += weight
    norm = math.sqrt(sum(value * value for value in vector))
    if norm > 0:
        vector = [value / norm for value in vector]
    return vector


def sparse_vector(text: str, vocab_size: int = 1_000_003) -> dict[int, float]:
    """Generate a deterministic sparse lexical vector for Milvus hybrid search."""
    counts = Counter(tokenize(text))
    if not counts:
        return {}
    vector: dict[int, float] = {}
    for token, count in counts.items():
        digest = hashlib.sha1(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % vocab_size
        vector[index] = vector.get(index, 0.0) + 1.0 + math.log1p(count)
    norm = math.sqrt(sum(value * value for value in vector.values()))
    if norm > 0:
        vector = {index: value / norm for index, value in vector.items()}
    return vector
