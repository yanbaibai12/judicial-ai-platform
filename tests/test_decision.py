from __future__ import annotations

import httpx
import pytest

from judicial_ai.config import Settings
from judicial_ai.services.decision import (
    JevDecisionModel,
    RuleDecisionModel,
    ShadowDecisionModel,
)


class _FakeResponse:
    def __init__(self, data: dict) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._data


class _FakeClient:
    def __init__(self, data: dict | None = None, error: Exception | None = None) -> None:
        self.data = data or {}
        self.error = error
        self.calls: list[dict] = []

    def post(self, url: str, json: dict, headers: dict) -> _FakeResponse:
        self.calls.append({"url": url, "json": json, "headers": headers})
        if self.error is not None:
            raise self.error
        return _FakeResponse(self.data)


def _settings(**overrides) -> Settings:
    values = {
        "jev_base_url": "https://jev.example",
        "jev_api_key": "test-key",
        "jev_model": "jev-1.14.0",
        "jev_redact_input": True,
        "jev_max_input_chars": 500,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("请帮我找民法典第六百八十条关于借款利率的规定", "STATUTE_LOOKUP"),
        ("有没有类似的民间借贷纠纷判例", "CASE_SEARCH"),
        ("第一次起诉离婚需要准备哪些材料", "PROCEDURE_GUIDE"),
        ("帮我起草一份民间借贷起诉状", "DOCUMENT_DRAFT"),
        ("借款利息太高了可以不给吗", "LEGAL_CONSULTATION"),
        ("今天天气怎么样", "UNKNOWN"),
    ],
)
def test_rule_decision_intents(query: str, expected: str) -> None:
    plan = RuleDecisionModel(_settings()).decide(query)
    assert plan.intent == expected, plan


def test_rule_decision_respects_top_k() -> None:
    plan = RuleDecisionModel(_settings()).decide("找一些类似判例", default_top_k=3)
    assert plan.top_k == 3
    assert plan.expand_top_n <= plan.top_k
    assert plan.context_budget_chars >= 400


def test_jev_adapter_parses_and_redacts() -> None:
    client = _FakeClient(
        {
            "model": "jev-1.14.0",
            "answers": {
                "intent": {
                    "type": "choice",
                    "choice": "CASE_SEARCH",
                    "confidence": 0.91,
                    "probabilities": {"CASE_SEARCH": 0.91},
                },
                "need_clarify": {"type": "noul", "noul": 0.1},
            },
            "usage": {"input_tokens": 80, "output_tokens": 5},
        }
    )
    model = JevDecisionModel(_settings(), client=client)
    plan = model.decide("借款人手机13800138000，找类似判例", default_top_k=10)

    assert plan.provider == "jev"
    assert plan.intent == "CASE_SEARCH"
    assert plan.confidence == 0.91
    assert plan.need_clarify is False
    assert client.calls[0]["url"] == "https://jev.example/v1/systemone"
    assert client.calls[0]["json"]["model"] == "jev-1.14.0"
    assert client.calls[0]["json"]["questions"]["intent"]["type"] == "choice"
    assert "13800138000" not in client.calls[0]["json"]["state"]


def test_jev_adapter_falls_back_to_rule() -> None:
    client = _FakeClient(error=httpx.ConnectError("offline"))
    plan = JevDecisionModel(_settings(), client=client).decide("找一些类似判例")

    assert plan.provider == "rule"
    assert plan.fallback_from == "jev"
    assert plan.intent == "CASE_SEARCH"


def test_jev_adapter_opens_circuit_after_failure() -> None:
    client = _FakeClient(error=httpx.ConnectError("offline"))
    model = JevDecisionModel(_settings(), client=client)

    first = model.decide("找一些类似判例")
    second = model.decide("找一些类似判例")

    assert first.fallback_from == "jev"
    assert second.fallback_from == "jev"
    assert second.reason.startswith("JEV 暂时熔断")
    assert len(client.calls) == 1


def test_shadow_model_does_not_change_primary() -> None:
    client = _FakeClient({"intent": "STATUTE_LOOKUP", "confidence": 0.8})
    primary = RuleDecisionModel(_settings())
    shadow = JevDecisionModel(_settings(), client=client)
    plan = ShadowDecisionModel(primary, shadow).decide("有没有类似的民间借贷纠纷判例")

    assert plan.provider == "rule"
    assert plan.intent == "CASE_SEARCH"
    assert plan.shadow is not None
    assert plan.shadow["provider"] == "jev"
    assert plan.shadow["intent"] == "STATUTE_LOOKUP"
    assert plan.shadow["agreement"] is False
