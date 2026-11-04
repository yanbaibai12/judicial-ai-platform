from __future__ import annotations

import uuid

from judicial_ai.config import get_settings
from judicial_ai.db import SessionLocal
from judicial_ai.models import Job

DOC_TEXT = (
    "第六百八十条 禁止高利放贷，借款的利率不得违反国家有关规定。\n\n"
    "借款合同对支付利息没有约定的，视为没有利息。"
)


def _create_kb(client, headers) -> int:
    response = client.post(
        "/api/v1/kbs",
        json={
            "code": f"kb-{uuid.uuid4().hex[:8]}",
            "name": "自动索引测试库",
            "domain": "legal",
            "embedding_model": "bge-m3",
            "chunk_config": {"max_chars": 240, "overlap": 30},
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


def _add_document(client, headers, kb_id: int, title: str = "借款条款") -> dict:
    response = client.post(
        f"/api/v1/kbs/{kb_id}/documents",
        json={"title": title, "doc_type": "law", "text": DOC_TEXT},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_upload_auto_enqueues_index_job(client, new_tenant_app):
    headers = new_tenant_app["headers"]
    kb_id = _create_kb(client, headers)

    data = _add_document(client, headers, kb_id)
    job_id = data["index_job_id"]
    assert job_id

    job = SessionLocal().get(Job, job_id)
    assert job is not None
    assert job.job_type == "kb_index"
    assert job.payload["kb_id"] == kb_id
    assert job.payload["trigger"] == "auto_upload"

    # 重复上传同一内容 → 幂等，返回同一个 job
    duplicate = _add_document(client, headers, kb_id, title="借款条款副本")
    assert duplicate["duplicate"] is True
    assert duplicate["index_job_id"] == job_id


def test_auto_index_can_be_disabled(client, new_tenant_app, monkeypatch):
    settings = get_settings()
    previous = settings.kb_auto_index_after_upload
    settings.kb_auto_index_after_upload = False
    try:
        headers = new_tenant_app["headers"]
        kb_id = _create_kb(client, headers)
        data = _add_document(client, headers, kb_id)
        assert data["index_job_id"] is None
    finally:
        settings.kb_auto_index_after_upload = previous
