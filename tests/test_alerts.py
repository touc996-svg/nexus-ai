"""Internal alerting: thresholds, windows, dedup, cooldown, resolution, persistence, concurrency, config, endpoint auth."""

import io
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.api import observability
from app.core import logging_config as lc
from app.core.alerts import (
    DEFAULT_RULES,
    AlertEvaluator,
    AlertEventRow,
    AlertStateRow,
    load_rules,
)
from app.core.database import Base
from app.core.metrics import Metrics
from app.core.metrics_store import MetricsStore
from app.core.monitoring import Monitoring
from app.security.hardening import install_security
from tests.monitoring_helpers import Clock, counter


@pytest.fixture
def env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'alerts.db'}", connect_args={"timeout": 30, "check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    clock = Clock()
    recorder = Metrics(clock=clock)
    store = MetricsStore(factory, clock=clock)
    mon = Monitoring(recorder=recorder, store=store, clock=clock)

    def evaluator(rules=None):
        return AlertEvaluator(store, factory, rules=rules, clock=clock, recorder=recorder)

    class Env:
        pass

    e = Env()
    e.clock, e.recorder, e.store, e.mon, e.factory, e.evaluator = clock, recorder, store, mon, factory, evaluator
    yield e
    engine.dispose()


def only(rule_id, **override):
    from dataclasses import replace

    return tuple(replace(r, **override) for r in DEFAULT_RULES if r.rule_id == rule_id)


def run(env, rules, evaluator=None):
    env.mon.flush()
    return (evaluator or env.evaluator(rules)).evaluate()


@pytest.fixture
def log_lines():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(lc.JsonFormatter())
    logger = logging.getLogger("nexus.alerts")
    logger.addHandler(handler)
    previous, logger.propagate = logger.level, logger.propagate
    logger.setLevel(logging.DEBUG)
    yield lambda: [json.loads(line) for line in stream.getvalue().splitlines()]
    logger.removeHandler(handler)
    logger.setLevel(previous)


# ---- thresholds per rule -----------------------------------------------------------------------------------------

def test_5xx_rate_fires_above_threshold_and_not_at_or_below_it(env):
    rules = only("http_5xx_rate")  # threshold 0.05, min 20 samples
    for _ in range(95):
        env.recorder.record_request("GET", "/a", 200, 5)
    for _ in range(5):
        env.recorder.record_request("GET", "/a", 500, 5)
    assert run(env, rules) == []  # exactly 5% is not above the threshold
    env.recorder.record_request("GET", "/a", 500, 5)
    result = run(env, rules)
    assert [t["event"] for t in result] == ["fired"] and result[0]["rule"] == "http_5xx_rate"


def test_too_few_samples_never_fire(env):
    rules = only("http_5xx_rate")
    for _ in range(10):
        env.recorder.record_request("GET", "/a", 500, 5)  # 100% errors but below min_samples=20
    assert run(env, rules) == []
    assert env.evaluator(rules).report()["rules"][0]["status"] == "ok"


def test_p95_latency_rule(env):
    rules = only("http_p95_latency")  # 5000 ms, min 20
    for _ in range(95):
        env.recorder.record_request("GET", "/a", 200, 40)
    for _ in range(5):
        env.recorder.record_request("GET", "/a", 200, 20000)
    assert run(env, rules) == []  # the slowest 5% do not move p95
    for _ in range(10):
        env.recorder.record_request("GET", "/a", 200, 20000)
    assert [t["event"] for t in run(env, rules)] == ["fired"]


def test_p95_past_the_last_bucket_still_fires_with_a_huge_threshold(env):
    rules = only("http_p95_latency", threshold=90000)
    for _ in range(30):
        env.recorder.record_request("GET", "/a", 200, 500_000)
    assert [t["event"] for t in run(env, rules)] == ["fired"]


def test_qdrant_unavailable_count_rule(env):
    rules = only("qdrant_unavailable")  # 3 events
    env.recorder.inc("qdrant_unavailable_total", amount=2)
    assert run(env, rules) == []
    env.recorder.inc("qdrant_unavailable_total")
    assert [t["event"] for t in run(env, rules)] == ["fired"]


def test_llm_failure_rate_and_latency_rules(env):
    rules = only("llm_failure_rate") + only("llm_p95_latency")
    for _ in range(8):
        env.recorder.record_llm("m", True, 100, 1, 1)
    for _ in range(2):
        env.recorder.record_llm("m", False, 100, error_type="TimeoutError")
    assert run(env, rules) == []  # 20% is not above 0.2
    env.recorder.record_llm("m", False, 100, error_type="TimeoutError")
    assert {t["rule"] for t in run(env, rules)} == {"llm_failure_rate"}
    for _ in range(10):
        env.recorder.record_llm("m", True, 55_000, 1, 1)
    assert "llm_p95_latency" in {t["rule"] for t in run(env, rules)}


