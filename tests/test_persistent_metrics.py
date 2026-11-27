"""Persistent, multi-worker metrics: restart survival, concurrent workers, no double counting, bounded labels, failures."""

import subprocess
import sys
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.orm import sessionmaker

from app.core import alerts as alerts_module  # noqa: F401  (registers the alert tables)
from app.core.database import Base
from app.core.metrics import BUCKETS_MS, WINDOW_SECONDS, Metrics
from app.core.metrics_store import (
    MetricCounterRow,
    MetricFlushRow,
    MetricHistogramRow,
    MetricsStore,
    encode_labels,
)
from app.core.monitoring import Monitoring
from tests.monitoring_helpers import Clock, counter


@pytest.fixture
def db(tmp_path):
    url = f"sqlite:///{tmp_path / 'metrics.db'}"
    engine = create_engine(url, connect_args={"timeout": 30, "check_same_thread": False})
    Base.metadata.create_all(engine)
    yield url, sessionmaker(bind=engine, autoflush=False)
    engine.dispose()


def worker(factory, clock=None, **kwargs):
    clock = clock or Clock()
    recorder = Metrics(clock=clock)
    store = MetricsStore(factory, clock=clock)
    return recorder, Monitoring(recorder=recorder, store=store, clock=clock, **kwargs), store


def test_counters_and_histograms_are_persisted_and_read_back(db):
    _, factory = db
    recorder, mon, store = worker(factory)
    recorder.record_request("GET", "/profile/{user_id}", 200, 12)
    recorder.record_request("GET", "/profile/{user_id}", 500, 300)
    assert mon.flush() is True
    snap = store.snapshot()
    assert counter(snap, "http_requests_total", route="/profile/{user_id}") == 2
    assert counter(snap, "http_errors_total") == 1
    hist = next(h for h in snap["histograms"] if h["name"] == "http_request_duration_ms")
    assert hist["count"] == 2 and hist["sum_ms"] == 312 and hist["buckets_ms"]["25"] == 1 and hist["buckets_ms"]["500"] == 1


def test_metrics_survive_a_restart_and_keep_accumulating(db):
    _, factory = db
    recorder, mon, _ = worker(factory)
    for _ in range(5):
        recorder.record_request("GET", "/a", 200, 5)
    mon.stop() if mon.active else mon.flush()
    # a brand new process: fresh recorder, fresh store object, same database
    recorder2, mon2, store2 = worker(factory)
    assert counter(store2.snapshot(), "http_requests_total") == 5
    for _ in range(3):
        recorder2.record_request("GET", "/a", 200, 5)
    mon2.flush()
    assert counter(store2.snapshot(), "http_requests_total") == 8


def test_the_same_flush_id_is_never_applied_twice(db):
    _, factory = db
    recorder, _, store = worker(factory)
    recorder.record_request("GET", "/a", 200, 5)
    counters, hists = recorder.drain_pending()
    assert store.apply("flush-1", counters, hists) is True
    assert store.apply("flush-1", counters, hists) is False
    assert counter(store.snapshot(), "http_requests_total") == 1


def test_an_unknown_commit_outcome_does_not_double_count(db):
    """The batch is committed but the caller sees an error (connection lost after COMMIT); the retry must not re-add it."""
    _, factory = db
    recorder, mon, store = worker(factory)
    recorder.record_request("GET", "/a", 200, 5)
    real_apply = store.apply
    calls = {"n": 0}

    def lossy(flush_id, counters, hists):
        calls["n"] += 1
        real_apply(flush_id, counters, hists)
        if calls["n"] == 1:
            raise ConnectionError("connection lost after commit")
        return True

    store.apply = lossy  # type: ignore[method-assign]
    assert mon.flush() is False and mon.retry_backlog() == 1
    assert mon.flush() is True and mon.retry_backlog() == 0
    assert counter(store.snapshot(), "http_requests_total") == 1


def test_concurrent_workers_in_threads_aggregate_exactly(db):
    _, factory = db
    workers = [worker(factory) for _ in range(6)]

    def run(index):
        recorder, mon, _ = workers[index]
        for i in range(300):
            recorder.record_request("GET", "/x", 200 if i % 10 else 500, 20)
            if i % 50 == 0:
                mon.flush()
        mon.flush()

    with ThreadPoolExecutor(6) as pool:
        list(pool.map(run, range(6)))
    snap = workers[0][2].snapshot()
    assert counter(snap, "http_requests_total") == 1800
    assert counter(snap, "http_errors_total") == 180
    hist = next(h for h in snap["histograms"] if h["name"] == "http_request_duration_ms")
    assert hist["count"] == 1800


