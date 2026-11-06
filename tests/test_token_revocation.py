from __future__ import annotations

from datetime import timedelta

from judicial_ai.db import SessionLocal
from judicial_ai.models import utcnow
from judicial_ai.services.revocation import (
    delete_revocation,
    invalidate_cache,
    is_token_revoked,
    list_revocations,
    revoke,
    to_datetime,
)


def _cleanup() -> None:
    db = SessionLocal()
    try:
        for record in list_revocations(db, limit=500):
            delete_revocation(db, record.id)
    finally:
        db.close()
    invalidate_cache()


def test_jti_revocation_takes_effect_and_can_be_removed(client) -> None:
    db = SessionLocal()
    try:
        _cleanup()
        assert is_token_revoked(db, jti="jti-1") == (False, None)

        record = revoke(db, jti="jti-1", reason="疑似泄露", created_by="tester")
        assert record.kind == "jti"

        revoked, reason = is_token_revoked(db, jti="jti-1")
        assert revoked is True
        assert reason == "疑似泄露"

        delete_revocation(db, record.id)
        assert is_token_revoked(db, jti="jti-1") == (False, None)
    finally:
        db.close()
        _cleanup()


def test_subject_revocation_only_covers_tokens_issued_before(client) -> None:
    db = SessionLocal()
    try:
        _cleanup()
        cutoff = utcnow()
        revoke(db, subject="user-1", reason="离职", revoked_before=cutoff, created_by="tester")

        before = cutoff - timedelta(hours=1)
        after = cutoff + timedelta(hours=1)

        assert is_token_revoked(db, subject="user-1", issued_at=before)[0] is True
        assert is_token_revoked(db, subject="user-1", issued_at=after)[0] is False
        # 缺失 iat 时无法证明晚于撤销时间，按已撤销处理（fail-closed）
        assert is_token_revoked(db, subject="user-1", issued_at=None)[0] is True
    finally:
        db.close()
        _cleanup()


def test_expired_revocation_record_is_ignored(client) -> None:
    db = SessionLocal()
    try:
        _cleanup()
        record = revoke(db, jti="jti-expired", expires_in_seconds=60)
        record.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
        invalidate_cache()

        assert is_token_revoked(db, jti="jti-expired") == (False, None)
    finally:
        db.close()
        _cleanup()


def test_to_datetime_accepts_seconds_and_milliseconds() -> None:
    seconds = to_datetime(1_700_000_000)
    milliseconds = to_datetime(1_700_000_000_000)
    assert seconds == milliseconds
    assert seconds is not None and seconds.year == 2023
    assert to_datetime(None) is None
    assert to_datetime("not-a-number") is None


def test_admin_can_create_list_and_delete_revocation(client, admin_headers) -> None:
    _cleanup()
    response = client.post(
        "/api/v1/admin/token-revocations",
        json={"jti": "jti-api-1", "subject": None, "reason": "API 测试"},
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    created = response.json()["data"]
    assert created["kind"] == "jti"
    assert created["value"] == "jti-api-1"

    listing = client.get("/api/v1/admin/token-revocations", headers=admin_headers)
    assert listing.status_code == 200, listing.text
    assert any(item["id"] == created["id"] for item in listing.json()["data"]["items"])

    deleted = client.delete(
        f"/api/v1/admin/token-revocations/{created['id']}", headers=admin_headers
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"]["status"] == "DELETED"

    again = client.delete(
        f"/api/v1/admin/token-revocations/{created['id']}", headers=admin_headers
    )
    assert again.status_code == 404, again.text


def test_revocation_requires_exactly_one_target(client, admin_headers) -> None:
    both = client.post(
        "/api/v1/admin/token-revocations",
        json={"jti": "a", "subject": "b"},
        headers=admin_headers,
    )
    assert both.status_code == 400, both.text
    assert both.json()["error"]["code"] == "INVALID_ARGUMENT"

    neither = client.post(
        "/api/v1/admin/token-revocations",
        json={"reason": "empty"},
        headers=admin_headers,
    )
    assert neither.status_code == 400, neither.text
    assert neither.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_revocation_requires_admin(client, demo_headers) -> None:
    response = client.post(
        "/api/v1/admin/token-revocations",
        json={"jti": "jti-forbidden"},
        headers=demo_headers,
    )
    assert response.status_code == 403, response.text
