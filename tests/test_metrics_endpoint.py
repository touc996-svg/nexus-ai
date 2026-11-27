"""Admin-only /system/metrics, health behaviour, and end-to-end redaction through the real middleware chain."""

import io
import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import health, observability
from app.core import logging_config as lc
from app.core.metrics import metrics
from app.core.request_observability import install_observability
from app.security.hardening import install_security

OPERATOR = "operator-key-0123456789-abcdef"
ALICE = "alice-key-0123456789-abcdefgh"
ENV = ("NEXUS_DEMO_MODE", "APP_ENV", "NEXUS_API_KEY", "NEXUS_API_KEYS", "NEXUS_REQUIRE_API_KEY",
       "NEXUS_RATE_LIMIT_PER_MINUTE", "NEXUS_TRUSTED_PROXY_HOPS", "NEXUS_KEYSTORE_ENABLED")


@pytest.fixture
def stack(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NEXUS_API_KEY", OPERATOR)
    monkeypatch.setenv("NEXUS_API_KEYS", f"{ALICE}=alice")
    api = FastAPI()
    api.include_router(health.router)
    api.include_router(observability.router)

    @api.get("/profile/{user_id}")
    def profile(user_id: str):
        return {"user": user_id}

    @api.post("/boom")
    def boom():
        raise ValueError("PROMPT-CANARY-TEXT and hunter2-password")

    install_security(api)
    install_observability(api)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(lc.JsonFormatter())
    logger = logging.getLogger("nexus")
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    metrics.reset()
    yield TestClient(api, raise_server_exceptions=False), stream
    logger.removeHandler(handler)
    logger.setLevel(previous)


def auth(key):
    return {"Authorization": f"Bearer {key}"}


def test_metrics_endpoint_is_admin_only(stack):
    client, _ = stack
    assert client.get("/system/metrics").status_code == 401
    assert client.get("/system/metrics", headers=auth(ALICE)).status_code == 403
    response = client.get("/system/metrics", headers=auth(OPERATOR))
    assert response.status_code == 200 and "counters" in response.json()


def test_requests_are_counted_by_route_template_including_rejections(stack):
    client, _ = stack
    for user in ("alice", "bob", "carol"):
        client.get(f"/profile/{user}", headers=auth(ALICE) if user == "alice" else auth(OPERATOR))
    client.get("/profile/alice")  # 401
    counters = client.get("/system/metrics", headers=auth(OPERATOR)).json()["counters"]
    by = {(c["labels"].get("route"), c["labels"].get("status_class")): c["value"] for c in counters if c["name"] == "http_requests_total"}
    assert by[("/profile/{user_id}", "2xx")] == 3
    assert by[("unmatched", "4xx")] == 1  # rejected before routing, so there is no route template to record
    assert not any("alice" in (route or "") for route, _ in by)  # concrete ids never become labels


def test_concrete_paths_with_user_ids_are_not_logged(stack):
    client, stream = stack
    client.get("/profile/alice", headers=auth(ALICE))
    assert "/profile/alice" not in stream.getvalue()


def test_a_failing_request_is_a_500_metric_and_leaks_nothing(stack):
    client, stream = stack
    response = client.post("/boom?token=hunter2-password", headers={**auth(OPERATOR), "X-Custom": "PROMPT-CANARY-TEXT"},
                           json={"message": "PROMPT-CANARY-TEXT sk-ant-abcdefghij1234"})
    assert response.status_code == 500
    snapshot = json.dumps(metrics.snapshot())
    logs = stream.getvalue()
    for canary in ("PROMPT-CANARY-TEXT", "hunter2-password", "sk-ant-abcdefghij1234", OPERATOR, ALICE):
        assert canary not in logs and canary not in snapshot
    entries = [json.loads(line) for line in logs.splitlines()]
    failed = [e for e in entries if e["event"] == "http_request_failed"]
    assert failed and failed[0]["error_type"] == "ValueError" and failed[0]["route"] == "/boom"
    assert any(c["name"] == "http_errors_total" for c in metrics.snapshot()["counters"])


def test_log_lines_for_one_request_share_the_request_id_and_header(stack):
    client, stream = stack
    response = client.get("/profile/alice", headers=auth(ALICE))
    ids = {json.loads(line)["request_id"] for line in stream.getvalue().splitlines()}
    assert response.headers["X-Request-ID"] in ids and None not in ids


def test_concurrent_requests_are_all_counted(stack):
    from concurrent.futures import ThreadPoolExecutor

    client, _ = stack
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda _: client.get("/profile/alice", headers=auth(ALICE)).status_code, range(40)))
    counters = metrics.snapshot()["counters"]
    total = sum(c["value"] for c in counters if c["name"] == "http_requests_total" and c["labels"]["route"] == "/profile/{user_id}")
    assert total == 40


def test_health_endpoints_behave_as_before_and_are_quiet_in_logs(stack, monkeypatch):
    client, stream = stack
    monkeypatch.setenv("NEXUS_DEMO_MODE", "true")
    live = client.get("/health/live")
    assert live.status_code == 200 and live.json() == {"status": "alive"}
    ready = client.get("/health/ready")
    assert ready.status_code == 200 and ready.json()["mode"] == "demo"
    assert all(json.loads(line)["level"] == "DEBUG" for line in stream.getvalue().splitlines() if '"route": "/health/' in line)
    assert "X-Request-ID" in live.headers
