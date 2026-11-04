from __future__ import annotations

from judicial_ai.services.decision import (
    CanaryDecisionModel,
    RetrievalPlan,
    RuleDecisionModel,
    _query_bucket,
    get_decision_model,
)


def _plan(intent: str, provider: str) -> RetrievalPlan:
    return RetrievalPlan(
        intent=intent,
        confidence=0.9,
        need_clarify=False,
        top_k=8,
        context_budget_chars=6000,
        compression_ratio=0.5,
        expand_top_n=2,
        preserve_verbatim=True,
        provider=provider,
        reason="test",
    )


class _FakeModel:
    def __init__(self, provider: str, intent: str) -> None:
        self.provider = provider
        self.intent = intent

    def decide(self, query: str, default_top_k: int = 10) -> RetrievalPlan:
        return _plan(self.intent, self.provider)


def test_query_bucket_stable():
    assert _query_bucket("百万借款合同纠纷起诉") == _query_bucket(" 百万借款合同纠纷起诉 ")
    assert 0 <= _query_bucket("任意查询") <= 99


def test_canary_routes_by_bucket():
    rule = _FakeModel("rule", "CASE_SEARCH")
    canary = _FakeModel("jev", "STATUTE_LOOKUP")
    model = CanaryDecisionModel(rule, canary, 100)
    plan = model.decide("测试", 10)
    assert plan.provider == "jev"
    assert plan.shadow["mode"] == "canary"

    model0 = CanaryDecisionModel(rule, canary, 0)
    plan0 = model0.decide("测试", 10)
    assert plan0.provider == "rule"


def test_canary_failure_falls_back_to_rule():
    class _Broken:
        provider = "jev"

        def decide(self, query, default_top_k=10):
            raise RuntimeError("boom")

    rule = _FakeModel("rule", "CASE_SEARCH")
    model = CanaryDecisionModel(rule, _Broken(), 100)
    plan = model.decide("测试", 10)
    assert plan.provider == "rule"
    assert plan.fallback_from == "jev"


def test_factory_returns_canary(monkeypatch):
    from judicial_ai.config import get_settings
    from judicial_ai.services import decision as decision_mod

    settings = get_settings()
    monkeypatch.setattr(
        type(settings), "jev_base_url", property(lambda self: "http://jev"), raising=False
    )
    monkeypatch.setattr(settings, "decision_provider", "rule", raising=False)
    monkeypatch.setattr(settings, "decision_jev_canary_percent", 25, raising=False)
    monkeypatch.setattr(decision_mod, "JevDecisionModel", lambda s: _FakeModel("jev", "UNKNOWN"))
    get_decision_model.cache_clear()
    model = get_decision_model()
    assert isinstance(model, CanaryDecisionModel)
    assert "25" in model.provider
    get_decision_model.cache_clear()


def test_rule_model_still_works():
    plan = RuleDecisionModel().decide("帮我查找民法典第一千零六十四条", 10)
    assert plan.intent == "STATUTE_LOOKUP"