def test_concurrent_worker_processes_aggregate_exactly(db, tmp_path):
    url, factory = db
    script = textwrap.dedent("""
        import sys
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.core.metrics import Metrics
        from app.core.metrics_store import MetricsStore
        from app.core.monitoring import Monitoring
        engine = create_engine(sys.argv[1], connect_args={"timeout": 30})
        factory = sessionmaker(bind=engine, autoflush=False)
        recorder = Metrics()
        mon = Monitoring(recorder=recorder, store=MetricsStore(factory))
        for i in range(200):
            recorder.record_request("POST", "/chat", 200, 40)
            if i % 40 == 0:
                assert mon.flush()
        assert mon.flush()
    """)
    procs = [subprocess.Popen([sys.executable, "-c", script, url], cwd=".") for _ in range(4)]
    assert [p.wait(timeout=120) for p in procs] == [0, 0, 0, 0]
    assert counter(MetricsStore(factory).snapshot(), "http_requests_total") == 800


def test_a_failing_database_keeps_the_batch_and_recovers_without_loss(db):
    _, factory = db
    recorder, mon, store = worker(factory)
    real_apply = store.apply
    state = {"down": True}

    def flaky(flush_id, counters, hists):
        if state["down"]:
            raise RuntimeError("database unavailable")
        return real_apply(flush_id, counters, hists)

    store.apply = flaky  # type: ignore[method-assign]
    for _ in range(4):
        recorder.record_request("GET", "/a", 200, 5)
    assert mon.flush() is False
    recorder.record_request("GET", "/a", 200, 5)
    assert mon.flush() is False and mon.retry_backlog() == 2
    state["down"] = False
    assert mon.flush() is True and mon.retry_backlog() == 0
    snap = store.snapshot()
    assert counter(snap, "http_requests_total") == 5
    mon.flush()
    assert counter(store.snapshot(), "metrics_flush_failures_total") == 2  # the failures were themselves counted


def test_a_long_outage_drops_oldest_batches_and_counts_them(db):
    _, factory = db
    recorder, mon, store = worker(factory)
    store.apply = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))  # type: ignore[method-assign]
    for _ in range(45):
        recorder.record_request("GET", "/a", 200, 5)
        mon.flush()
    assert mon.retry_backlog() == 30
    assert recorder.snapshot()["counters"]
    assert counter(recorder.snapshot(), "metrics_flush_dropped_total") == 15


def test_flush_never_raises_even_when_the_store_explodes(db):
    _, factory = db
    recorder, mon, store = worker(factory)
    store.apply = lambda *a, **k: (_ for _ in ()).throw(BaseException.__new__(RuntimeError))  # type: ignore[method-assign]
    recorder.record_request("GET", "/a", 200, 5)
    assert mon.flush() is False


def test_graceful_shutdown_flushes_the_last_deltas(db, monkeypatch):
    _, factory = db
    monkeypatch.setenv("NEXUS_METRICS_PERSISTENCE", "true")
    monkeypatch.setattr("app.core.monitoring.init_db", lambda: None)
    recorder, mon, store = worker(factory, flush_seconds=300)  # the periodic flush will not run in this test
    assert mon.start() is True
    recorder.record_request("GET", "/a", 200, 5)
    assert counter(store.snapshot(), "http_requests_total") == 0
    mon.stop()
    assert counter(store.snapshot(), "http_requests_total") == 1


def test_the_background_thread_flushes_periodically(db, monkeypatch):
    _, factory = db
    monkeypatch.setenv("NEXUS_METRICS_PERSISTENCE", "true")
    monkeypatch.setattr("app.core.monitoring.init_db", lambda: None)
    recorder, mon, store = worker(factory, flush_seconds=1)
    mon.start()
    try:
        recorder.record_request("GET", "/a", 200, 5)
        done = threading.Event()
        for _ in range(60):
            if counter(store.snapshot(), "http_requests_total") == 1:
                done.set()
                break
            done.wait(0.1)
        assert done.is_set()
    finally:
        mon.stop()


def test_persistence_can_be_switched_off(db, monkeypatch):
    _, factory = db
    monkeypatch.setenv("NEXUS_METRICS_PERSISTENCE", "false")
    _, mon, _ = worker(factory)
    assert mon.start() is False and mon.active is False


def test_rolling_windows_only_include_recent_data(db):
    _, factory = db
    clock = Clock()
    recorder, mon, store = worker(factory, clock)
    recorder.inc("qdrant_unavailable_total", amount=3)
    mon.flush()
    clock.now += 1000
    recorder.inc("qdrant_unavailable_total", amount=2)
    mon.flush()
    assert store.counter_sum("qdrant_unavailable_total", 300) == 2
    assert store.counter_sum("qdrant_unavailable_total", 3600) == 5