def test_extraction_rejections_count_only_the_ingest_route(env):
    rules = only("extraction_rejections")  # 5 responses
    for _ in range(10):
        env.recorder.record_request("POST", "/chat", 429, 3)  # other routes' throttling is not an extraction problem
    assert run(env, rules) == []
    for status in (429, 429, 503, 503, 503):
        env.recorder.record_request("POST", "/knowledge/ingest", status, 3)
    assert [t["event"] for t in run(env, rules)] == ["fired"]


def test_rag_and_agent_failure_rules(env):
    rules = only("rag_failure_rate") + only("agent_failure_rate")
    for _ in range(6):
        env.recorder.record_rag("search", False, 5, error_type="RuntimeError")
    for _ in range(6):
        env.recorder.record_agent("enterprise", "failed", 5, error="ValueError")
    assert {t["rule"] for t in run(env, rules)} == {"rag_failure_rate", "agent_failure_rate"}


# ---- state machine ----------------------------------------------------------------------------------------------

def breach_qdrant(env, n=5):
    env.recorder.inc("qdrant_unavailable_total", amount=n)


def test_a_firing_alert_is_announced_once_not_on_every_evaluation(env, log_lines):
    rules = only("qdrant_unavailable")
    breach_qdrant(env)
    ev = env.evaluator(rules)
    assert len(run(env, rules, ev)) == 1
    for _ in range(5):
        env.clock.now += 30
        assert run(env, rules, ev) == []
    fired = [e for e in log_lines() if e["event"] == "alert_fired"]
    assert len(fired) == 1
    assert env.evaluator(rules).report()["rules"][0]["fire_count"] == 1


def test_the_alert_resolves_when_the_window_empties(env, log_lines):
    rules = only("qdrant_unavailable")
    breach_qdrant(env)
    ev = env.evaluator(rules)
    run(env, rules, ev)
    env.clock.now += 361  # the events leave the 300 s window (windows are whole 60 s buckets, so up to 360 s)
    assert [t["event"] for t in run(env, rules, ev)] == ["resolved"]
    state = ev.report()["rules"][0]
    assert state["status"] == "ok" and state["resolve_count"] == 1 and state["last_resolved_at"] is not None
    assert [e["event"] for e in log_lines() if e["event"].startswith("alert_")] == ["alert_fired", "alert_resolved"]


def test_cooldown_blocks_refiring_after_resolution_then_allows_it(env):
    rules = only("qdrant_unavailable", cooldown_seconds=600)
    ev = env.evaluator(rules)
    breach_qdrant(env)
    run(env, rules, ev)
    env.clock.now += 361
    run(env, rules, ev)  # resolved at +301
    breach_qdrant(env)
    env.clock.now += 60
    assert run(env, rules, ev) == []  # breached again, but inside the cooldown
    assert ev.report()["rules"][0]["cooldown_until"] is not None
    env.clock.now += 600
    breach_qdrant(env)
    assert [t["event"] for t in run(env, rules, ev)] == ["fired"]


def test_state_persists_across_evaluator_instances_and_restarts(env, log_lines):
    rules = only("qdrant_unavailable")
    breach_qdrant(env)
    run(env, rules, env.evaluator(rules))
    second = env.evaluator(rules)  # a new worker/process with no memory of the first
    assert second.report()["rules"][0]["status"] == "firing"
    assert run(env, rules, second) == []  # not announced again
    env.clock.now += 361
    assert [t["event"] for t in run(env, rules, second)] == ["resolved"]
    assert len([e for e in log_lines() if e["event"] == "alert_fired"]) == 1


def test_concurrent_evaluators_produce_exactly_one_transition(env):
    rules = only("qdrant_unavailable")
    breach_qdrant(env)
    env.mon.flush()
    evaluators = [env.evaluator(rules) for _ in range(8)]
    barrier = threading.Barrier(8)

    def go(ev):
        barrier.wait()
        return ev.evaluate()

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(go, evaluators))
    assert sum(len(r) for r in results) == 1
    session = env.factory()
    assert session.execute(select(func.count()).select_from(AlertEventRow)).scalar() == 1
    session.close()
    assert evaluators[0].report()["rules"][0]["fire_count"] == 1


