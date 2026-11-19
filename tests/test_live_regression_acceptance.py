from __future__ import annotations

import os
import time
import uuid

import httpx
import pytest

BASE_URL = os.getenv("JAI_TEST_LIVE_URL")
ADMIN_TOKEN = os.getenv("JAI_TEST_LIVE_ADMIN_TOKEN")
pytestmark = pytest.mark.skipif(
    not BASE_URL or not ADMIN_TOKEN,
    reason="explicit isolated live server and admin token are required",
)


def test_live_document_feedback_and_worker_regression():
    suffix = uuid.uuid4().hex[:8]
    admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    with httpx.Client(base_url=BASE_URL, timeout=180) as client:
        def post(path, body, headers, expected=201):
            response = client.post(path, json=body, headers=headers)
            assert response.status_code == expected, response.text
            return response.json()["data"]

        def wait_job(job_id, headers):
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                response = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
                assert response.status_code == 200, response.text
                job = response.json()["data"]
                if job["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                    assert job["status"] == "SUCCEEDED", job
                    return job
                time.sleep(0.2)
            pytest.fail(f"job did not complete: {job_id}")

        tenant = post("/api/v1/admin/tenants", {"code": f"live-{suffix}", "name": "test"}, admin)
        permissions = [
            "app:invoke", "kb:read", "kb:write", "kb:promote",
            "governance:read", "governance:write", "job:read", "job:write",
        ]

        def app(code, tenant_id=None):
            record = post(
                "/api/v1/admin/apps",
                {"tenant_id": tenant_id or tenant["id"], "code": f"{code}-{suffix}",
                 "name": code, "permissions": permissions},
                admin,
            )
            credential = post(f"/api/v1/admin/apps/{record['id']}/credentials", {}, admin)
            return record["id"], {"Authorization": f"Bearer {credential['api_key']}"}

        _, owner = app("owner")
        reader_id, reader = app("reader")
        other_tenant = post(
            "/api/v1/admin/tenants", {"code": f"other-{suffix}", "name": "other"}, admin,
        )
        _, outsider = app("outsider", other_tenant["id"])
        kb = post("/api/v1/kbs", {"code": f"live-{suffix}", "name": "test"}, owner)
        kb_id = kb["id"]
        document = post(
            f"/api/v1/kbs/{kb_id}/documents",
            {"title": "integration test", "text": f"synthetic integration evidence {suffix}"},
            owner,
        )
        job = wait_job(document["index_job_id"], owner)
        index = job["result"]["index"]
        assert index["status"] == "PENDING_APPROVAL"
        versions = client.get(f"/api/v1/kbs/{kb_id}/versions", headers=owner)
        assert versions.status_code == 200, versions.text
        kb_version = next(
            item["version"] for item in versions.json()["data"]["kb_versions"]
            if item["id"] == index["kb_version_id"]
        )
        alias = {"alias": "prod", "kb_version": kb_version, "index_version": index["version"]}
        response = client.post(f"/api/v1/kbs/{kb_id}/aliases", json=alias, headers=owner)
        assert response.status_code == 409
        post(f"/api/v1/kbs/{kb_id}/indexes/{index['id']}/approve",
             {"reason": "isolated integration test"}, admin, expected=200)
        post(f"/api/v1/kbs/{kb_id}/aliases", alias, owner, expected=200)
        query = {"kb_ids": [kb_id], "query": f"synthetic integration evidence {suffix}", "top_k": 3}
        assert client.post("/v1/retrieve", json=query, headers=reader).status_code == 403
        assert client.post("/v1/retrieve", json=query, headers=outsider).status_code in {403, 404}
        post(
            f"/api/v1/kbs/{kb_id}/grants",
            {"subject_kind": "app", "subject_id": str(reader_id), "level": "read"},
            owner,
        )
        response = client.post("/v1/retrieve", json=query, headers=reader)
        assert response.status_code == 200, response.text
        retrieval = response.json()
        assert retrieval["citation"]["status"] == "VERIFIED"
        hit = retrieval["results"][0]
        source = client.get(hit["citation"]["source_url"], headers=reader)
        assert source.status_code == 200, source.text
        assert any(chunk["chunk_id"] == hit["chunk_id"]
                   for chunk in source.json()["data"]["chunks"])

        feedback = post(
            "/api/v1/feedback",
            {"request_id": retrieval["request_id"], "rating": "NEGATIVE"}, reader,
        )
        second_feedback = post(
            "/api/v1/feedback",
            {"request_id": retrieval["request_id"], "rating": "NEGATIVE"}, reader,
        )
        dataset = post(
            "/api/v1/datasets",
            {"code": f"live-eval-{suffix}", "name": "test", "type": "RETRIEVAL_EVAL"},
            reader,
        )
        version = post(
            f"/api/v1/datasets/{dataset['id']}/versions",
            {"version": "v1",
             "source_snapshot": {
                 "source": "retrieval_feedback",
                 "feedback_ids": [feedback["id"], second_feedback["id"]],
             },
             "split": {"train": 0, "validation": 0, "test": 1}},
            reader,
        )
        version_id = version["id"]
        response = client.get(f"/api/v1/dataset-versions/{version_id}/items", headers=reader)
        assert response.status_code == 200, response.text
        samples = response.json()["data"]["items"]
        assert len(samples) == 2
        assert all(sample["review_status"] == "NEEDS_REVIEW" for sample in samples)
        task = post(
            "/api/v1/annotation-tasks",
            {"name": "test review", "task_type": "RETRIEVAL",
             "dataset_version_id": version_id, "required_reviewers": 1},
            reader,
        )
        post(
            f"/api/v1/annotation-tasks/{task['id']}/records",
            {"item_id": samples[0]["id"], "decision": "APPROVED",
             "label": {"relevant_chunk_ids": [hit["chunk_id"]]}},
            reader,
        )
        post(
            f"/api/v1/annotation-tasks/{task['id']}/records",
            {"item_id": samples[1]["id"], "decision": "REJECTED"},
            reader,
        )
        post(
            f"/api/v1/dataset-versions/{version_id}/items/exclude",
            {"item_ids": [samples[1]["id"]], "reason": "integration test"},
            reader, expected=200,
        )
        post(f"/api/v1/dataset-versions/{version_id}/publish", {}, reader, expected=200)
        response = client.post(
            f"/api/v1/dataset-versions/{version_id}/export",
            json={"format": "json"}, headers=reader,
        )
        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 1
        evaluation = post(
            "/api/v1/evals",
            {"dataset_version_id": version_id, "target_type": "RETRIEVAL",
             "target_version": "live", "config": {"gates": {"recall_at_k": 1.0}}},
            reader, expected=202,
        )
        wait_job(evaluation["job"]["id"], reader)
        response = client.get(f"/api/v1/evals/{evaluation['id']}", headers=reader)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == "SUCCEEDED"
        assert response.json()["data"]["metrics"]["gate_passed"] is True

        bad_case = post(
            "/api/v1/bad-cases",
            {"request_id": retrieval["request_id"], "category": "RETRIEVAL"}, reader,
        )
        bad_version = post(
            f"/api/v1/datasets/{dataset['id']}/versions",
            {"version": "bad-case",
             "source_snapshot": {
                 "source": "retrieval_bad_case", "bad_case_ids": [bad_case["id"]],
             },
             "split": {"train": 0, "validation": 0, "test": 1}},
            reader,
        )
        bad_version_id = bad_version["id"]
        response = client.get(
            f"/api/v1/dataset-versions/{bad_version_id}/items", headers=reader,
        )
        assert response.status_code == 200, response.text
        bad_sample = response.json()["data"]["items"][0]
        assert bad_sample["review_status"] == "NEEDS_REVIEW"
        assert bad_sample["metadata"]["bad_case_id"] == bad_case["id"]
        bad_task = post(
            "/api/v1/annotation-tasks",
            {"name": "bad case review", "task_type": "RETRIEVAL",
             "dataset_version_id": bad_version_id, "required_reviewers": 1},
            reader,
        )
        post(
            f"/api/v1/annotation-tasks/{bad_task['id']}/records",
            {"item_id": bad_sample["id"], "decision": "APPROVED",
             "label": {"relevant_chunk_ids": [hit["chunk_id"]]}},
            reader,
        )
        post(f"/api/v1/dataset-versions/{bad_version_id}/publish", {}, reader, expected=200)
        bad_eval = post(
            "/api/v1/evals",
            {"dataset_version_id": bad_version_id, "target_type": "RETRIEVAL",
             "target_version": "bad-case", "config": {"gates": {"recall_at_k": 1.0}}},
            reader, expected=202,
        )
        wait_job(bad_eval["job"]["id"], reader)
        response = client.get(f"/api/v1/evals/{bad_eval['id']}", headers=reader)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["status"] == "SUCCEEDED"
        assert response.json()["data"]["metrics"]["gate_passed"] is True
