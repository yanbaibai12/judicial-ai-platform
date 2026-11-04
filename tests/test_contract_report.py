from __future__ import annotations

RISKY = """借款合同

第一条 借款金额与利率
出借人向借款人出借人民币十万元，年利率为 28%，按月付息，利滚利计息。

第二条 违约责任
借款人逾期还款的，按未还金额的 50% 支付违约金。

第三条 争议解决
因本合同引起的争议，向合同签订地人民法院提起诉讼。
"""


def test_report_markdown(client, new_tenant_app):
    headers = new_tenant_app["headers"]
    response = client.post(
        "/api/v1/contract/report",
        json={"text": RISKY, "title": "采购借款合同", "format": "markdown"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["format"] == "md"
    assert data["overall_risk"] == "HIGH"
    assert data["finding_count"] >= 2
    content = data["content"]
    assert content.startswith("# 合同风险审查报告")
    assert "采购借款合同" in content
    assert "高风险" in content or "高" in content
    assert "借款利率条款待核查" in content
    assert "待核验法条线索" in content
    assert "引用状态" in content
    assert "民法典" in content
    assert "AI 辅助" in content
    # 高风险应排在报告前部
    first_high = content.find("借款利率条款待核查")
    first_low = content.find("自动续期")
    assert first_high != -1
    if first_low != -1:
        assert first_high < first_low


def test_report_html(client, new_tenant_app):
    headers = new_tenant_app["headers"]
    response = client.post(
        "/api/v1/contract/report",
        json={"text": RISKY, "title": "测试<合同>", "format": "html"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["format"] == "html"
    content = data["content"]
    assert content.startswith("<!doctype html>")
    assert "合同风险审查报告" in content
    assert "测试&lt;合同&gt;" in content  # HTML 转义防注入
    assert "finding high" in content
    assert "未经核验" in content


def test_report_rejects_bad_format(client, new_tenant_app):
    headers = new_tenant_app["headers"]
    response = client.post(
        "/api/v1/contract/report",
        json={"text": RISKY, "format": "pdf"},
        headers=headers,
    )
    assert response.status_code == 400
