from __future__ import annotations

import os
import subprocess
import sys
import uuid

import pytest

from judicial_ai.config import get_settings
from judicial_ai.db import SessionLocal
from judicial_ai.ids import new_id
from judicial_ai.models import AppCredential, DatasetItem, EvalTask, Job
from judicial_ai.security import hash_token
from judicial_ai.services.jobs import _execute_running


@pytest.fixture
def regression_case(client, admin_headers, new_tenant_app, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "resource_acl_enforced", True)
    monkeypatch.setattr(settings, "index_require_approval", False)
    monkeypatch.setattr(settings, "require_prod_alias", False)
    monkeypatch.setattr(settings, "kb_auto_index_after_upload", True)
    monkeypatch.setattr(settings, "job_mode", "inline")
    owner = new_tenant_app["headers"]
    suffix = uuid.uuid4().hex[:8]

    def post(path, body, headers=owner, expected=201):
        response = client.post(path, json=body, headers=headers)
        assert response.status_code == expected, response.text
        return response.json()["data"]

    kb = post("/api/v1/kbs", {"code": f"review-{suffix}", "name": "review"})
    kb_id = kb["id"]
    for number in (1, 2):
        post(
            f"/api/v1/kbs/{kb_id}/documents",
            {"title": f"doc-{number}", "text": f"feedback regression test {suffix} item {number}"},
        )
    response = client.post(
        "/v1/retrieve",
        json={"kb_ids": [kb_id], "query": f"feedback regression {suffix}", "top_k": 5},
        headers=owner,
    )
    assert response.status_code == 200, response.text
    search = response.json()
    chunks = [item["chunk_id"] for item in search["results"]]
    assert len(chunks) == 2
    feedback = post("/api/v1/feedback", {"request_id": search["request_id"], "rating": "NEGATIVE"})
    dataset = post(
        "/api/v1/datasets",
        {"code": f"review-eval-{suffix}", "name": "review", "type": "RETRIEVAL_EVAL"},
    )
    payload = {
        "version": "v1",
        "source_snapshot": {"source": "retrieval_feedback", "feedback_ids": [feedback["id"]]},
        "split": {"train": 0, "validation": 0, "test": 1},
    }
    version = post(f"/api/v1/datasets/{dataset['id']}/versions", payload)
    items = client.get(f"/api/v1/dataset-versions/{version['id']}/items", headers=owner)
    assert items.status_code == 200, items.text
    sample = items.json()["data"]["items"][0]
    task = post(
        "/api/v1/annotation-tasks",
        {
            "name": "review", "task_type": "RETRIEVAL",
            "dataset_version_id": version["id"], "required_reviewers": 2,
        },
    )

    def reviewer(code):
        app = post(
            "/api/v1/admin/apps",
            {
                "tenant_id": new_tenant_app["tenant_id"], "code": f"{code}-{suffix}",
                "name": code,
                "permissions": ["app:invoke", "kb:read", "governance:read", "governance:write"],
            },
            admin_headers,
        )
        response = client.post(
            f"/api/v1/admin/apps/{app['id']}/credentials", headers=admin_headers
        )
        assert response.status_code == 201, response.text
        headers = {"Authorization": f"Bearer {response.json()['data']['api_key']}"}
        post(
            f"/api/v1/kbs/{kb_id}/grants",
            {"subject_kind": "app", "subject_id": str(app["id"]), "level": "read"},
        )
        return app["id"], headers

    return {
        "owner": owner, "kb_id": kb_id, "chunks": chunks, "version": version,
        "sample": sample, "task": task, "reviewer": reviewer, "dataset": dataset,
        "feedback": feedback, "payload": payload,
    }