def test_a_lost_race_performs_no_transition(env):
    """Compare-and-swap: when another worker already moved the state, this worker's swap changes nothing and reports None."""
    rules = only("qdrant_unavailable")
    ev = env.evaluator(rules)
    breach_qdrant(env)
    run(env, rules, ev)  # state is now 'firing'
    session = env.factory()
    try:
        stale = ev._swap(session, rules[0], "ok", "firing", env.clock.now, 9.0, {"fire_count": AlertStateRow.fire_count + 1})
        session.commit()
        assert stale is None
        assert session.execute(select(func.count()).select_from(AlertEventRow)).scalar() == 1  # no duplicate event row
    finally:
        session.close()
    assert ev.report()["rules"][0]["fire_count"] == 1


def test_alert_metrics_are_counted_and_state_is_a_gauge(env):
    rules = only("qdrant_unavailable")
    ev = env.evaluator(rules)
    breach_qdrant(env)
    run(env, rules, ev)
    assert ev.firing_gauges() == {"qdrant_unavailable": 1}
    env.clock.now += 361
    run(env, rules, ev)
    assert ev.firing_gauges() == {"qdrant_unavailable": 0}
    snap = env.recorder.snapshot()
    assert counter(snap, "alerts_fired_total", rule="qdrant_unavailable") == 1
    assert counter(snap, "alerts_resolved_total", rule="qdrant_unavailable") == 1


def test_log_events_are_structured_and_leak_nothing(env, log_lines):
    rules = only("qdrant_unavailable")
    breach_qdrant(env)
    run(env, rules)
    event = next(e for e in log_lines() if e["event"] == "alert_fired")
    assert event["rule"] == "qdrant_unavailable" and event["threshold"] == 3 and event["window_seconds"] == 300
    assert event["value"] == 5 and event["severity"] == "critical" and event["level"] == "WARNING"


def test_one_failing_rule_does_not_stop_the_others(env):
    rules = only("qdrant_unavailable") + only("rag_failure_rate")
    ev = env.evaluator(rules)
    breach_qdrant(env)
    env.mon.flush()
    real = ev.measure

    def measure(rule):
        if rule.rule_id == "qdrant_unavailable":
            raise RuntimeError("boom")
        return real(rule)

    ev.measure = measure  # type: ignore[method-assign]
    assert ev.evaluate() == []  # logged and skipped, not raised
    ev.measure = real  # type: ignore[method-assign]
    assert [t["rule"] for t in ev.evaluate()] == ["qdrant_unavailable"]


def test_disabled_rules_are_skipped(env):
    rules = only("qdrant_unavailable", enabled=False)
    breach_qdrant(env)
    assert run(env, rules) == []
    assert env.evaluator(rules).report()["rules"][0]["status"] == "disabled"


# ---- configuration -----------------------------------------------------------------------------------------------

def test_defaults_cover_the_required_signals():
    ids = {r.rule_id for r in load_rules("")}
    assert {"http_5xx_rate", "http_p95_latency", "qdrant_unavailable", "llm_failure_rate", "llm_p95_latency",
            "extraction_rejections"} <= ids


def test_rule_overrides_apply():
    rules = {r.rule_id: r for r in load_rules(json.dumps(
        {"http_5xx_rate": {"threshold": 0.5, "window_seconds": 900, "min_samples": 50, "cooldown_seconds": 10},
         "llm_p95_latency": {"enabled": False}}))}
    assert rules["http_5xx_rate"].threshold == 0.5 and rules["http_5xx_rate"].window_seconds == 900
    assert rules["llm_p95_latency"].enabled is False and rules["qdrant_unavailable"].threshold == 3


@pytest.mark.parametrize("raw", [
    "not json", "[]", '{"nope": {"threshold": 1}}', '{"http_5xx_rate": {"surprise": 1}}',
    '{"http_5xx_rate": {"threshold": 2}}', '{"http_5xx_rate": {"threshold": 0}}', '{"http_5xx_rate": {"threshold": "x"}}',
    '{"http_5xx_rate": {"threshold": true}}', '{"http_5xx_rate": {"window_seconds": 5}}',
    '{"http_5xx_rate": {"window_seconds": 999999}}', '{"http_5xx_rate": {"min_samples": -1}}',
    '{"http_5xx_rate": {"cooldown_seconds": -1}}', '{"http_5xx_rate": {"enabled": "yes"}}',
    '{"http_5xx_rate": {"threshold": NaN}}', '{"http_5xx_rate": "x"}',
])
def test_invalid_rule_configuration_is_refused(raw):
    with pytest.raises(ValueError):
        load_rules(raw)


