from __future__ import annotations

import json
import uuid

from .conftest import register_chat_capability


def _unique_code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _create_dataset(client, headers, dataset_type: str = "SFT") -> int:
    response = client.post(
        "/api/v1/datasets",
        json={
            "code": _unique_code("gov"),
            "name": "治理闭环测试集",
            "type": dataset_type,
            "owner": "test",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


def _create_event_version(
    client,
    headers,
    dataset_id: int,
    *,
    event_types: list[str] | None = None,
    filter_config: dict | None = None,
    publish: bool = False,
) -> dict:
    response = client.post(
        f"/api/v1/datasets/{dataset_id}/versions",
        json={
            "version": "v1.0.0",
            "source_snapshot": {
                "event_types": event_types or ["INFERENCE_COMPLETED"],
                "from": "2000-01-01T00:00:00",
            },
            "filter_config": filter_config or {"redact": True, "dedup": True},
            "split": {"train": 1.0, "validation": 0.0, "test": 0.0},
            "publish": publish,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _invoke_chat(client, headers, code: str, content: str) -> str:
    response = client.post(
        f"/api/v1/capabilities/{code}/invoke",
        json={"input": {"messages": [{"role": "user", "content": content}]}},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["request_id"]


def test_event_redaction_dataset_lineage_and_export(client, admin_headers, new_tenant_app):
    headers = new_tenant_app["headers"]
    code = register_chat_capability(client, admin_headers, new_tenant_app["app_id"])
    phone = "13800138000"
    id_card = "110101199003071234"
    request_id = _invoke_chat(
        client,
        headers,
        code,
        f"请分析民间借贷利率，联系电话{phone}，身份证号{id_card}，依据民法典第六百八十条。",
    )

    response = client.get(
        f"/api/v1/events?request_id={request_id}",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    events = response.json()["data"]["items"]
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "INFERENCE_COMPLETED"
    payload_text = json.dumps(event["payload"], ensure_ascii=False)
    assert phone not in payload_text
    assert id_card not in payload_text
    assert "[手机号]" in payload_text
    assert "[身份证号]" in payload_text

    dataset_id = _create_dataset(client, headers, "SFT")
    version = _create_event_version(client, headers, dataset_id, publish=True)
    assert version["status"] == "PUBLISHED"
    assert version["prelabel_model"] == "rule-based/v1"
    assert version["stats"]["total"] >= 1
    assert version["stats"]["redaction"]["phone"] >= 1
    assert version["stats"]["redaction"]["id_card"] >= 1
    assert version["stats"]["train"] == version["stats"]["total"]

    response = client.get(
        f"/api/v1/dataset-versions/{version['id']}/items", headers=headers
    )
    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]
    assert items
    assert phone not in json.dumps(items, ensure_ascii=False)
    assert items[0]["review_status"] in {"AUTO_ACCEPTED", "NEEDS_REVIEW"}

    response = client.get(
        f"/api/v1/dataset-versions/{version['id']}/lineage", headers=headers
    )
    assert response.status_code == 200, response.text
    stages = [item["stage"] for item in response.json()["data"]["items"]]
    assert stages[:5] == ["COLLECT", "DESENSITIZE", "QUALITY", "DEDUP", "SPLIT"]
    assert "PUBLISH" in stages

    response = client.post(
        f"/api/v1/dataset-versions/{version['id']}/export",
        json={"format": "jsonl", "approved_by": "reviewer-1"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["x-data-watermark"]
    rows = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert rows
    assert rows[0]["_watermark"].startswith(response.headers["x-data-watermark"])


def test_annotation_reviews_and_agreement(client, admin_headers, new_tenant_app):
    headers = new_tenant_app["headers"]
    code = register_chat_capability(client, admin_headers, new_tenant_app["app_id"])
    _invoke_chat(client, headers, code, "请解释民法典第六百八十条关于借款利息的规定。")

    dataset_id = _create_dataset(client, headers, "SFT")
    version = _create_event_version(client, headers, dataset_id, publish=True)
    response = client.get(
        f"/api/v1/dataset-versions/{version['id']}/items", headers=headers
    )
    item_id = response.json()["data"]["items"][0]["id"]

    response = client.post(
        "/api/v1/annotation-tasks",
        json={
            "name": "双人复核",
            "task_type": "QA",
            "dataset_version_id": version["id"],
            "required_reviewers": 2,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    task = response.json()["data"]
    assert task["total_items"] == version["stats"]["total"]

    response = client.post(
        f"/api/v1/annotation-tasks/{task['id']}/claim", headers=headers
    )
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "IN_PROGRESS"

    for reviewer_id in (101, 102):
        response = client.post(
            f"/api/v1/annotation-tasks/{task['id']}/records",
            json={
                "item_id": item_id,
                "reviewer_id": reviewer_id,
                "label": {"approved": True},
                "decision": "APPROVED",
                "confidence": 0.9,
            },
            headers=headers,
        )
        assert response.status_code == 201, response.text

    response = client.post(
        f"/api/v1/annotation-tasks/{task['id']}/records",
        json={"item_id": item_id, "reviewer_id": 101, "decision": "REJECTED"},
        headers=headers,
    )
    assert response.status_code == 409

    response = client.get(
        f"/api/v1/annotation-tasks/{task['id']}/stats", headers=headers
    )
    assert response.status_code == 200, response.text
    stats = response.json()["data"]
    assert stats["records"] == 2
    assert stats["agreement_rate"] == 1.0
    assert stats["cohen_kappa"] == 1.0
    assert stats["approval_rate"] == 1.0
    assert stats["status"] == "COMPLETED"

    response = client.get(
        f"/api/v1/dataset-versions/{version['id']}/items", headers=headers
    )
    assert response.json()["data"]["items"][0]["review_status"] == "APPROVED"


def test_text_and_retrieval_eval(client, admin_headers, seeded, demo_headers):
    _invoke_chat(
        client,
        demo_headers,
        "llm.qwen3.chat",
        "请依据民法典说明借款合同利息约定不明确时如何处理。",
    )

    dataset_id = _create_dataset(client, demo_headers, "EVAL")
    version = _create_event_version(client, demo_headers, dataset_id, publish=True)
    response = client.post(
        "/api/v1/evals",
        json={
            "dataset_version_id": version["id"],
            "target_type": "MODEL",
            "target_version": "Qwen3-8B",
            "config": {"pass_threshold": 0.5, "gates": {"exact_match": 0.5}},
        },
        headers=demo_headers,
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["status"] == "SUCCEEDED"
    assert data["metrics"]["samples"] >= 1
    assert data["metrics"]["exact_match"] == 1.0
    assert data["metrics"]["gate_passed"] is True

    response = client.get(f"/api/v1/evals/{data['id']}", headers=demo_headers)
    assert response.status_code == 200
    assert response.json()["data"]["results"]

    result = client.post(
        "/v1/retrieve",
        json={"kb_ids": [seeded["kb_id"]], "query": "民间借贷利率四倍", "top_k": 3},
        headers=demo_headers,
    )
    assert result.status_code == 200, result.text
    retrieved = result.json()["results"]
    assert retrieved
    request_id = result.json()["request_id"]

    response = client.get(
        f"/api/v1/events?request_id={request_id}", headers=demo_headers
    )
    events = response.json()["data"]["items"]
    assert events and events[0]["event_type"] == "RETRIEVAL_COMPLETED"
    assert events[0]["payload"]["chunk_ids"]

    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "case.search",
                "arguments": {"case_no": "(2024)京01民初1号"},
            },
        },
        headers=demo_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["result"]["isError"] is False
    response = client.get("/api/v1/events?event_type=TOOL_CALLED", headers=demo_headers)
    assert response.json()["data"]["total"] >= 1

    retrieval_dataset_id = _create_dataset(client, demo_headers, "RETRIEVAL_EVAL")
    retrieval_version = _create_event_version(
        client,
        demo_headers,
        retrieval_dataset_id,
        event_types=["RETRIEVAL_COMPLETED"],
    )
    response = client.post(
        "/api/v1/evals",
        json={
            "dataset_version_id": retrieval_version["id"],
            "target_type": "RETRIEVAL",
            "target_version": "hybrid-v1",
            "config": {"top_k": 3, "gates": {"recall_at_k": 0.9}},
        },
        headers=demo_headers,
    )
    assert response.status_code == 201, response.text
    retrieval_metrics = response.json()["data"]["metrics"]
    assert retrieval_metrics["samples"] >= 1
    assert retrieval_metrics["recall_at_k"] == 1.0
    assert retrieval_metrics["gate_passed"] is True


def test_bad_case_regression_is_idempotent(client, admin_headers, new_tenant_app):
    headers = new_tenant_app["headers"]
    response = client.post(
        "/api/v1/bad-cases",
        json={
            "request_id": "req-regression-1",
            "category": "CITATION",
            "sub_category": "CITATION_NOT_SUPPORT",
            "priority": "P1",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    case_id = response.json()["data"]["id"]

    response = client.patch(
        f"/api/v1/bad-cases/{case_id}",
        json={"root_cause": "引用不匹配", "add_to_regression": True},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["regression_case_id"]

    response = client.patch(
        f"/api/v1/bad-cases/{case_id}",
        json={"add_to_regression": True},
        headers=headers,
    )
    assert response.status_code == 200, response.text

    response = client.get("/api/v1/datasets", headers=headers)
    datasets = [
        item
        for item in response.json()["data"]["items"]
        if item["code"] == "bad-case-regression"
    ]
    assert len(datasets) == 1
    response = client.get(
        f"/api/v1/datasets/{datasets[0]['id']}/versions", headers=headers
    )
    versions = response.json()["data"]["items"]
    assert len(versions) == 1
    response = client.get(
        f"/api/v1/dataset-versions/{versions[0]['id']}/items", headers=headers
    )
    items = response.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["label"]["bad_case_id"] == case_id


def test_metrics_and_retention(client, admin_headers, new_tenant_app):
    headers = new_tenant_app["headers"]
    code = register_chat_capability(client, admin_headers, new_tenant_app["app_id"])
    _invoke_chat(client, headers, code, "请总结民间借贷司法解释。")

    response = client.get(
        f"/api/v1/governance/metrics?tenant_id={new_tenant_app['tenant_id']}",
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    metrics = response.json()["data"]
    assert metrics["events"]["total"] >= 1
    assert metrics["events"]["by_type"]["INFERENCE_COMPLETED"] >= 1

    response = client.post(
        f"/api/v1/events/process?tenant_id={new_tenant_app['tenant_id']}",
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["processed"] >= 1

    response = client.get(
        f"/api/v1/events?tenant_id={new_tenant_app['tenant_id']}",
        headers=admin_headers,
    )
    event = response.json()["data"]["items"][0]
    assert event["status"] == "PROCESSED"

    response = client.post(
        f"/api/v1/events/{event['event_id']}/replay", headers=admin_headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "NEW"

    response = client.post(
        "/api/v1/retention-jobs",
        json={
            "tenant_id": new_tenant_app["tenant_id"],
            "target": "AI_EVENT",
            "before": "2999-01-01T00:00:00",
            "dry_run": True,
        },
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    job = response.json()["data"]
    assert job["deleted_count"] >= 1
    assert job["detail"]["dry_run"] is True

    response = client.get(
        f"/api/v1/events?tenant_id={new_tenant_app['tenant_id']}",
        headers=admin_headers,
    )
    assert response.json()["data"]["total"] >= 1

    response = client.post(
        "/api/v1/retention-jobs",
        json={
            "tenant_id": new_tenant_app["tenant_id"],
            "target": "AI_EVENT",
            "before": "2999-01-01T00:00:00",
        },
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["data"]["deleted_count"] >= 1

    response = client.get(
        f"/api/v1/events?tenant_id={new_tenant_app['tenant_id']}",
        headers=admin_headers,
    )
    assert response.json()["data"]["total"] == 0