def test_regression_review_conflict_arbitration_and_freeze(client, regression_case):
    case = regression_case
    owner = case["owner"]
    version_id = case["version"]["id"]
    task_id = case["task"]["id"]
    item_id = case["sample"]["id"]
    path = f"/api/v1/annotation-tasks/{task_id}/records"
    duplicate_task = client.post(
        "/api/v1/annotation-tasks",
        json={"name": "bypass", "task_type": "RETRIEVAL",
              "dataset_version_id": version_id, "required_reviewers": 1},
        headers=owner,
    )
    assert duplicate_task.status_code == 409
    unpublished_eval = client.post(
        "/api/v1/evals",
        json={"dataset_version_id": version_id, "target_type": "RETRIEVAL",
              "target_version": "unpublished"},
        headers=owner,
    )
    assert unpublished_eval.status_code == 409
    label = {"relevant_chunk_ids": [case["chunks"][0]]}
    forged = client.post(
        path,
        json={"item_id": item_id, "decision": "APPROVED", "reviewer_id": 123, "label": label},
        headers=owner,
    )
    assert forged.status_code == 403
    invalid = client.post(
        path,
        json={"item_id": item_id, "decision": "APPROVED",
              "label": {"relevant_chunk_ids": ["missing"]}},
        headers=owner,
    )
    assert invalid.status_code == 403
    first = client.post(
        path, json={"item_id": item_id, "decision": "APPROVED", "label": label}, headers=owner
    )
    assert first.status_code == 201, first.text
    assert client.post(
        path, json={"item_id": item_id, "decision": "APPROVED", "label": label}, headers=owner
    ).status_code == 409
    _, second = case["reviewer"]("reviewer2")
    response = client.post(
        path,
        json={"item_id": item_id, "decision": "APPROVED",
              "label": {"relevant_chunk_ids": [case["chunks"][1]]}},
        headers=second,
    )
    assert response.status_code == 201, response.text
    sample = client.get(f"/api/v1/dataset-versions/{version_id}/items", headers=owner)
    assert sample.json()["data"]["items"][0]["review_status"] == "CONFLICT"
    assert client.post(
        f"/api/v1/dataset-versions/{version_id}/publish", headers=owner
    ).status_code == 409
    _, arbitrator = case["reviewer"]("arbitrator")
    response = client.post(
        path, json={"item_id": item_id, "decision": "ARBITRATED", "label": label},
        headers=arbitrator,
    )
    assert response.status_code == 201, response.text
    response = client.post(f"/api/v1/dataset-versions/{version_id}/publish", headers=owner)
    assert response.status_code == 200, response.text
    response = client.post(
        path, json={"item_id": item_id, "decision": "APPROVED", "label": label},
        headers=arbitrator,
    )
    assert response.status_code == 409
    response = client.post(
        "/api/v1/evals",
        json={"dataset_version_id": version_id, "target_type": "RETRIEVAL",
              "target_version": "baseline", "config": {"gates": {"not_a_metric": 0}}},
        headers=owner,
    )
    assert response.status_code == 400
    response = client.post(
        "/api/v1/evals",
        json={"dataset_version_id": version_id, "target_type": "RETRIEVAL",
              "target_version": "baseline", "config": {"gates": {"recall_at_k": 1.0}}},
        headers=owner,
    )
    assert response.status_code == 201, response.text
    evaluation = response.json()["data"]
    assert evaluation["status"] == "SUCCEEDED"
    assert evaluation["metrics"]["gate_passed"]
    assert evaluation["metrics"]["evaluated_versions"][0]["item_id"] == item_id


def test_regression_permissions_rechecked(client, regression_case):
    case = regression_case
    reviewer_id, reviewer = case["reviewer"]("revoked")
    version_id = case["version"]["id"]
    response = client.get(f"/api/v1/dataset-versions/{version_id}/items", headers=reviewer)
    assert response.status_code == 200
    response = client.delete(
        f"/api/v1/kbs/{case['kb_id']}/grants",
        params={"subject_kind": "app", "subject_id": str(reviewer_id)},
        headers=case["owner"],
    )
    assert response.status_code == 200, response.text
    response = client.get(f"/api/v1/dataset-versions/{version_id}/items", headers=reviewer)
    assert response.status_code == 403
    response = client.post(
        f"/api/v1/annotation-tasks/{case['task']['id']}/records",
        json={"item_id": case["sample"]["id"], "decision": "APPROVED",
              "label": {"relevant_chunk_ids": [case["chunks"][0]]}},
        headers=reviewer,
    )
    assert response.status_code == 403


def test_feedback_selection_rejects_invalid_or_unowned_samples(client, regression_case):
    case = regression_case
    path = f"/api/v1/datasets/{case['dataset']['id']}/versions"
    payload = case["payload"]
    duplicate = {
        **payload, "version": "duplicate",
        "source_snapshot": {"source": "retrieval_feedback",
                            "feedback_ids": [case["feedback"]["id"]] * 2},
    }
    assert client.post(path, json=duplicate, headers=case["owner"]).status_code == 400
    wrong = {**payload, "version": "wrong", "source_snapshot": {
        "source": "retrieval_feedback", "feedback_ids": [123],
    }}
    assert client.post(path, json=wrong, headers=case["owner"]).status_code == 404
    no_review = {**payload, "version": "publish", "publish": True}
    assert client.post(path, json=no_review, headers=case["owner"]).status_code == 400