def test_prune_removes_expired_windows_only(db):
    _, factory = db
    clock = Clock()
    recorder, mon, store = worker(factory, clock)
    recorder.record_request("GET", "/a", 200, 5)
    mon.flush()
    clock.now += 10 * 3600
    recorder.record_request("GET", "/a", 200, 5)
    mon.flush()
    assert store.prune(5 * 3600) > 0
    assert counter(store.snapshot(), "http_requests_total") == 1
    session = factory()
    assert session.execute(select(func.count()).select_from(MetricFlushRow)).scalar() == 1
    session.close()
    assert store.prune(5 * 3600) == 0  # idempotent


def test_series_are_capped_across_workers_and_fold_into_overflow(db):
    _, factory = db
    clock = Clock()
    store = MetricsStore(factory, max_series=5, clock=clock)
    recorder = Metrics(clock=clock)
    for index in range(12):
        recorder.inc("http_errors_total", f"/route-{index}")
    counters, hists = recorder.drain_pending()
    store.apply("f1", counters, hists)
    snap = store.snapshot()
    labels = {c["labels"].get("route") or c["labels"].get("series") for c in snap["counters"]}
    assert "overflow" in labels and len(snap["counters"]) <= 6
    assert sum(c["value"] for c in snap["counters"]) == 12  # nothing is lost, only folded


def test_labels_stay_bounded_and_sanitised(db):
    _, factory = db
    recorder, mon, store = worker(factory)
    hostile = "/x/" + "A" * 5000 + "';DROP TABLE metric_counters;--<script>"
    recorder.record_request("GET\n", hostile, 200, 5)
    mon.flush()
    from app.core.metrics import safe_label

    counters = store.snapshot()["counters"]
    routes = {c["labels"]["route"] for c in counters if c["name"] == "http_requests_total"}
    assert routes == {safe_label(hostile, 100)} and len(next(iter(routes))) == 100  # exactly the sanitised, bounded value
    assert {c["labels"]["method"] for c in counters if c["name"] == "http_requests_total"} == {"OTHER"}
    for c in counters:
        for value in c["labels"].values():
            assert len(value) <= 100 and all(ch.isalnum() or ch in "/{}_-.:" for ch in value)
    assert len(encode_labels(("x" * 1000,))) <= 400 and encode_labels(("x" * 1000,)) == '["overflow"]'
    with pytest.raises(ValueError):
        recorder.inc("not_a_metric")
    session = factory()
    assert session.execute(select(func.count()).select_from(MetricCounterRow)).scalar() >= 1
    session.close()


def test_histogram_columns_match_the_bucket_layout_and_migration():
    columns = [c.name for c in MetricHistogramRow.__table__.columns if c.name.startswith("b")]
    assert len(columns) == len(BUCKETS_MS) + 1
    import importlib

    assert importlib.import_module("migrations.versions.0002_monitoring").BUCKETS == len(BUCKETS_MS) + 1


def test_pending_deltas_are_window_bucketed_and_drain_exactly_once():
    clock = Clock(1_700_000_030.0)
    recorder = Metrics(clock=clock)
    recorder.inc("llm_usage_unknown_total", "m")
    clock.now += WINDOW_SECONDS
    recorder.inc("llm_usage_unknown_total", "m", amount=4)
    counters, _ = recorder.drain_pending()
    assert sorted(counters.values()) == [1, 4] and len({w for w, _ in counters}) == 2
    assert recorder.drain_pending() == ({}, {})
    assert recorder.snapshot()["counters"][0]["value"] == 5  # the in-process totals are unchanged by draining


def test_throttled_responses_and_qdrant_events_are_recorded():
    recorder = Metrics()
    recorder.record_request("POST", "/knowledge/ingest", 429, 3)
    recorder.record_request("POST", "/knowledge/ingest", 503, 3)
    recorder.record_request("POST", "/knowledge/ingest", 200, 3)
    by = {(c["labels"]["route"], c["labels"]["status"]): c["value"] for c in recorder.snapshot()["counters"]
          if c["name"] == "http_throttled_total"}
    assert by == {("/knowledge/ingest", "429"): 1, ("/knowledge/ingest", "503"): 1}


def test_tables_exist_after_create_all(db):
    url, _ = db
    names = set(inspect(create_engine(url)).get_table_names())
    assert {"metric_counters", "metric_histograms", "metric_flushes", "alert_state", "alert_events"} <= names
