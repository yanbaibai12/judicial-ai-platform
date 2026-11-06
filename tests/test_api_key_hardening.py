from __future__ import annotations

import hashlib
import os
import tempfile

_TEST_DIR = tempfile.mkdtemp(prefix="judicial-ai-hardening-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TEST_DIR}/hardening.db")
os.environ.setdefault("DATA_DIR", _TEST_DIR)
os.environ.setdefault("OBJECT_STORE_PATH", f"{_TEST_DIR}/objects")
os.environ.setdefault("ADMIN_TOKEN", "test-admin")
os.environ.setdefault("INFERENCE_MODE", "mock")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("GRAPH_ENABLED", "false")
os.environ.setdefault("EMBEDDING_DIM", "128")

import pytest

from judicial_ai.config import get_settings
from judicial_ai.db import SessionLocal, init_db
from judicial_ai.errors import sanitize_exception_summary
from judicial_ai.ids import new_id
from judicial_ai.models import App, AppCredential, Tenant
from judicial_ai.security import (
    HASH_ALGO_HMAC,
    HASH_ALGO_LEGACY_SHA256,
    _app_principal,
    generate_api_key,
    hash_token,
    sha256_token,
    split_stored_digest,
    token_digest_candidates,
)

PRIMARY_SECRET = "primary-secret-abcdefghijklmnopqrstuvwxyz"
LEGACY_SECRET = "legacy-secret-abcdefghijklmnopqrstuvwxyz"


@pytest.fixture(autouse=True)
def _isolated_db():
    init_db()
    yield
    session = SessionLocal()
    try:
        session.query(AppCredential).delete()
        session.query(App).delete()
        session.query(Tenant).delete()
        session.commit()
    finally:
        session.close()


def _make_app_credential(plaintext: str, key_hash: str, hash_algo: str | None) -> int:
    session = SessionLocal()
    try:
        tenant = Tenant(id=new_id(), code="t-hardening", name="局", status="ACTIVE")
        session.add(tenant)
        session.flush()
        app = App(
            id=new_id(),
            tenant_id=tenant.id,
            code="app-hardening",
            name="局",
            permissions=["app:invoke"],
        )
        session.add(app)
        session.flush()
        credential = AppCredential(
            id=new_id(),
            app_id=app.id,
            credential_type="API_KEY",
            key_hash=key_hash,
            key_prefix=plaintext[:10],
            hash_algo=hash_algo,
            status="ACTIVE",
        )
        session.add(credential)
        session.commit()
        return credential.id
    finally:
        session.close()


def test_hash_token_uses_hmac_prefix():
    plaintext, digest, prefix = generate_api_key()
    assert plaintext.startswith("ak_")
    assert digest.startswith(f"{HASH_ALGO_HMAC}$")
    assert prefix == plaintext[:10]
    assert digest != hash_token(plaintext + "x")


def test_split_stored_digest_handles_both_shapes():
    plaintext = "ak_abc"
    algo, digest = split_stored_digest(hash_token(plaintext))
    assert algo == HASH_ALGO_HMAC
    assert len(digest) == 64

    legacy = hashlib.sha256(plaintext.encode()).hexdigest()
    algo, digest = split_stored_digest(legacy)
    assert algo == HASH_ALGO_LEGACY_SHA256
    assert digest == legacy


def test_candidates_cover_primary_then_legacy_sha256():
    settings = get_settings()
    original_secret = settings.api_key_hash_secret
    original_legacy = settings.api_key_hash_legacy_secrets
    try:
        settings.api_key_hash_secret = PRIMARY_SECRET
        settings.api_key_hash_legacy_secrets = ""
        candidates = token_digest_candidates("ak_test")
        assert candidates[0][0] == HASH_ALGO_HMAC
        assert candidates[-1] == (
            HASH_ALGO_LEGACY_SHA256,
            hashlib.sha256(b"ak_test").hexdigest(),
        )
    finally:
        settings.api_key_hash_secret = original_secret
        settings.api_key_hash_legacy_secrets = original_legacy


def test_candidates_include_legacy_secret_digest():
    settings = get_settings()
    original_secret = settings.api_key_hash_secret
    original_legacy = settings.api_key_hash_legacy_secrets
    try:
        settings.api_key_hash_secret = PRIMARY_SECRET
        settings.api_key_hash_legacy_secrets = LEGACY_SECRET
        token = "ak_plaintext_token_value"
        digests = [digest for _algo, digest in token_digest_candidates(token)]
        import hmac

        legacy_digest = hmac.new(
            LEGACY_SECRET.encode(), token.encode(), hashlib.sha256
        ).hexdigest()
        assert legacy_digest in digests
        assert sha256_token(token) in digests
        assert len(digests) == 3
    finally:
        settings.api_key_hash_secret = original_secret
        settings.api_key_hash_legacy_secrets = original_legacy


def test_legacy_sha256_credential_allows_auth_and_upgrades():
    token = "ak_legacy_plain_token_value_for_test"
    credential_id = _make_app_credential(token, sha256_token(token), HASH_ALGO_LEGACY_SHA256)

    session = SessionLocal()
    try:
        principal = _app_principal(session, token)
        assert principal is not None
        assert principal.kind == "app"
    finally:
        session.close()

    session = SessionLocal()
    try:
        reloaded = session.get(AppCredential, credential_id)
        assert reloaded.key_hash.startswith(f"{HASH_ALGO_HMAC}$")
        assert reloaded.hash_algo == HASH_ALGO_HMAC
    finally:
        session.close()


def test_unknown_token_yields_no_principal():
    session = SessionLocal()
    try:
        assert _app_principal(session, "ak_not_registered_anywhere_token") is None
    finally:
        session.close()


def test_revoked_credential_not_matched():
    token = "ak_revoked_plain_token_value_for_test"
    credential_id = _make_app_credential(token, hash_token(token), HASH_ALGO_HMAC)
    session = SessionLocal()
    try:
        credential = session.get(AppCredential, credential_id)
        credential.status = "REVOKED"
        session.commit()
    finally:
        session.close()

    session = SessionLocal()
    try:
        assert _app_principal(session, token) is None
    finally:
        session.close()


def test_sanitize_exception_summary_keeps_type_only():
    sensitive_message = (
        "connect failed for postgres://admin:p@ssw0rd@10.0.0.8:5432/judicial "
        "kernel_id=42 user_id=ac-918273 revision=backoff"
    )

    class OperationalError(Exception):
        pass

    summary = sanitize_exception_summary(OperationalError(sensitive_message))
    assert summary == "OperationalError"
    for leaked in ("p@ssw0rd", "10.0.0.8", "5432", "postgres://", "user_id", "ac-918273"):
        assert leaked not in summary