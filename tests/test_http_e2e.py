from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import uuid

import httpx
import pytest


def test_http_chunked_request_limit(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = os.environ.copy()
    env.update(
        DATABASE_URL=f"sqlite:///{(tmp_path / 'limit.db').as_posix()}",
        DATA_DIR=str(tmp_path),
        MAX_REQUEST_BYTES="512",
        INFERENCE_MODE="mock",
        RATE_LIMIT_ENABLED="false",
        APP_ENV="dev",
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "judicial_ai.main:app", "--host", "127.0.0.1",
         "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
            for _ in range(100):
                if process.poll() is not None:
                    raise AssertionError("HTTP server exited before becoming ready")
                try:
                    if client.get("/readyz").status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                time.sleep(0.1)
            else:
                raise AssertionError("HTTP server did not become ready")
            response = client.post(
                "/api/v1/kbs",
                content=iter([b"x" * 300, b"x" * 300]),
                headers={"content-type": "application/json", "x-request-id": "chunked-e2e"},
            )
            assert response.status_code == 413
            assert response.json()["error"]["code"] == "REQUEST_TOO_LARGE"
            assert response.headers["x-request-id"] == "chunked-e2e"
            assert client.get("/readyz").status_code == 200
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.mark.parametrize("job_mode", ["inline", "worker", "native-sql", "native"])
def test_http_kb_lifecycle_and_authorization(tmp_path, job_mode):
    native = job_mode in {"native", "native-sql"}
    milvus = job_mode == "native"
    if native and not os.environ.get("JAI_NATIVE_TEST_DATABASE_URL"):
        pytest.skip("JAI_NATIVE_TEST_DATABASE_URL not configured")
    if milvus and not os.environ.get("MILVUS_URI"):
        pytest.skip("MILVUS_URI not configured")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = os.environ.copy()
    env.update(
        DATABASE_URL=(
            os.environ["JAI_NATIVE_TEST_DATABASE_URL"]
            if native else f"sqlite:///{(tmp_path / 'e2e.db').as_posix()}"
        ),
        DATA_DIR=str(tmp_path),
        ADMIN_TOKEN="http-e2e-admin",
        INFERENCE_MODE="mock",
        RESOURCE_ACL_ENFORCED="true",
        JOB_MODE="worker" if native else job_mode,
        WORKER_POLL_SECONDS="0.1",
        REDIS_URL="redis://127.0.0.1:6379/0" if native else "",
        REDIS_KEY_PREFIX=f"jai-e2e-{uuid.uuid4().hex}",
        VECTOR_BACKEND="milvus" if milvus else "sql",
        MILVUS_URI="http://127.0.0.1:19530",
        VECTOR_FALLBACK_SQL="false",
        GRAPH_ENABLED="true" if native else "false",
        GRAPH_BACKEND="neo4j" if native else "sql",
        NEO4J_URI="bolt://127.0.0.1:7687",
        NEO4J_PASSWORD="",
        RATE_LIMIT_ENABLED="true" if native else "false",
        RATE_LIMIT_FAIL_OPEN="false",
        APP_ENV="dev",
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "judicial_ai.main:app", "--host", "127.0.0.1",
         "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    worker = None
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", timeout=90 if native else 10
        ) as client:
            for _ in range(100):
                if process.poll() is not None:
                    raise AssertionError("HTTP server exited before becoming ready")
                try:
                    if client.get("/readyz").status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                time.sleep(0.1)
            else:
                raise AssertionError("HTTP server did not become ready")
            if job_mode in {"worker", "native", "native-sql"}:
                worker = subprocess.Popen(
                    [sys.executable, "-m", "judicial_ai.worker"],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

            admin = {"Authorization": "Bearer http-e2e-admin"}

            def post(path, body, headers):
                response = client.post(path, json=body, headers=headers)
                assert response.status_code == 201, response.text
                return response.json()["data"]

            code = f"e2e-{uuid.uuid4().hex[:8]}"
            tenant = post("/api/v1/admin/tenants", {"code": code, "name": "E2E"}, admin)
            app = post(
                "/api/v1/admin/apps",
                {"tenant_id": tenant["id"], "code": "e2e-app", "name": "E2E"},
                admin,
            )
            credential = client.post(
                f"/api/v1/admin/apps/{app['id']}/credentials", headers=admin
            )
            assert credential.status_code == 201
            owner = {"Authorization": f"Bearer {credential.json()['data']['api_key']}"}
            kb = post("/api/v1/kbs", {"code": "e2e-kb", "name": "E2E"}, owner)
            document = post(
                f"/api/v1/kbs/{kb['id']}/documents",
                {"title": "test law", "text": "民间借贷涉及高利放贷，借款金额100万元。"},
                owner,
            )
            assert document["chunk_count"] == 1
            job_id = document["index_job_id"]
            for _ in range(900 if native else 200):
                job = client.get(f"/api/v1/jobs/{job_id}", headers=owner).json()["data"]
                if job["status"] in {"SUCCEEDED", "FAILED"}:
                    break
                time.sleep(0.1)
            assert job["status"] == "SUCCEEDED", job
            response = client.post(
                "/v1/retrieve",
                json={"kb_ids": [kb["id"]], "query": "民间借贷高利放贷", "with_graph": native},
                headers=owner,
            )
            assert response.status_code == 200, response.text
            assert response.json()["results"]
            citation = response.json()["results"][0]["citation"]
            assert citation["source_status"] == "SNAPSHOT_AVAILABLE"
            source = client.get(citation["source_url"], headers=owner)
            assert source.status_code == 200, source.text
            assert source.json()["data"]["snapshot"]["document_version_id"] is not None
            if native:
                assert response.json()["usage"]["recall_mode"] == (
                    "milvus-hybrid" if milvus else "sql-hybrid"
                )
                graph = response.json()["graph"]
                assert graph["backend"] == "neo4j", graph
                assert graph["entities"], graph
                assert graph["paths"], graph

            first_index = job["result"]["index"]["version"]
            kb_version = response.json()["versions"][str(kb["id"])]["kb"]
            response = client.post(
                f"/api/v1/kbs/{kb['id']}/aliases",
                json={"alias": "prod", "kb_version": kb_version, "index_version": first_index},
                headers=owner,
            )
            assert response.status_code == 200, response.text
            second = post(
                f"/api/v1/kbs/{kb['id']}/documents",
                {"title": "second law", "text": "合同违约赔偿责任与证据。"},
                owner,
            )
            for _ in range(900 if native else 200):
                job = client.get(
                    f"/api/v1/jobs/{second['index_job_id']}", headers=owner
                ).json()["data"]
                if job["status"] in {"SUCCEEDED", "FAILED"}:
                    break
                time.sleep(0.1)
            assert job["status"] == "SUCCEEDED", job
            second_index = job["result"]["index"]["version"]
            response = client.get(f"/api/v1/kbs/{kb['id']}/versions", headers=owner)
            assert response.status_code == 200, response.text
            versions = response.json()["data"]["kb_versions"]
            second_version = next(
                item["version"] for item in versions
                if item["id"] == job["result"]["index"]["kb_version_id"]
            )
            for selected_index, expected in (
                (None, {document["document"]["id"]}),
                (second_index, {document["document"]["id"], second["document"]["id"]}),
            ):
                payload = {"kb_ids": [kb["id"]], "query": "合同借贷", "top_k": 10}
                if selected_index:
                    payload["index_version"] = selected_index
                    payload["kb_version"] = second_version
                response = client.post("/v1/retrieve", json=payload, headers=owner)
                assert response.status_code == 200, response.text
                assert {item["doc_id"] for item in response.json()["results"]} == expected

            other = post(
                "/api/v1/admin/apps",
                {"tenant_id": tenant["id"], "code": "other-app", "name": "Other"},
                admin,
            )
            credential = client.post(
                f"/api/v1/admin/apps/{other['id']}/credentials", headers=admin
            )
            assert credential.status_code == 201
            headers = {"Authorization": f"Bearer {credential.json()['data']['api_key']}"}
            assert client.get(f"/api/v1/jobs/{job_id}", headers=headers).status_code == 404
            response = client.post(
                "/v1/retrieve",
                json={"kb_ids": [kb["id"]], "query": "loan interest"},
                headers=headers,
            )
            assert response.status_code == 403
            response = client.post(
                f"/api/v1/kbs/{kb['id']}/grants",
                json={"subject_kind": "app", "subject_id": str(other["id"]), "level": "read"},
                headers=owner,
            )
            assert response.status_code == 201, response.text
            second_id = second["document"]["id"]
            response = client.put(
                f"/api/v1/documents/{second_id}/access-policy",
                json={"access_policy": "RESTRICTED"}, headers=owner,
            )
            assert response.status_code == 200, response.text
            assert client.get(f"/api/v1/documents/{second_id}", headers=headers).status_code == 403
            search = {
                "kb_ids": [kb["id"]], "query": "合同借贷", "top_k": 10,
                "kb_version": second_version, "index_version": second_index, "with_graph": native,
            }
            response = client.post("/v1/retrieve", json=search, headers=headers)
            assert response.status_code == 200, response.text
            assert {item["doc_id"] for item in response.json()["results"]} == {
                document["document"]["id"]
            }
            response = client.post(
                "/api/v1/contract/review",
                json={"text": "借款合同约定年利率为28%，违约金为50%。", "kb_ids": [kb["id"]]},
                headers=headers,
            )
            assert response.status_code == 200, response.text
            review = response.json()["data"]
            assert review["review_status"] == "HUMAN_REVIEW_REQUIRED"
            citations = [c for finding in review["findings"] for c in finding["citations"]]
            assert citations
            assert {c["document_id"] for c in citations} == {document["document"]["id"]}
            assert "15.4%" not in response.text
            response = client.post(
                "/api/v1/contract/report",
                json={"text": "借款合同约定年利率为28%，违约金为50%。", "kb_ids": [kb["id"]]},
                headers=headers,
            )
            assert response.status_code == 200, response.text
            report = response.json()["data"]
            assert report["review_status"] == "HUMAN_REVIEW_REQUIRED"
            assert "未经核验" in report["content"]
            assert "second law" not in report["content"]
            response = client.post(
                f"/api/v1/documents/{second_id}/grants",
                json={"subject_kind": "app", "subject_id": str(other["id"]), "level": "read"},
                headers=owner,
            )
            assert response.status_code == 201, response.text
            response = client.post("/v1/retrieve", json=search, headers=headers)
            assert response.status_code == 200, response.text
            assert {item["doc_id"] for item in response.json()["results"]} == {
                document["document"]["id"], second_id
            }
            response = client.delete(
                f"/api/v1/documents/{second_id}/grants",
                params={"subject_kind": "app", "subject_id": str(other["id"])}, headers=owner,
            )
            assert response.status_code == 200, response.text
            assert client.get(f"/api/v1/documents/{second_id}", headers=headers).status_code == 403
    finally:
        if worker:
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=10)
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def test_http_queued_index_rechecks_revoked_access(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = os.environ.copy()
    env.update(
        DATABASE_URL=f"sqlite:///{(tmp_path / 'revoke.db').as_posix()}",
        DATA_DIR=str(tmp_path),
        ADMIN_TOKEN="http-e2e-admin",
        APP_ENV="dev",
        INFERENCE_MODE="mock",
        RESOURCE_ACL_ENFORCED="true",
        JOB_MODE="worker",
        WORKER_POLL_SECONDS="0.1",
        RATE_LIMIT_ENABLED="false",
        REDIS_URL="",
        VECTOR_BACKEND="sql",
        GRAPH_ENABLED="false",
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "judicial_ai.main:app", "--host", "127.0.0.1",
         "--port", str(port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    worker = None
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
            for _ in range(100):
                if process.poll() is not None:
                    raise AssertionError("HTTP server exited before becoming ready")
                try:
                    if client.get("/readyz").status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                time.sleep(0.1)
            else:
                raise AssertionError("HTTP server did not become ready")
            admin = {"Authorization": "Bearer http-e2e-admin"}

            def post(path, payload, headers):
                response = client.post(path, json=payload, headers=headers)
                assert response.status_code == 201, response.text
                return response.json()["data"]

            tenant = post("/api/v1/admin/tenants", {"code": "revoke", "name": "test"}, admin)
            actors = []
            for code in ("owner", "writer"):
                app = post(
                    "/api/v1/admin/apps",
                    {"tenant_id": tenant["id"], "code": code, "name": code},
                    admin,
                )
                response = client.post(
                    f"/api/v1/admin/apps/{app['id']}/credentials", headers=admin
                )
                assert response.status_code == 201
                actors.append((app["id"], {
                    "Authorization": f"Bearer {response.json()['data']['api_key']}"
                }))
            owner = actors[0][1]
            writer_id, writer = actors[1]
            kb = post("/api/v1/kbs", {"code": "revoke-kb", "name": "test"}, owner)
            post(
                f"/api/v1/kbs/{kb['id']}/grants",
                {"subject_kind": "app", "subject_id": str(writer_id), "level": "write"},
                owner,
            )
            response = client.post(f"/api/v1/kbs/{kb['id']}/index-jobs", headers=writer)
            assert response.status_code == 202, response.text
            job_id = response.json()["data"]["id"]
            response = client.delete(
                f"/api/v1/kbs/{kb['id']}/grants",
                params={"subject_kind": "app", "subject_id": str(writer_id)},
                headers=owner,
            )
            assert response.status_code == 200, response.text
            worker = subprocess.Popen(
                [sys.executable, "-m", "judicial_ai.worker"],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            for _ in range(200):
                response = client.get(f"/api/v1/jobs/{job_id}", headers=admin)
                assert response.status_code == 200, response.text
                job = response.json()["data"]
                if job["status"] in {"SUCCEEDED", "FAILED"}:
                    break
                time.sleep(0.1)
            assert job["status"] == "FAILED", job
            assert job["attempts"] == 1
            assert not job["result"]
            versions = client.get(f"/api/v1/kbs/{kb['id']}/versions", headers=owner)
            assert versions.status_code == 200
            assert not versions.json()["data"]["index_versions"]
    finally:
        if worker:
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=10)
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


@pytest.mark.skipif(
    not os.getenv("JAI_TEST_INFERENCE_BASE_URL"), reason="real inference endpoint not configured"
)
def test_http_real_inference_proxy(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = os.environ.copy()
    env.update(
        DATABASE_URL=f"sqlite:///{(tmp_path / 'proxy.db').as_posix()}",
        DATA_DIR=str(tmp_path),
        ADMIN_TOKEN="http-e2e-admin",
        APP_ENV="dev",
        INFERENCE_MODE="proxy",
        INFERENCE_BASE_URL=os.environ["JAI_TEST_INFERENCE_BASE_URL"],
        RESOURCE_ACL_ENFORCED="true",
        RATE_LIMIT_ENABLED="false",
        REDIS_URL="",
        VECTOR_BACKEND="sql",
        GRAPH_ENABLED="false",
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "judicial_ai.main:app", "--host", "127.0.0.1",
         "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=90) as client:
            for _ in range(150):
                if process.poll() is not None:
                    raise AssertionError("HTTP server exited before becoming ready")
                try:
                    if client.get("/readyz").status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                time.sleep(0.1)
            else:
                raise AssertionError("HTTP server did not become ready")
            admin = {"Authorization": "Bearer http-e2e-admin"}

            def post(path, body):
                response = client.post(path, json=body, headers=admin)
                assert response.status_code in {200, 201}, response.text
                return response.json()["data"]

            tenant = post("/api/v1/admin/tenants", {"code": "proxy-test", "name": "test"})
            app = post(
                "/api/v1/admin/apps",
                {"tenant_id": tenant["id"], "code": "proxy-app", "name": "test"},
            )
            credential = client.post(
                f"/api/v1/admin/apps/{app['id']}/credentials", headers=admin
            )
            assert credential.status_code == 201
            headers = {"Authorization": f"Bearer {credential.json()['data']['api_key']}"}
            code = "llm.real-test"
            post(
                "/api/v1/admin/capabilities",
                {
                    "code": code, "name": "test", "type": "MODEL_CHAT", "owner": "test",
                    "version": "1.0.0", "protocol": "OPENAI", "model_name": "stories260k",
                    "request_schema": {"type": "object"},
                    "response_schema": {"type": "object"},
                },
            )
            assert client.post(
                f"/api/v1/admin/capabilities/{code}/versions/1.0.0/publish", headers=admin
            ).status_code == 200
            post(f"/api/v1/admin/capabilities/{code}/grants", {"app_id": app["id"]})
            payload = {
                "model": code, "messages": [{"role": "user", "content": "Once upon a time"}],
                "max_tokens": 8,
            }
            response = client.post("/v1/chat/completions", json=payload, headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()["choices"][0]["message"]["role"] == "assistant"
            with client.stream(
                "POST", "/v1/chat/completions", json={**payload, "stream": True}, headers=headers
            ) as response:
                assert response.status_code == 200
                assert "data: [DONE]" in "".join(response.iter_text())
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
