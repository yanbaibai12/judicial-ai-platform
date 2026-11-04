from __future__ import annotations

import pytest

from judicial_ai.acl import (
    assert_access,
    grant_resource,
    has_access,
    revoke_resource,
)
from judicial_ai.config import get_settings
from judicial_ai.db import SessionLocal, init_db
from judicial_ai.errors import ApiError
from judicial_ai.security import Principal

init_db()


def _principal(**kwargs) -> Principal:
    defaults = dict(
        kind="user",
        tenant_id=1,
        user_id="u-1",
        username="alice",
        roles=("platform-reader",),
    )
    defaults.update(kwargs)
    return Principal(**defaults)


def test_grant_level_upgrade_and_revoke():
    settings = get_settings()
    previous = settings.resource_acl_enforced
    settings.resource_acl_enforced = True
    try:
        grant = grant_resource(
            SessionLocal(), 1, "kb", 99001, "user", "u-1", level="read", granted_by="test"
        )
        assert has_access(SessionLocal(), _principal(), "kb", 99001, "read")
        assert not has_access(SessionLocal(), _principal(), "kb", 99001, "write")
        # 升级到 write
        grant_resource(SessionLocal(), 1, "kb", 99001, "user", "u-1", level="write")
        assert has_access(SessionLocal(), _principal(), "kb", 99001, "write")
        assert grant.status == "ACTIVE"
        # 未知主体被拒绝
        assert not has_access(
            SessionLocal(), _principal(user_id="u-2", username="bob"), "kb", 99001, "read"
        )
        with pytest.raises(ApiError):
            assert_access(SessionLocal(), _principal(user_id="u-2"), "kb", 99001, "read")
        assert revoke_resource(SessionLocal(), "kb", 99001, "user", "u-1")
        assert not has_access(SessionLocal(), _principal(), "kb", 99001, "read")
    finally:
        settings.resource_acl_enforced = previous


def test_acl_disabled_by_default_allows():
    settings = get_settings()
    settings.resource_acl_enforced = False
    try:
        assert has_access(None, _principal(), "kb", 12345, "write")
    finally:
        settings.resource_acl_enforced = settings.resource_acl_enforced


def test_admin_always_allowed():
    settings = get_settings()
    settings.resource_acl_enforced = True
    try:
        assert has_access(None, Principal(kind="admin"), "kb", 1, "write")
    finally:
        settings.resource_acl_enforced = False


def test_group_role_grant():
    settings = get_settings()
    previous = settings.resource_acl_enforced
    settings.resource_acl_enforced = True
    try:
        grant_resource(SessionLocal(), 1, "kb", 99002, "group", "platform-reader", "read")
        assert has_access(SessionLocal(), _principal(), "kb", 99002, "read")
    finally:
        revoke_resource(SessionLocal(), "kb", 99002, "group", "platform-reader")
        settings.resource_acl_enforced = previous
