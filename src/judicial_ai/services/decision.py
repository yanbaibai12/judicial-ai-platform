from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from typing import Any, Protocol

import httpx

from ..config import Settings, get_settings
from .governance_pipeline import sanitize_text

INTENTS: tuple[str, ...] = (
    "STATUTE_LOOKUP",
    "CASE_SEARCH",
    "PROCEDURE_GUIDE",
    "DOCUMENT_DRAFT",
    "LEGAL_CONSULTATION",
    "UNKNOWN",
)

_KEYWORDS: dict[str, tuple[tuple[str, str, float], ...]] = {
    "STATUTE_LOOKUP": (
        ("条号", r"第[一二三四五六七八九十百千零〇0-9]{1,10}条", 3.0),
        ("法条", r"法条|条文", 3.0),
        ("法律名称", r"民法典|刑法|刑事诉讼法|民事诉讼法|行政诉讼法|司法解释|条例|修正案", 2.0),
        ("法律依据", r"法律规定|法律依据|依据|根据", 1.0),
        ("法律", r"法律|规定", 0.6),
    ),
    "CASE_SEARCH": (
        ("案例", r"案例|判例|类案", 3.0),
        ("裁判文书", r"判决书|裁定书|裁判文书", 2.0),
        ("相似案件", r"类似案件|相似案件|同案", 2.5),
        ("裁判规则", r"裁判规则|指导性案例|公报案例", 2.5),
    ),
    "PROCEDURE_GUIDE": (
        ("诉讼流程", r"怎么起诉|如何起诉|怎么立案|如何立案|诉讼流程|起诉流程", 3.0),
        ("起诉", r"起诉|立案", 1.5),
        ("程序节点", r"管辖|诉讼费|举证期限|开庭|上诉|申请执行|执行期限|再审申请", 2.0),
        ("期限", r"多久|几天|多长时间|期限", 1.5),
        ("材料", r"需要什么材料|准备哪些材料|提交材料|所需材料", 2.0),
    ),
    "DOCUMENT_DRAFT": (
        ("起草", r"起草|撰写|拟写|帮我写|写一份|生成一份", 3.0),
        ("文书", r"起诉状|答辩状|上诉状|申请书|律师函|法律意见书|判决书|裁定书|模板", 2.5),
    ),
    "LEGAL_CONSULTATION": (
        ("咨询", r"能否|是否可以|是否合法|是否有效|怎么办|该如何|怎么处理", 2.0),
        (
            "责任",
            r"责任|赔偿|违约金|利息|利率|合同|离婚|继承|工伤|侵权|犯罪|量刑|借款|欠款|劳动",
            1.5,
        ),
        ("判断", r"合法|违法|有效|无效|保护|支持", 1.0),
    ),
}

_RULES: tuple[tuple[str, str, re.Pattern[str], float], ...] = tuple(
    (intent, label, re.compile(pattern), weight)
    for intent, entries in _KEYWORDS.items()
    for label, pattern, weight in entries
)

_INTENT_PLANS: dict[str, dict[str, float | int | bool]] = {
    "STATUTE_LOOKUP": {
        "top_k": 8,
        "context_budget_chars": 6000,
        "compression_ratio": 0.6,
        "expand_top_n": 3,
        "preserve_verbatim": True,
    },
    "CASE_SEARCH": {
        "top_k": 10,
        "context_budget_chars": 8000,
        "compression_ratio": 0.5,
        "expand_top_n": 2,
        "preserve_verbatim": True,
    },
    "PROCEDURE_GUIDE": {
        "top_k": 6,
        "context_budget_chars": 4000,
        "compression_ratio": 0.4,
        "expand_top_n": 1,
        "preserve_verbatim": False,
    },
    "DOCUMENT_DRAFT": {
        "top_k": 8,
        "context_budget_chars": 6000,
        "compression_ratio": 0.45,
        "expand_top_n": 2,
        "preserve_verbatim": True,
    },
    "LEGAL_CONSULTATION": {
        "top_k": 10,
        "context_budget_chars": 8000,
        "compression_ratio": 0.5,
        "expand_top_n": 2,
        "preserve_verbatim": True,
    },
    "UNKNOWN": {
        "top_k": 8,
        "context_budget_chars": 6000,
        "compression_ratio": 0.5,
        "expand_top_n": 1,
        "preserve_verbatim": True,
    },
}