def test_feedback_selection_accepts_exact_browser_ids(client, regression_case):
    case = regression_case
    path = f"/api/v1/datasets/{case['dataset']['id']}/versions"
    feedback_id = case["feedback"]["id"]
    snapshot = {"source": "retrieval_feedback", "feedback_ids": [str(feedback_id)]}
    payload = {**case["payload"], "version": "browser-id", "source_snapshot": snapshot}
    assert client.post(path, json=payload, headers=case["owner"]).status_code == 201
    for index, value in enumerate(
        ([feedback_id, str(feedback_id)], [True], ["１２３"], ["9223372036854775808"])
    ):
        payload["version"] = f"invalid-browser-id-{index}"
        payload["source_snapshot"] = {**snapshot, "feedback_ids": value}
        assert client.post(path, json=payload, headers=case["owner"]).status_code == 400


def test_delivery_review_requires_user(client, regression_case, monkeypatch):
    case = regression_case
    monkeypatch.setattr(get_settings(), "app_env", "staging")
    response = client.post(
        f"/api/v1/annotation-tasks/{case['task']['id']}/records",
        json={
            "item_id": case["sample"]["id"],
            "decision": "APPROVED",
            "label": {"relevant_chunk_ids": [case["chunks"][0]]},
        },
        headers=case["owner"],
    )
    assert response.status_code == 403


def test_explicit_empty_retrieval_is_evaluated_separately(client, regression_case, monkeypatch):
    case = regression_case
    path = f"/api/v1/annotation-tasks/{case['task']['id']}/records"
    body = {
        "item_id": case["sample"]["id"],
        "decision": "APPROVED",
        "label": {"relevant_chunk_ids": [], "expect_empty": True},
    }
    assert client.post(path, json=body, headers=case["owner"]).status_code == 201
    _, reviewer = case["reviewer"]("empty-reviewer")
    assert client.post(path, json=body, headers=reviewer).status_code == 201
    version_id = case["version"]["id"]
    assert client.post(
        f"/api/v1/dataset-versions/{version_id}/publish", headers=case["owner"]
    ).status_code == 200
    payload = {
        "dataset_version_id": version_id, "target_type": "RETRIEVAL",
        "target_version": "empty", "config": {"gates": {"empty_accuracy": 1.0}},
    }
    response = client.post("/api/v1/evals", json=payload, headers=case["owner"])
    metrics = response.json()["data"]["metrics"]
    assert metrics["empty_samples"] == 1
    assert metrics["empty_accuracy"] == 0
    assert metrics["gate_passed"] is False
    monkeypatch.setattr(
        "judicial_ai.services.governance.retrieve",
        lambda *args, **kwargs: {"results": [], "versions": {}},
    )
    response = client.post("/api/v1/evals", json=payload, headers=case["owner"])
    metrics = response.json()["data"]["metrics"]
    assert metrics["empty_accuracy"] == 1
    assert metrics["gate_passed"] is True
    assert metrics["relevant_samples"] == 0

    payload["config"]["gates"] = {"recall_at_k": 0.5}
    response = client.post("/api/v1/evals", json=payload, headers=case["owner"])
    metrics = response.json()["data"]["metrics"]
    assert metrics["gates"]["recall_at_k"] is False
    assert metrics["gate_passed"] is False


def test_bad_case_can_create_reviewable_retrieval_samples(client, regression_case):
    case = regression_case
    response = client.post(
        "/api/v1/bad-cases",
        json={"request_id": case["feedback"]["request_id"], "category": "RETRIEVAL"},
        headers=case["owner"],
    )
    assert response.status_code == 201, response.text
    bad_case_id = response.json()["data"]["id"]
    response = client.post(
        f"/api/v1/datasets/{case['dataset']['id']}/versions",
        json={
            "version": "bad-case",
            "source_snapshot": {
                "source": "retrieval_bad_case", "bad_case_ids": [bad_case_id],
            },
            "split": {"train": 0, "validation": 0, "test": 1},
        },
        headers=case["owner"],
    )
    assert response.status_code == 201, response.text
    version_id = response.json()["data"]["id"]
    response = client.get(
        f"/api/v1/dataset-versions/{version_id}/items", headers=case["owner"]
    )
    sample = response.json()["data"]["items"][0]
    assert sample["metadata"]["bad_case_id"] == bad_case_id
    assert sample["review_status"] == "NEEDS_REVIEW"
    assert sample["label"]["relevant_chunk_ids"] == []
    assert client.post(
        f"/api/v1/dataset-versions/{version_id}/publish", headers=case["owner"]
    ).status_code == 409


