from __future__ import annotations

import uuid

import pytest

from judicial_ai.db import SessionLocal
from judicial_ai.models import CapabilityGrant


@pytest.mark.parametrize("capability_type", ["RAG_RETRIEVE", "GRAPH_RETRIEVE"])
def test_retrieval_capability_scope_fails_closed(
    client, admin_headers, new_tenant_app, monkeypatch, capability_type
):
    owner = new_tenant_app["headers"]
    kb_ids = []
    for _ in range(2):
        response = client.post(
            "/api/v1/kbs",
            json={"code": f"scope-{uuid.uuid4().hex[:8]}", "name": "scope"},
            headers=owner,
        )
        assert response.status_code == 201, response.text
        kb_ids.append(response.json()["data"]["id"])

    code = f"scope.{uuid.uuid4().hex}"
    response = client.post(
        "/api/v1/admin/capabilities",
        json={
            "code": code, "name": "scope", "type": capability_type, "owner": "test",
            "version": "1.0.0", "request_schema": {"type": "object"},
            "response_schema": {"type": "object"},
        },
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    assert client.post(
        f"/api/v1/admin/capabilities/{code}/versions/1.0.0/publish",
        headers=admin_headers,
    ).status_code == 200
    grant_url = f"/api/v1/admin/capabilities/{code}/grants"
    for scope in ({"kb_ids": None}, {"kb_ids": [True]}, {"kb_ids": ["abc"]}, {"kb_id": 1}):
        response = client.post(
            grant_url, json={"app_id": new_tenant_app["app_id"], "data_scope": scope},
            headers=admin_headers,
        )
        assert response.status_code == 400, response.text
    response = client.post(
        grant_url, json={"app_id": new_tenant_app["app_id"], "data_scope": {"kb_ids": []}},
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    grant_id = response.json()["data"]["id"]
    calls = []
    monkeypatch.setattr(
        "judicial_ai.api.gateway.retrieve",
        lambda *args, **kwargs: calls.append(kwargs) or {"results": [], "graph": {}, "usage": {}},
    )

    def invoke(ids):
        return client.post(
            f"/api/v1/capabilities/{code}/invoke",
            json={"input": {"kb_ids": ids, "query": "scope test"}},
            headers=owner,
        )

    assert invoke([kb_ids[0]]).status_code == 403
    assert not calls
    invalid_ids = (["abc"], [True], [0]) if capability_type == "GRAPH_RETRIEVE" else (["abc"], [])
    for invalid in invalid_ids:
        assert invoke(invalid).status_code == 400
    assert not calls
    for scope, ids, expected in (
        ({"kb_ids": [str(kb_ids[0])]}, [kb_ids[0]], 200),
        ({"kb_ids": [kb_ids[0]]}, kb_ids, 403),
        ({"kb_ids": None}, [kb_ids[0]], 403),
        ({"kb_id": kb_ids[0]}, [kb_ids[0]], 403),
        (None, kb_ids, 200),
    ):
        with SessionLocal() as db:
            db.get(CapabilityGrant, grant_id).data_scope = scope
            db.commit()
        before = len(calls)
        response = invoke(ids)
        assert response.status_code == expected, response.text
        assert len(calls) == before + (expected == 200)