_INTENT_ALIASES: dict[str, str] = {
    "STATUTE": "STATUTE_LOOKUP",
    "STATUTE_LOOKUP": "STATUTE_LOOKUP",
    "LAW_LOOKUP": "STATUTE_LOOKUP",
    "LEGAL_PROVISION": "STATUTE_LOOKUP",
    "法条": "STATUTE_LOOKUP",
    "CASE": "CASE_SEARCH",
    "CASE_SEARCH": "CASE_SEARCH",
    "CASE_LOOKUP": "CASE_SEARCH",
    "案例": "CASE_SEARCH",
    "PROCEDURE": "PROCEDURE_GUIDE",
    "PROCEDURE_GUIDE": "PROCEDURE_GUIDE",
    "PROCESS": "PROCEDURE_GUIDE",
    "程序": "PROCEDURE_GUIDE",
    "DRAFT": "DOCUMENT_DRAFT",
    "DOCUMENT_DRAFT": "DOCUMENT_DRAFT",
    "DOCUMENT": "DOCUMENT_DRAFT",
    "文书": "DOCUMENT_DRAFT",
    "CONSULTATION": "LEGAL_CONSULTATION",
    "LEGAL_CONSULTATION": "LEGAL_CONSULTATION",
    "LEGAL_ADVICE": "LEGAL_CONSULTATION",
    "咨询": "LEGAL_CONSULTATION",
    "OTHER": "UNKNOWN",
    "UNKNOWN": "UNKNOWN",
}

_JEV_CRITERIA: dict[str, str] = {
    "STATUTE_LOOKUP": "查找法条、法律规定、条文或法律依据",
    "CASE_SEARCH": "查找案例、判例、类案或裁判规则",
    "PROCEDURE_GUIDE": "询问起诉、立案、管辖、期限、材料等诉讼流程",
    "DOCUMENT_DRAFT": "起草、撰写或生成起诉状、答辩状、申请书等法律文书",
    "LEGAL_CONSULTATION": "咨询具体法律问题、权利义务、责任或处理方式",
    "UNKNOWN": "无法归入以上类别的其他请求",
}


@dataclass(slots=True)
class RetrievalPlan:
    intent: str
    confidence: float
    need_clarify: bool
    top_k: int
    context_budget_chars: int
    compression_ratio: float
    expand_top_n: int
    preserve_verbatim: bool
    provider: str
    reason: str
    signals: list[str] = field(default_factory=list)
    latency_ms: int = 0
    fallback_from: str | None = None
    shadow: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DecisionModel(Protocol):
    provider: str

    def decide(self, query: str, default_top_k: int = 10) -> RetrievalPlan: ...


def _score_query(text: str) -> dict[str, float]:
    scores = {intent: 0.0 for intent in INTENTS if intent != "UNKNOWN"}
    for intent, _label, pattern, weight in _RULES:
        hits = len(pattern.findall(text))
        if hits:
            scores[intent] += min(hits, 3) * weight
    return scores


def _signals(text: str, intent: str) -> list[str]:
    return [
        label
        for rule_intent, label, pattern, _weight in _RULES
        if rule_intent == intent and pattern.search(text)
    ][:5]


def _clamp_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, min(1.0, confidence)), 4)


def _normalize_intent(value: Any) -> str:
    if not isinstance(value, str):
        return "UNKNOWN"
    candidate = value.strip().upper().replace("-", "_").replace(" ", "_")
    return _INTENT_ALIASES.get(candidate, _INTENT_ALIASES.get(value.strip(), "UNKNOWN"))


def _build_plan(
    intent: str,
    default_top_k: int,
    settings: Settings,
    *,
    provider: str,
    confidence: float,
    reason: str,
    signals: list[str] | None = None,
    fallback_from: str | None = None,
) -> RetrievalPlan:
    defaults = _INTENT_PLANS.get(intent, _INTENT_PLANS["UNKNOWN"])
    planned_top_k = max(1, int(defaults["top_k"]))
    top_k = max(1, min(planned_top_k, default_top_k, settings.max_top_k))
    budget = int(defaults["context_budget_chars"])
    if top_k < planned_top_k:
        budget = max(400, int(budget * top_k / planned_top_k))
    need_clarify = intent == "UNKNOWN" and confidence < 0.4
    return RetrievalPlan(
        intent=intent,
        confidence=_clamp_confidence(confidence),
        need_clarify=need_clarify,
        top_k=top_k,
        context_budget_chars=min(budget, settings.rag_compression_max_chars),
        compression_ratio=float(defaults["compression_ratio"]),
        expand_top_n=min(int(defaults["expand_top_n"]), top_k),
        preserve_verbatim=bool(defaults["preserve_verbatim"]),
        provider=provider,
        reason=reason,
        signals=list(signals or []),
        fallback_from=fallback_from,
    )