def test_rejected_sample_can_be_reviewed_again_without_erasing_history(client, regression_case):
    case = regression_case
    owner = case["owner"]
    _, second = case["reviewer"]("retry-reviewer")
    first_task = case["task"]["id"]
    item_id = case["sample"]["id"]
    first_path = f"/api/v1/annotation-tasks/{first_task}/records"
    rejected = {"item_id": item_id, "decision": "REJECTED"}
    assert client.post(first_path, json=rejected, headers=owner).status_code == 201
    assert client.post(first_path, json=rejected, headers=second).status_code == 201
    version_id = case["version"]["id"]
    assert client.post(
        f"/api/v1/dataset-versions/{version_id}/publish", headers=owner
    ).status_code == 409
    payload = {
        "name": "re-review", "task_type": "RETRIEVAL",
        "dataset_version_id": version_id, "required_reviewers": 1,
    }
    assert client.post("/api/v1/annotation-tasks", json=payload, headers=owner).status_code == 400
    payload["required_reviewers"] = 2
    response = client.post("/api/v1/annotation-tasks", json=payload, headers=owner)
    assert response.status_code == 201, response.text
    second_path = f"/api/v1/annotation-tasks/{response.json()['data']['id']}/records"
    approved = {
        "item_id": item_id, "decision": "APPROVED",
        "label": {"relevant_chunk_ids": [case["chunks"][0]]},
    }
    assert client.post(second_path, json=approved, headers=owner).status_code == 201
    assert client.post(second_path, json=approved, headers=second).status_code == 201
    stats = client.get(f"/api/v1/annotation-tasks/{first_task}/stats", headers=owner)
    assert stats.json()["data"]["decision_counts"] == {"REJECTED": 2}
    assert client.post(
        f"/api/v1/dataset-versions/{version_id}/publish", headers=owner
    ).status_code == 200