def test_eval_interval_bounds(monkeypatch):
    from app.core.alerts import eval_interval_seconds

    assert eval_interval_seconds() == 30
    for bad in ("1", "99999", "abc"):
        monkeypatch.setenv("NEXUS_ALERT_EVAL_SECONDS", bad)
        with pytest.raises(ValueError):
            eval_interval_seconds()


# ---- endpoints: authorization and content ----------------------------------------------------------------------

OPERATOR = "operator-key-0123456789-abcdef"
ALICE = "alice-key-0123456789-abcdefgh"


@pytest.fixture
def client(env, monkeypatch):
    for name in ("NEXUS_DEMO_MODE", "APP_ENV", "NEXUS_REQUIRE_API_KEY", "NEXUS_KEYSTORE_ENABLED",
                 "NEXUS_TRUSTED_PROXY_HOPS", "NEXUS_RATE_LIMIT_PER_MINUTE", "NEXUS_ALERTS_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NEXUS_API_KEY", OPERATOR)
    monkeypatch.setenv("NEXUS_API_KEYS", f"{ALICE}=alice")
    monkeypatch.setenv("NEXUS_METRICS_PERSISTENCE", "true")
    env.mon.active = True
    env.mon._evaluator = env.evaluator(only("qdrant_unavailable"))
    monkeypatch.setattr(observability, "monitoring", env.mon)
    monkeypatch.setattr(observability, "metrics", env.recorder)
    api = FastAPI()
    api.include_router(observability.router)
    install_security(api)
    return TestClient(api, raise_server_exceptions=False)


def bearer(key):
    return {"Authorization": f"Bearer {key}"}


@pytest.mark.parametrize("path", ["/system/alerts", "/system/metrics"])
def test_monitoring_endpoints_are_admin_only(client, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers=bearer(ALICE)).status_code == 403
    assert client.get(path, headers={"X-API-Key": "wrong-key-0123456789-abcdefghij"}).status_code == 401
    assert client.get(path, headers=bearer(OPERATOR)).status_code == 200


@pytest.mark.parametrize("method", ["post", "put", "delete", "patch"])
def test_alert_state_cannot_be_modified_through_the_api(client, method):
    assert getattr(client, method)("/system/alerts", headers=bearer(OPERATOR)).status_code == 405


def test_alerts_endpoint_reports_state_and_events(client, env):
    env.recorder.inc("qdrant_unavailable_total", amount=4)
    env.mon.flush()
    env.mon.evaluator.evaluate()
    body = client.get("/system/alerts", headers=bearer(OPERATOR)).json()
    assert body["firing"] == 1 and body["rules"][0]["rule"] == "qdrant_unavailable" and body["rules"][0]["status"] == "firing"
    assert body["recent_events"][0]["event"] == "fired"
    text = json.dumps(body)
    assert OPERATOR not in text and ALICE not in text


def test_metrics_endpoint_serves_persisted_totals_and_gauges(client, env):
    env.recorder.record_request("GET", "/a", 200, 5)
    body = client.get("/system/metrics", headers=bearer(OPERATOR)).json()
    assert body["persistence"]["status"] == "ok" and "persistent" in body["scope"]
    assert counter(body, "http_requests_total") == 1
    assert "alerts_firing" in body["gauges"]
    # a restarted process sees the same totals: wipe the recorder and ask again
    env.recorder.reset()
    assert counter(client.get("/system/metrics", headers=bearer(OPERATOR)).json(), "http_requests_total") == 1


def test_metrics_endpoint_falls_back_when_the_store_is_down(client, env):
    env.recorder.record_request("GET", "/a", 200, 5)
    env.store.snapshot = lambda: (_ for _ in ()).throw(RuntimeError("db down"))  # type: ignore[method-assign]
    body = client.get("/system/metrics", headers=bearer(OPERATOR)).json()
    assert body["persistence"]["status"] == "unavailable" and counter(body, "http_requests_total") == 1


def test_alerts_endpoint_is_a_503_not_a_traceback_when_the_database_fails(client, env):
    from sqlalchemy.exc import OperationalError

    env.mon.evaluator.report = lambda: (_ for _ in ()).throw(OperationalError("select", {}, Exception("secret-dsn")))  # type: ignore[method-assign]
    response = client.get("/system/alerts", headers=bearer(OPERATOR))
    assert response.status_code == 503 and "secret-dsn" not in response.text


def test_alerts_can_be_switched_off(client, monkeypatch):
    monkeypatch.setenv("NEXUS_ALERTS_ENABLED", "false")
    assert client.get("/system/alerts", headers=bearer(OPERATOR)).json() == {
        "enabled": False, "firing": 0, "rules": [], "recent_events": []}
