"""Monitoring store and alerts against a real PostgreSQL (NEXUS_TEST_POSTGRES_URL); skipped otherwise."""

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.alerts import DEFAULT_RULES, AlertEvaluator, AlertEventRow
from app.core.database import Base, build_engine_options, normalize_database_url
from app.core.metrics import Metrics
from app.core.metrics_store import MetricsStore
from app.core.monitoring import Monitoring
from tests.monitoring_helpers import Clock, counter

POSTGRES_URL = os.getenv("NEXUS_TEST_POSTGRES_URL", "")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="set NEXUS_TEST_POSTGRES_URL to a throwaway PostgreSQL database")


@pytest.fixture
def pg():
    url, kwargs = build_engine_options(normalize_database_url(POSTGRES_URL))
    engine = create_engine(url, **{**kwargs, "pool_size": 10, "max_overflow": 10})
    def wipe():
        Base.metadata.drop_all(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")

    wipe()
    with engine.connect() as connection:  # the real migration path, not create_all
        cfg = Config("alembic.ini")
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
        connection.commit()
    yield sessionmaker(bind=engine, autoflush=False)
    wipe()
    engine.dispose()


def test_concurrent_workers_aggregate_exactly_on_postgres(pg):
    clock = Clock()
    workers = [(Metrics(clock=clock), MetricsStore(pg, clock=clock)) for _ in range(6)]
    monitors = [Monitoring(recorder=r, store=s, clock=clock) for r, s in workers]

    def run(i):
        recorder, _ = workers[i]
        for n in range(300):
            recorder.record_request("GET", "/x", 200 if n % 10 else 500, 20)
            if n % 50 == 0:
                assert monitors[i].flush()
        assert monitors[i].flush()

    with ThreadPoolExecutor(6) as pool:
        list(pool.map(run, range(6)))
    snap = workers[0][1].snapshot()
    assert counter(snap, "http_requests_total") == 1800 and counter(snap, "http_errors_total") == 180
    hist = next(h for h in snap["histograms"] if h["name"] == "http_request_duration_ms")
    assert hist["count"] == 1800 and abs(hist["sum_ms"] - 36000) < 1e-6


def test_duplicate_flush_id_is_ignored_on_postgres(pg):
    recorder = Metrics()
    store = MetricsStore(pg)
    recorder.record_request("GET", "/a", 200, 5)
    counters, hists = recorder.drain_pending()
    assert store.apply("same-id", counters, hists) is True
    assert store.apply("same-id", counters, hists) is False
    assert counter(store.snapshot(), "http_requests_total") == 1


def test_alert_transition_is_single_under_concurrency_on_postgres(pg):
    clock = Clock()
    recorder, store = Metrics(clock=clock), MetricsStore(pg, clock=clock)
    rules = tuple(replace(r) for r in DEFAULT_RULES if r.rule_id == "qdrant_unavailable")
    recorder.inc("qdrant_unavailable_total", amount=9)
    Monitoring(recorder=recorder, store=store, clock=clock).flush()
    evaluators = [AlertEvaluator(store, pg, rules=rules, clock=clock, recorder=recorder) for _ in range(8)]
    barrier = threading.Barrier(8)

    def go(ev):
        barrier.wait()
        return ev.evaluate()

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(go, evaluators))
    assert sum(len(r) for r in results) == 1
    session = pg()
    assert session.execute(select(func.count()).select_from(AlertEventRow)).scalar() == 1
    session.close()
    clock.now += 400
    assert [t["event"] for t in evaluators[0].evaluate()] == ["resolved"]