def test_excluded_rejected_sample_is_preserved_but_not_evaluated(client, regression_case):
    case = regression_case
    owner = case["owner"]
    _, second = case["reviewer"]("exclude-reviewer")
    with SessionLocal() as db:
        original = db.get(DatasetItem, case["sample"]["id"])
        excluded_id = new_id()
        db.add(DatasetItem(
            id=excluded_id, dataset_version_id=original.dataset_version_id,
            item_type=original.item_type, input=original.input, output=original.output,
            label={"relevant_chunk_ids": []}, extra=original.extra,
            review_status="NEEDS_REVIEW", split="test",
        ))
        db.commit()
    path = f"/api/v1/annotation-tasks/{case['task']['id']}/records"
    approved = {
        "item_id": case["sample"]["id"], "decision": "APPROVED",
        "label": {"relevant_chunk_ids": [case["chunks"][0]]},
    }
    rejected = {"item_id": excluded_id, "decision": "REJECTED"}
    for body in (approved, rejected):
        assert client.post(path, json=body, headers=owner).status_code == 201
        assert client.post(path, json=body, headers=second).status_code == 201
    version_id = case["version"]["id"]
    exclude = f"/api/v1/dataset-versions/{version_id}/items/exclude"
    response = client.post(
        exclude, json={"item_ids": [excluded_id], "reason": "not suitable"}, headers=owner
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["stats"]["excluded"] == 1
    assert response.json()["data"]["stats"]["included"] == 1
    items = client.get(f"/api/v1/dataset-versions/{version_id}/items", headers=owner)
    excluded = next(item for item in items.json()["data"]["items"] if item["id"] == excluded_id)
    assert excluded["review_status"] == "EXCLUDED"
    assert excluded["metadata"]["exclusion"]["reason"] == "not suitable"
    assert client.post(
        f"/api/v1/dataset-versions/{version_id}/publish", headers=owner
    ).status_code == 200
    assert client.post(
        exclude, json={"item_ids": [excluded_id], "reason": "again"}, headers=owner
    ).status_code == 409
    exported = client.post(
        f"/api/v1/dataset-versions/{version_id}/export",
        json={"format": "json"}, headers=owner,
    )
    assert exported.status_code == 200, exported.text
    assert len(exported.json()["items"]) == 1
    response = client.post(
        "/api/v1/evals",
        json={"dataset_version_id": version_id, "target_type": "RETRIEVAL",
              "target_version": "exclusion"},
        headers=owner,
    )
    assert response.status_code == 201, response.text
    assert response.json()["data"]["metrics"]["samples"] == 1


def test_dataset_with_only_excluded_samples_cannot_publish(client, regression_case):
    case = regression_case
    _, second = case["reviewer"]("exclude-all-reviewer")
    path = f"/api/v1/annotation-tasks/{case['task']['id']}/records"
    rejected = {"item_id": case["sample"]["id"], "decision": "REJECTED"}
    assert client.post(path, json=rejected, headers=case["owner"]).status_code == 201
    assert client.post(path, json=rejected, headers=second).status_code == 201
    version_id = case["version"]["id"]
    response = client.post(
        f"/api/v1/dataset-versions/{version_id}/items/exclude",
        json={"item_ids": [case["sample"]["id"]], "reason": "unsuitable"},
        headers=case["owner"],
    )
    assert response.status_code == 200, response.text
    assert client.post(
        f"/api/v1/dataset-versions/{version_id}/publish", headers=case["owner"]
    ).status_code == 409


@pytest.mark.parametrize(
    "revoked", ["grant", "credential", "none", "process", "grant_process", "credential_process"]
)
def test_queued_regression_rechecks_authorization(
    client, regression_case, monkeypatch, admin_headers, revoked
):
    case = regression_case
    reviewer_id, reviewer = case["reviewer"]("worker-reviewer")
    path = f"/api/v1/annotation-tasks/{case['task']['id']}/records"
    body = {
        "item_id": case["sample"]["id"], "decision": "APPROVED",
        "label": {"relevant_chunk_ids": [case["chunks"][0]]},
    }
    assert client.post(path, json=body, headers=case["owner"]).status_code == 201
    assert client.post(path, json=body, headers=reviewer).status_code == 201
    version_id = case["version"]["id"]
    assert client.post(
        f"/api/v1/dataset-versions/{version_id}/publish", headers=case["owner"]
    ).status_code == 200
    monkeypatch.setattr(get_settings(), "job_mode", "worker")
    response = client.post(
        "/api/v1/evals",
        json={"dataset_version_id": version_id, "target_type": "RETRIEVAL",
              "target_version": "worker"},
        headers=reviewer,
    )
    assert response.status_code == 202, response.text
    result = response.json()["data"]
    if revoked in {"grant", "grant_process"}:
        response = client.delete(
            f"/api/v1/kbs/{case['kb_id']}/grants",
            params={"subject_kind": "app", "subject_id": str(reviewer_id)},
            headers=case["owner"],
        )
        assert response.status_code == 200, response.text
    with SessionLocal() as db:
        if revoked in {"credential", "credential_process"}:
            token = reviewer["Authorization"].removeprefix("Bearer ")
            db.query(AppCredential).filter_by(key_hash=hash_token(token)).one().status = "REVOKED"
            db.commit()
        job = db.get(Job, result["job"]["id"])
        if revoked in {"process", "grant_process", "credential_process"}:
            env = {
                **os.environ,
                "JOB_MODE": "worker",
                "RESOURCE_ACL_ENFORCED": "true",
                "REQUIRE_PROD_ALIAS": "false",
                "VECTOR_BACKEND": "sql",
            }
            completed = subprocess.run(
                [sys.executable, "-m", "judicial_ai.worker", "--once", "--batch-size", "100"],
                env=env, capture_output=True, text=True, timeout=60, check=False,
            )
            assert completed.returncode == 0, completed.stderr
            db.refresh(job)
        else:
            job.status = "RUNNING"
            job.attempts = 1
            db.commit()
            job = _execute_running(db, job, from_worker=True)
        task = db.get(EvalTask, result["id"])
        db.refresh(task)
        expected = "SUCCEEDED" if revoked in {"none", "process"} else "FAILED"
        assert job.status == expected
        assert task.status == expected
    if revoked in {"grant", "grant_process"}:
        response = client.post(
            f"/api/v1/kbs/{case['kb_id']}/grants",
            json={"subject_kind": "app", "subject_id": str(reviewer_id), "level": "read"},
            headers=case["owner"],
        )
        assert response.status_code == 201, response.text
        response = client.post(
            f"/api/v1/jobs/{result['job']['id']}/retry", headers=admin_headers
        )
        assert response.status_code == 200, response.text
        with SessionLocal() as db:
            task = db.get(EvalTask, result["id"])
            assert task.status == "PENDING"
            job = db.get(Job, result["job"]["id"])
            if revoked == "grant":
                job.status = "RUNNING"
                job.attempts = 1
                db.commit()
                job = _execute_running(db, job, from_worker=True)
                db.refresh(task)
                assert job.status == task.status == "SUCCEEDED"
        if revoked == "grant_process":
            completed = subprocess.run(
                [sys.executable, "-m", "judicial_ai.worker", "--once", "--batch-size", "100"],
                env=env, capture_output=True, text=True, timeout=60, check=False,
            )
            assert completed.returncode == 0, completed.stderr
            with SessionLocal() as db:
                assert db.get(Job, result["job"]["id"]).status == "SUCCEEDED"
                assert db.get(EvalTask, result["id"]).status == "SUCCEEDED"
