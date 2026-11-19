from __future__ import annotations

import json

import httpx
import pytest

from scripts.acceptance import run_staging_acceptance as runner


@pytest.mark.parametrize(
    ("statuses", "expected", "exit_code"),
    [
        ([runner.PASS], runner.PASS, 0),
        ([runner.PASS, runner.SKIP], runner.INCOMPLETE, 3),
        ([runner.SKIP, runner.FAIL], runner.FAIL, 1),
        ([], runner.INCOMPLETE, 3),
    ],
)
def test_summary_and_exit_code_do_not_treat_skips_as_pass(
    monkeypatch, tmp_path, capsys, statuses, expected, exit_code
):
    scenario = runner.Scenario("test")
    for index, status in enumerate(statuses):
        scenario.add(str(index), "step", status)
    monkeypatch.setattr(runner, "_execute", lambda api, args: [scenario])

    code = runner.main([
        "--base-url", "http://test.invalid", "--admin-token", "test-token",
        "--evidence-dir", str(tmp_path),
    ])

    assert code == exit_code
    assert f"test: {expected}" in capsys.readouterr().out
    summary = next(tmp_path.glob("*/summary.md")).read_text(encoding="utf-8")
    expected_text = {runner.PASS: "通过", runner.FAIL: "失败", runner.INCOMPLETE: "未完成"}
    assert f"## test：{expected_text[expected]}" in summary


@pytest.mark.parametrize(
    ("model", "status_code", "content", "expected"),
    [
        (None, 200, "not used", runner.SKIP),
        ("test", 200, "sensitive answer", runner.SKIP),
        ("test", 200, "", runner.FAIL),
        ("test", 200, "   ", runner.FAIL),
        ("test", 200, None, runner.FAIL),
        ("test", 503, "", runner.FAIL),
    ],
)
def test_model_result_is_not_persisted_and_failures_are_visible(
    monkeypatch, model, status_code, content, expected
):
    results = iter([
        (200, {
            "results": [{"chunk_id": "c1", "citation": {"source_url": "/source",
                                                          "document_version_id": 1}}],
            "citation": {"status": "VERIFIED"},
        }),
        (200, {"results": []}),
    ])
    monkeypatch.setattr(runner, "_retrieve", lambda *args: next(results))

    class FakeApi:
        def call(self, method, path, **kwargs):
            if path == "/source":
                return httpx.Response(200, json={"data": {
                    "chunks": [{"chunk_id": "c1"}], "document_version": {"id": 1},
                }})
            return httpx.Response(status_code, json={"data": {
                "choices": [{"message": {"content": content}}],
            }})

    scenario = runner.Scenario("A05")
    runner.scenario_a05(
        FakeApi(), scenario,
        {"app_token": "test", "kb_id": 1, "alpha": "test", "suffix": "123"}, model,
    )
    step = next(item for item in scenario.steps if item.sid == "A05-07")
    assert step.status == expected
    assert "sensitive answer" not in json.dumps(scenario.evidence)
    assert "answer_preview" not in scenario.evidence.get("A05_chat", {})


def test_capability_response_is_not_persisted():
    responses = iter([
        httpx.Response(200, json={"data": {"answer": "sensitive answer"}}),
        httpx.Response(403),
        httpx.Response(401),
    ])

    class FakeApi:
        def call(self, *args, **kwargs):
            return next(responses)

    scenario = runner.Scenario("A01")
    runner._invoke_matrix(
        FakeApi(), scenario, {"app_token": "test", "denied_token": "denied"}, "cap",
    )
    assert scenario.status == runner.PASS
    assert "sensitive answer" not in json.dumps(scenario.evidence)
    assert "body" not in scenario.evidence["A01_invoke_allowed"]