class RuleDecisionModel:
    provider = "rule"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def decide(self, query: str, default_top_k: int = 10) -> RetrievalPlan:
        started = time.perf_counter()
        text = query.strip()
        scores = _score_query(text)
        intent = max(scores, key=lambda item: scores[item])
        best = scores[intent]
        if best <= 0:
            intent = "UNKNOWN"
        confidence = 0.2 if intent == "UNKNOWN" else min(0.95, 0.45 + 0.1 * best)
        plan = _build_plan(
            intent,
            default_top_k,
            self.settings,
            provider=self.provider,
            confidence=confidence,
            reason=f"规则关键词命中：{'、'.join(_signals(text, intent)) or '无'}",
            signals=_signals(text, intent),
        )
        plan.latency_ms = int((time.perf_counter() - started) * 1000)
        return plan


def _extract_decision(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        answers = data.get("answers")
        if isinstance(answers, dict):
            intent_answer = answers.get("intent")
            if isinstance(intent_answer, dict):
                result: dict[str, Any] = {
                    "intent": intent_answer.get("choice"),
                    "confidence": intent_answer.get("confidence"),
                    "model": data.get("model"),
                    "usage": data.get("usage"),
                }
                clarify_answer = answers.get("need_clarify")
                if isinstance(clarify_answer, dict) and "noul" in clarify_answer:
                    try:
                        result["need_clarify"] = float(clarify_answer["noul"]) >= 0.5
                    except (TypeError, ValueError):
                        result["need_clarify"] = False
                return result
        for key in ("output", "data", "result", "decision", "response"):
            if key in data:
                return _extract_decision(data[key])
        choices = data.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
            if isinstance(message, dict) and "content" in message:
                return _extract_decision(message["content"])
    elif isinstance(data, str):
        try:
            return _extract_decision(json.loads(data))
        except json.JSONDecodeError:
            return {"intent": "UNKNOWN", "confidence": 0.0}
    return data if isinstance(data, dict) else {"intent": "UNKNOWN", "confidence": 0.0}


class JevDecisionModel:
    provider = "jev"

    def __init__(self, settings: Settings | None = None, *, client: Any | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.jev_base_url:
            raise ValueError("JEV_BASE_URL 未配置")
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(self.settings.jev_timeout_seconds)
        )
        self._disabled_until = 0.0

    def _endpoint(self) -> str:
        base = str(self.settings.jev_base_url).rstrip("/")
        if base.endswith("/v1"):
            return f"{base}/systemone"
        return f"{base}/v1/systemone"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.settings.jev_api_key:
            headers["Authorization"] = f"Bearer {self.settings.jev_api_key}"
        return headers

    def _call(self, query: str) -> dict[str, Any]:
        text = query
        if self.settings.jev_redact_input:
            text, _counts = sanitize_text(
                text, redact=True, max_chars=self.settings.jev_max_input_chars
            )
        payload = {
            "model": self.settings.jev_model,
            "state": text,
            "questions": {
                "intent": {
                    "type": "choice",
                    "instructions": "判断用户请求属于哪一类司法 AI 任务",
                    "criteria": _JEV_CRITERIA,
                },
                "need_clarify": {
                    "type": "noul",
                    "instructions": "该请求是否过于模糊，需要先向用户追问澄清",
                },
            },
        }
        response = self._client.post(self._endpoint(), json=payload, headers=self._headers())
        response.raise_for_status()
        return _extract_decision(response.json())

    def _fallback(
        self, query: str, default_top_k: int, started: float, reason: str
    ) -> RetrievalPlan:
        fallback = RuleDecisionModel(self.settings).decide(query, default_top_k)
        fallback.fallback_from = self.provider
        fallback.reason = reason
        fallback.latency_ms = int((time.perf_counter() - started) * 1000)
        return fallback

    def decide(self, query: str, default_top_k: int = 10) -> RetrievalPlan:
        started = time.perf_counter()
        if time.monotonic() < self._disabled_until:
            return self._fallback(
                query, default_top_k, started, "JEV 暂时熔断，已回退规则"
            )
        try:
            decision = self._call(query)
        except Exception as exc:
            self._disabled_until = time.monotonic() + 60.0
            return self._fallback(
                query,
                default_top_k,
                started,
                f"JEV 调用失败，已回退规则：{type(exc).__name__}",
            )
        intent = _normalize_intent(decision.get("intent"))
        plan = _build_plan(
            intent,
            default_top_k,
            self.settings,
            provider=self.provider,
            confidence=_clamp_confidence(decision.get("confidence")),
            reason=str(decision.get("reason") or f"JEV {self.settings.jev_model} 决策"),
        )
        if "need_clarify" in decision:
            plan.need_clarify = bool(decision["need_clarify"])
        top_k = decision.get("top_k")
        if isinstance(top_k, (int, float)) and not isinstance(top_k, bool):
            plan.top_k = max(1, min(int(top_k), self.settings.max_top_k))
            plan.expand_top_n = min(plan.expand_top_n, plan.top_k)
        plan.latency_ms = int((time.perf_counter() - started) * 1000)
        return plan


class ShadowDecisionModel:
    def __init__(self, primary: DecisionModel, shadow: DecisionModel) -> None:
        self.primary = primary
        self.shadow = shadow
        self.provider = f"shadow:{primary.provider}:{shadow.provider}"

    def decide(self, query: str, default_top_k: int = 10) -> RetrievalPlan:
        plan = self.primary.decide(query, default_top_k)
        try:
            shadow_plan = self.shadow.decide(query, default_top_k)
            plan.shadow = {
                "provider": shadow_plan.provider,
                "intent": shadow_plan.intent,
                "confidence": shadow_plan.confidence,
                "need_clarify": shadow_plan.need_clarify,
                "agreement": shadow_plan.intent == plan.intent,
                "fallback": shadow_plan.fallback_from,
                "latency_ms": shadow_plan.latency_ms,
            }
        except Exception as exc:
            plan.shadow = {"provider": self.shadow.provider, "error": type(exc).__name__}
        return plan


def _query_bucket(query: str) -> int:
    digest = hashlib.sha256(query.strip().encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100


class CanaryDecisionModel:
    """灰度切主：按 query 稳定哈希分流，canary 百分比内走 JEV，其余走规则。

    JEV 失败自动回退规则（JevDecisionModel 自带熔断），并在 shadow 中记录分流信息。
    """

    def __init__(
        self,
        rule: DecisionModel,
        canary: DecisionModel,
        percent: int,
    ) -> None:
        self.rule = rule
        self.canary = canary
        self.percent = max(0, min(100, int(percent)))
        self.provider = f"canary:{rule.provider}:{canary.provider}@{self.percent}"

    def decide(self, query: str, default_top_k: int = 10) -> RetrievalPlan:
        bucket = _query_bucket(query)
        use_canary = bucket < self.percent
        if use_canary:
            try:
                plan = self.canary.decide(query, default_top_k)
            except Exception as exc:
                plan = self.rule.decide(query, default_top_k)
                plan.fallback_from = self.canary.provider
                plan.reason = f"JEV 灰度调用失败，回退规则：{type(exc).__name__}"
        else:
            plan = self.rule.decide(query, default_top_k)
        plan.shadow = {
            "mode": "canary",
            "bucket": bucket,
            "percent": self.percent,
            "selected": self.canary.provider if use_canary else self.rule.provider,
        }
        return plan


@lru_cache
def get_decision_model() -> DecisionModel:
    settings = get_settings()
    if settings.decision_provider == "jev" and settings.jev_base_url:
        try:
            return JevDecisionModel(settings)
        except ValueError:
            return RuleDecisionModel(settings)
    primary: DecisionModel = RuleDecisionModel(settings)
    if settings.decision_jev_canary_percent > 0 and settings.jev_base_url:
        try:
            canary: DecisionModel = JevDecisionModel(settings)
        except ValueError:
            return primary
        return CanaryDecisionModel(primary, canary, settings.decision_jev_canary_percent)
    if settings.decision_shadow_enabled and settings.jev_base_url:
        try:
            shadow: DecisionModel = JevDecisionModel(settings)
        except ValueError:
            return primary
        return ShadowDecisionModel(primary, shadow)
    return primary


def plan_query(query: str, default_top_k: int = 10) -> RetrievalPlan:
    return get_decision_model().decide(query, default_top_k)
