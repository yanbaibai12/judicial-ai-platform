from __future__ import annotations

from judicial_ai.config import Settings
from judicial_ai.errors import ApiError

HIGH_RISK_CONTRACT = """借款合同

第一条 借款金额与利率
出借人向借款人出借人民币十万元，年利率为 28%，按月付息，利滚利计息。

第二条 违约责任
借款人逾期还款的，按未还金额的 50% 支付违约金。

第三条 其他
出借人有权单方面解除本合同，且无需通知借款人。
借款人对本合同项下债务承担无限连带责任。
"""

GOOD_CONTRACT = """服务合同

第一条 价款与支付
服务费总额为人民币五万元，合同签订后十个工作日内支付 50%，验收合格后支付剩余 50%。

第二条 履行期限
乙方应于 2026 年 12 月 31 日前完成全部交付内容。

第三条 违约责任
任何一方违约的，应赔偿对方因此遭受的实际损失，违约金不超过合同总价的 20%。

第四条 争议解决
因本合同引起的争议，双方协商不成的，向合同签订地人民法院提起诉讼。
"""


def test_review_high_risk_contract(client, new_tenant_app):
    headers = new_tenant_app["headers"]
    response = client.post(
        "/api/v1/contract/review",
        json={"text": HIGH_RISK_CONTRACT},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["clause_count"] >= 3
    assert data["overall_risk"] == "HIGH"
    rule_ids = {item["rule_id"] for item in data["findings"]}
    assert "INTEREST_CAP" in rule_ids
    assert "PENALTY_EXCESSIVE" in rule_ids
    assert "UNILATERAL_TERMINATION" in rule_ids
    assert "UNLIMITED_LIABILITY" in rule_ids
    # 缺失条款检查：该合同没有"付款"相关表述时应命中价款缺失
    missing = {
        item["rule_id"] for item in data["findings"] if item["rule_id"].startswith("MISSING")
    }
    assert missing
    # 每个发现都有建议与法条依据
    for item in data["findings"]:
        assert item["suggestion"]
        assert "民法典" in item["legal_basis"] or "诉讼法" in item["legal_basis"]
    assert "AI 辅助" in data["disclaimer"]
    assert data["review_status"] == "HUMAN_REVIEW_REQUIRED"
    assert all(item["citation_status"] == "NOT_REQUESTED" for item in data["findings"])
    assert all(item["legal_basis_status"] == "UNVERIFIED" for item in data["findings"])


def test_review_clean_contract_low_risk(client, new_tenant_app):
    headers = new_tenant_app["headers"]
    response = client.post(
        "/api/v1/contract/review",
        json={"text": GOOD_CONTRACT},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    rule_ids = {item["rule_id"] for item in data["findings"]}
    assert "INTEREST_CAP" not in rule_ids
    assert "PENALTY_EXCESSIVE" in rule_ids
    assert data["severity_counts"]["high"] == 0


def test_review_with_citations_from_kb(client, new_tenant_app):
    headers = new_tenant_app["headers"]
    # 建库 + 放法条 + 自动索引
    response = client.post(
        "/api/v1/kbs",
        json={
            "code": "law-ref",
            "name": "法条引用库",
            "domain": "legal",
            "embedding_model": "bge-m3",
            "chunk_config": {"max_chars": 240, "overlap": 30},
        },
        headers=headers,
    )
    assert response.status_code == 201
    kb_id = response.json()["data"]["id"]
    response = client.post(
        f"/api/v1/kbs/{kb_id}/documents",
        json={
            "title": "民法典借款合同编",
            "doc_type": "law",
            "text": (
                "第六百八十条 禁止高利放贷，借款的利率不得违反国家有关规定。\n\n"
                "借款合同对支付利息没有约定的，视为没有利息。"
            ),
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text

    # 发布版本 + 建索引 + 设 prod 别名（与 retrieval 测试同流程）
    response = client.post(
        f"/api/v1/kbs/{kb_id}/versions",
        json={"version": "v1.0.0", "publish": True},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    response = client.post(f"/api/v1/kbs/{kb_id}/index-jobs?version=v1.0.0", headers=headers)
    assert response.status_code == 202, response.text
    index_version = response.json()["data"]["index"]["version"]
    response = client.post(
        f"/api/v1/kbs/{kb_id}/aliases",
        json={"alias": "prod", "kb_version": "v1.0.0", "index_version": index_version},
        headers=headers,
    )
    assert response.status_code == 200, response.text

    response = client.post(
        "/api/v1/contract/review",
        json={"text": HIGH_RISK_CONTRACT, "kb_ids": [kb_id]},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    cited = [item for item in data["findings"] if item["citations"]]
    assert cited, "应至少有一条风险带知识库引用"
    assert all(item["citation_status"] == "RETRIEVED_UNVERIFIED" for item in cited)


def test_review_rejects_empty_text(client, new_tenant_app):
    headers = new_tenant_app["headers"]
    response = client.post(
        "/api/v1/contract/review",
        json={"text": "太短"},
        headers=headers,
    )
    assert response.status_code in (400, 422)


def test_rules_endpoint(client):
    response = client.get("/api/v1/contract/rules")
    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]
    assert len(items) >= 6
    assert {item["severity"] for item in items} >= {"high", "medium", "low"}
    assert all(item["legal_basis_status"] == "UNVERIFIED" for item in items)


def test_interest_and_penalty_are_clues_not_fixed_legal_thresholds(client, new_tenant_app):
    response = client.post(
        "/api/v1/contract/review",
        json={"text": "借款合同约定年利率为 5%，违约金为合同价款的 1%。"},
        headers=new_tenant_app["headers"],
    )
    assert response.status_code == 200, response.text
    findings = response.json()["data"]["findings"]
    assert {"INTEREST_CAP", "PENALTY_EXCESSIVE"} <= {item["rule_id"] for item in findings}
    assert "15.4%" not in response.text
    assert "30%" not in response.text
    assert all(item["requires_human_review"] for item in findings)


def test_no_findings_is_not_a_pass(client, new_tenant_app):
    response = client.post(
        "/api/v1/contract/review",
        json={"text": "约定争议解决、赔偿、支付和履行日期，其他内容由双方协商。"},
        headers=new_tenant_app["headers"],
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["finding_count"] == 0
    assert data["overall_risk"] == "REVIEW_REQUIRED"
    assert "未检出不代表无风险" in data["disclaimer"]


def test_production_and_explicit_disabled_review_are_blocked(client, new_tenant_app, monkeypatch):
    from judicial_ai.services import contract_review

    for settings in (Settings(app_env="production"), Settings(contract_review_enabled=False)):
        monkeypatch.setattr(contract_review, "get_settings", lambda settings=settings: settings)
        for endpoint in ("review", "report"):
            response = client.post(
                f"/api/v1/contract/{endpoint}",
                json={"text": HIGH_RISK_CONTRACT},
                headers=new_tenant_app["headers"],
            )
            assert response.status_code == 503, response.text
            assert response.json()["error"]["code"] == "FEATURE_DISABLED"


def test_citation_failure_is_visible_and_authorization_errors_propagate(
    client, new_tenant_app, monkeypatch
):
    from judicial_ai.services import contract_review

    headers = new_tenant_app["headers"]
    kb = client.post("/api/v1/kbs", json={"code": "citation-fail", "name": "test"}, headers=headers)
    assert kb.status_code == 201, kb.text
    payload = {"text": HIGH_RISK_CONTRACT, "kb_ids": [kb.json()["data"]["id"]]}

    def fail(*args, **kwargs):
        raise RuntimeError("sensitive upstream detail")

    monkeypatch.setattr(contract_review, "retrieve", fail)
    response = client.post("/api/v1/contract/review", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    assert all(
        item["citation_status"] == "UNAVAILABLE"
        for item in response.json()["data"]["findings"]
    )
    assert "sensitive upstream detail" not in response.text
    report = client.post("/api/v1/contract/report", json=payload, headers=headers)
    assert report.status_code == 200, report.text
    assert "引用检索不可用" in report.json()["data"]["content"]

    def forbidden(*args, **kwargs):
        raise ApiError("FORBIDDEN", "无权访问", 403)

    monkeypatch.setattr(contract_review, "retrieve", forbidden)
    response = client.post("/api/v1/contract/review", json=payload, headers=headers)
    assert response.status_code == 403, response.text


def test_empty_citations_are_distinguished_from_failures(client, new_tenant_app, monkeypatch):
    from judicial_ai.services import contract_review

    headers = new_tenant_app["headers"]
    kb = client.post(
        "/api/v1/kbs", json={"code": "citation-empty", "name": "test"}, headers=headers
    )
    assert kb.status_code == 201, kb.text
    monkeypatch.setattr(contract_review, "retrieve", lambda *args, **kwargs: {"results": []})
    response = client.post(
        "/api/v1/contract/review",
        json={"text": HIGH_RISK_CONTRACT, "kb_ids": [kb.json()["data"]["id"]]},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert all(
        item["citation_status"] == "NOT_FOUND"
        for item in response.json()["data"]["findings"]
    )
