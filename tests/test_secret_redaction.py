"""Secrets never leave the process through repr(), str(), logs or HTTP responses.

Covers the operator key, per-user keys, the JWT signing secret, a stored key and its digest. Each scenario does
something that commonly leaks (a crash inside a handler, a failing store, a logged settings object) and then
searches every byte that was produced.
"""
import logging
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.security.hardening import (
    SecuritySettings,
    install_security,
    load_security_settings,
)
from app.security.jwt_auth import JwtSettings
from app.security.keystore import KeyStore, hash_key

OPERATOR_KEY = "operator-key-0123456789-abcdef-SECRET"
USER_KEY = "alice-key-0123456789-abcdefgh-SECRET"
JWT_SECRET = "jwt-secret-0123456789-abcdefghijklmnopqrstuv"
ENV_KEYS = ("NEXUS_DEMO_MODE", "APP_ENV", "NEXUS_API_KEY", "NEXUS_API_KEYS", "NEXUS_REQUIRE_API_KEY",
            "NEXUS_KEYSTORE_ENABLED", "NEXUS_JWT_SECRETS", "NEXUS_JWT_ISSUER", "NEXUS_JWT_AUDIENCE")


def _configure(monkeypatch, **env):
    for name in ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NEXUS_API_KEY", OPERATOR_KEY)
    monkeypatch.setenv("NEXUS_API_KEYS", f"{USER_KEY}=alice")
    monkeypatch.setenv("NEXUS_JWT_SECRETS", JWT_SECRET)
    monkeypatch.setenv("NEXUS_JWT_ISSUER", "https://issuer.test")
    monkeypatch.setenv("NEXUS_JWT_AUDIENCE", "nexus")
    for name, value in env.items():
        monkeypatch.setenv(name, value)


SECRETS = (OPERATOR_KEY, USER_KEY, JWT_SECRET)


def _assert_no_secret(text, where):
    for secret in SECRETS:
        assert secret not in text, f"{where} contains a secret"


def test_settings_repr_and_str_hide_every_key(monkeypatch):
    _configure(monkeypatch)
    settings = load_security_settings()
    for text in (repr(settings), str(settings), f"{settings}", f"{settings!r}", str([settings]), str({"s": settings})):
        _assert_no_secret(text, "settings rendering")
    assert "api_key=<set>" in repr(settings) and "user_keys=<1 hidden>" in repr(settings)
    assert "secrets=<1 hidden>" in repr(settings)  # the nested JwtSettings is hidden too


def test_settings_repr_still_shows_the_non_secret_configuration(monkeypatch):
    _configure(monkeypatch, NEXUS_RATE_LIMIT_PER_MINUTE="7")
    text = repr(load_security_settings())
    assert "rate_limit_per_minute=7" in text and "require_api_key=True" in text and "app_env='development'" in text
    empty = SecuritySettings(demo_mode=True, app_env="test", api_key="", user_keys=(), require_api_key=False,
                             cors_origins=(), rate_limit_per_minute=1, max_request_bytes=1024)
    assert "api_key=<unset>" in repr(empty) and "user_keys=<0 hidden>" in repr(empty)


def test_the_jwt_settings_repr_hides_the_secret():
    settings = JwtSettings(secrets=(JWT_SECRET.encode(),), issuer="i", audience="a")
    _assert_no_secret(repr(settings) + str(settings), "jwt settings")


def test_a_logged_settings_object_leaks_nothing(monkeypatch, caplog):
    _configure(monkeypatch)
    settings = load_security_settings()
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("nexus.security").info("settings loaded: %s", settings)
        logging.getLogger("nexus.security").info("settings loaded: %r", settings)
    _assert_no_secret(caplog.text, "log output")


def _app_with_crash(monkeypatch, key_store=None):
    _configure(monkeypatch)
    api = FastAPI()

    @api.get("/crash")
    def crash():
        raise RuntimeError(f"unexpected: {api.state.security_settings}")  # a careless message that embeds settings

    @api.get("/ok")
    def ok():
        return {"ok": True}

    install_security(api, key_store=key_store)
    return TestClient(api, raise_server_exceptions=False)


def test_a_handler_crash_that_embeds_settings_leaks_nothing_in_the_response_or_log(monkeypatch, caplog):
    client = _app_with_crash(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        response = client.get("/crash", headers={"Authorization": f"Bearer {OPERATOR_KEY}"})
        unauthorized = client.get("/ok", headers={"Authorization": "Bearer not-the-key-0123456789-abcd"})
        missing = client.get("/ok")
    assert response.status_code == 500
    for text, where in ((response.text, "500 response"), (unauthorized.text, "401 response"),
                        (missing.text, "401 response"), (caplog.text, "log")):
        _assert_no_secret(text, where)
    assert "request_failed" in caplog.text  # the failure is logged, without the message that embedded settings


def test_a_failing_key_store_leaks_no_stored_key_or_digest(monkeypatch, caplog):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    store = KeyStore(session_factory=sessionmaker(bind=engine), clock=lambda: datetime.now(UTC),
                     initialize=False)
    issued = store.issue("alice")

    def broken():
        raise RuntimeError(f"db down while checking {issued.key}")  # a careless message with the key in it

    broken_store = KeyStore(session_factory=broken, initialize=False)
    client = _app_with_crash(monkeypatch, key_store=broken_store)
    with caplog.at_level(logging.DEBUG):
        response = client.get("/ok", headers={"Authorization": f"Bearer {issued.key}"})
    assert response.status_code == 503
    for text in (response.text, caplog.text, repr(issued), str(store.list_keys())):
        assert issued.key not in text and hash_key(issued.key) not in text


@pytest.mark.parametrize("field", ["api_key", "user_keys"])
def test_the_raw_fields_are_still_available_to_the_code_that_needs_them(monkeypatch, field):
    # Redaction is in the rendering only; authentication still compares against the real values.
    _configure(monkeypatch)
    settings = load_security_settings()
    assert getattr(settings, field)
    client = _app_with_crash(monkeypatch)
    assert client.get("/ok", headers={"Authorization": f"Bearer {OPERATOR_KEY}"}).status_code == 200
    assert client.get("/ok", headers={"X-API-Key": USER_KEY}).status_code == 200
