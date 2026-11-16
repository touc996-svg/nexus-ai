import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.security.hardening import install_security
from app.api.health import router as health_router


def build_app(monkeypatch, **environment):
    for key in (
        "NEXUS_DEMO_MODE", "APP_ENV", "NEXUS_API_KEY",
        "NEXUS_REQUIRE_API_KEY", "NEXUS_RATE_LIMIT_PER_MINUTE",
        "NEXUS_MAX_REQUEST_BYTES", "NEXUS_CORS_ORIGINS",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    api = FastAPI()

    @api.get("/private")
    def private():
        return {"status": "ok"}

    @api.get("/activity")
    def activity():
        return {"activities": ["private activity"]}

    api.include_router(health_router)
    install_security(api)
    return TestClient(api)


def test_production_requires_api_key(monkeypatch):
    with pytest.raises(RuntimeError, match="NEXUS_API_KEY or NEXUS_API_KEYS is required"):
        build_app(monkeypatch, APP_ENV="production")


def test_api_key_accepts_bearer_and_x_api_key(monkeypatch):
    client = build_app(
        monkeypatch,
        APP_ENV="production",
        NEXUS_API_KEY="a-test-secret-that-is-not-used-outside-this-test",
    )
    assert client.get("/private").status_code == 401
    assert client.get(
        "/private", headers={"Authorization": "Bearer a-test-secret-that-is-not-used-outside-this-test"}
    ).status_code == 200
    assert client.get(
        "/private", headers={"X-API-Key": "a-test-secret-that-is-not-used-outside-this-test"}
    ).status_code == 200


def test_demo_mode_blocks_shared_activity_endpoint(monkeypatch):
    client = build_app(monkeypatch, NEXUS_DEMO_MODE="true")
    response = client.get("/activity")
    assert response.status_code == 403
    assert "not exposed" in response.json()["detail"]


def test_rate_limit_and_security_headers(monkeypatch):
    client = build_app(
        monkeypatch,
        NEXUS_RATE_LIMIT_PER_MINUTE="1",
    )
    first = client.get("/private")
    second = client.get("/private")
    assert first.status_code == 200
    assert first.headers.get("X-Request-ID")
    assert first.headers["X-Content-Type-Options"] == "nosniff"
    assert second.status_code == 429
    assert second.headers.get("Retry-After")


def test_oversized_content_length_is_rejected(monkeypatch):
    client = build_app(
        monkeypatch,
        NEXUS_MAX_REQUEST_BYTES="1024",
    )
    response = client.post(
        "/private",
        content=b"x" * 2048,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 413


def test_health_endpoints_are_exempt_from_api_key_auth(monkeypatch):
    client = build_app(
        monkeypatch,
        APP_ENV="production",
        NEXUS_API_KEY="a-test-secret-that-is-not-used-outside-this-test",
        NEXUS_DEMO_MODE="true",
    )
    assert client.get("/health/live").json() == {"status": "alive"}
    readiness = client.get("/health/ready")
    assert readiness.status_code == 200
    assert readiness.json()["mode"] == "demo"
