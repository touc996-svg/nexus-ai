"""Per-identity rate limiting, through the real middleware.

The per-address limiter runs before authentication (it protects the authentication step). The identity limiter
runs after it and gives each credential owner one budget however many addresses they use. Both are in process:
neither is shared across workers or instances.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.security.hardening import install_security, load_security_settings

OPERATOR_KEY = "operator-key-0123456789-abcdef"
ALICE_KEY = "alice-key-0123456789-abcdefgh"
BOB_KEY = "bob-key-0123456789-abcdefghij"
ENV_NAMES = ("NEXUS_DEMO_MODE", "APP_ENV", "NEXUS_API_KEY", "NEXUS_API_KEYS", "NEXUS_REQUIRE_API_KEY",
             "NEXUS_KEYSTORE_ENABLED", "NEXUS_RATE_LIMIT_PER_MINUTE", "NEXUS_RATE_LIMIT_PER_IDENTITY_PER_MINUTE")


def _app(monkeypatch, *, per_address, per_identity, keys=True):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NEXUS_RATE_LIMIT_PER_MINUTE", str(per_address))
    if per_identity is not None:
        monkeypatch.setenv("NEXUS_RATE_LIMIT_PER_IDENTITY_PER_MINUTE", str(per_identity))
    if keys:
        monkeypatch.setenv("NEXUS_API_KEY", OPERATOR_KEY)
        monkeypatch.setenv("NEXUS_API_KEYS", f"{ALICE_KEY}=alice,{BOB_KEY}=bob")
    api = FastAPI()

    @api.get("/ping")
    def ping():
        return {"ok": True}

    @api.get("/health/live")
    def live():
        return {"status": "alive"}

    install_security(api)
    return api


def _client(api, host):
    return TestClient(api, client=(host, 50000))


def _get(client, key=None):
    return client.get("/ping", headers={"Authorization": f"Bearer {key}"} if key else {})


def test_one_credential_shares_a_budget_across_addresses(monkeypatch):
    api = _app(monkeypatch, per_address=1000, per_identity=3)
    first, second = _client(api, "10.0.0.1"), _client(api, "10.0.0.2")
    statuses = [_get(client, ALICE_KEY).status_code for client in (first, second, first, second)]
    assert statuses == [200, 200, 200, 429]  # each address is far below its own limit; the credential is not
    blocked = _get(first, ALICE_KEY)
    assert blocked.status_code == 429 and int(blocked.headers["Retry-After"]) >= 1
    assert blocked.json() == {"detail": "Rate limit exceeded for this credential. Retry shortly."}
    assert blocked.headers["X-Content-Type-Options"] == "nosniff"  # the refusal still carries the security headers


def test_identities_have_separate_budgets(monkeypatch):
    api = _app(monkeypatch, per_address=1000, per_identity=2)
    client = _client(api, "10.0.0.1")
    assert [_get(client, ALICE_KEY).status_code for _ in range(3)] == [200, 200, 429]
    assert [_get(client, BOB_KEY).status_code for _ in range(3)] == [200, 200, 429]
    assert [_get(client, OPERATOR_KEY).status_code for _ in range(3)] == [200, 200, 429]


def test_unauthenticated_requests_do_not_spend_anyones_identity_budget(monkeypatch):
    api = _app(monkeypatch, per_address=1000, per_identity=2)
    client = _client(api, "10.0.0.1")
    for _ in range(10):
        assert _get(client, "a-wrong-key-0123456789-abcdefgh").status_code == 401
        assert _get(client).status_code == 401
    assert [_get(client, ALICE_KEY).status_code for _ in range(3)] == [200, 200, 429]


def test_the_per_address_limit_still_applies_first(monkeypatch):
    api = _app(monkeypatch, per_address=2, per_identity=1000)
    client = _client(api, "10.0.0.1")
    assert [_get(client, ALICE_KEY).status_code for _ in range(3)] == [200, 200, 429]
    assert _get(_client(api, "10.0.0.9"), ALICE_KEY).status_code == 200  # another address, same credential: fine


def test_public_paths_spend_no_identity_budget(monkeypatch):
    api = _app(monkeypatch, per_address=1000, per_identity=1)
    client = _client(api, "10.0.0.1")
    assert all(client.get("/health/live").status_code == 200 for _ in range(5))
    assert _get(client, ALICE_KEY).status_code == 200


def test_with_authentication_off_there_is_no_identity_to_limit(monkeypatch):
    api = _app(monkeypatch, per_address=1000, per_identity=1, keys=False)
    client = _client(api, "10.0.0.1")
    assert all(_get(client).status_code == 200 for _ in range(5))


def test_the_identity_limit_defaults_to_the_per_address_limit_and_must_be_positive(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NEXUS_RATE_LIMIT_PER_MINUTE", "37")
    assert load_security_settings().identity_rate_limit_per_minute == 37
    monkeypatch.setenv("NEXUS_RATE_LIMIT_PER_IDENTITY_PER_MINUTE", "5")
    assert load_security_settings().identity_rate_limit_per_minute == 5
    monkeypatch.setenv("NEXUS_RATE_LIMIT_PER_IDENTITY_PER_MINUTE", "0")
    with pytest.raises(ValueError, match="NEXUS_RATE_LIMIT_PER_IDENTITY_PER_MINUTE must be at least 1"):
        load_security_settings()
