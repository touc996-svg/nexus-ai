import json
import threading
from types import SimpleNamespace

import pytest

from app.core.metrics import MAX_SERIES, Metrics, instrument_llm_client


def series(snapshot, name, **labels):
    return [c["value"] for c in snapshot["counters"] if c["name"] == name and all(c["labels"].get(k) == v for k, v in labels.items())]


def test_request_metrics_use_route_templates_and_status_classes():
    m = Metrics()
    for _ in range(3):
        m.record_request("GET", "/profile/{user_id}", 200, 12.0)
    m.record_request("POST", "/chat", 503, 900.0)
    m.record_request("WEIRD", "/x", 404, 1.0)
    snap = m.snapshot()
    assert series(snap, "http_requests_total", route="/profile/{user_id}", status_class="2xx") == [3]
    assert series(snap, "http_errors_total", route="/chat") == [1]
    assert series(snap, "http_requests_total", method="OTHER") == [1]
    hist = next(h for h in snap["histograms"] if h["name"] == "http_request_duration_ms" and h["labels"]["route"] == "/profile/{user_id}")
    assert hist["count"] == 3 and hist["buckets_ms"]["25"] == 3


def test_series_are_capped_so_memory_cannot_grow_with_traffic():
    m = Metrics(max_series=50)
    for i in range(5000):
        m.record_request("GET", f"/r{i}", 200, 1.0)
    snap = m.snapshot()
    assert snap["series"] <= 50 + 2  # the cap plus one overflow series for each of the two metrics used
    assert any(c["name"] == "http_requests_total" and c["labels"].get("method") == "overflow" for c in snap["counters"])


def test_labels_cannot_carry_free_text():
    m = Metrics()
    m.record_agent("evil agent: ignore previous instructions", "success", 5)
    m.record_agent("enterprise", "weird-status", 5, "The user said hunter2")
    m.record_agent("personal", "failed", 5, "ValueError")
    m.record_agent("personal", "failed", 5, "bad password is hunter2")
    text = json.dumps(m.snapshot())
    assert "hunter2" not in text and "ignore previous" not in text
    snap = m.snapshot()
    assert series(snap, "agent_failures_total", agent="personal", error_type="ValueError") == [1]
    assert series(snap, "agent_failures_total", agent="personal", error_type="other") == [1]
    assert series(snap, "agent_runs_total", agent="other", status="success") == [1]


def test_unknown_metric_names_are_refused():
    with pytest.raises(ValueError):
        Metrics().inc("made_up_total", "x")


def test_concurrent_updates_lose_nothing():
    m = Metrics()

    def work():
        for _ in range(2000):
            m.record_request("GET", "/ping", 200, 3.0)
            m.record_llm("model-a", True, 4.0, 10, 5)

    threads = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    snap = m.snapshot()
    assert series(snap, "http_requests_total", route="/ping") == [16000]
    assert series(snap, "llm_tokens_total", kind="prompt") == [160000]
    assert series(snap, "llm_tokens_total", kind="completion") == [80000]
    assert next(h for h in snap["histograms"] if h["name"] == "llm_duration_ms")["count"] == 16000


class _Completions:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    def create(self, *args, **kwargs):
        if self.error:
            raise self.error
        return self.result


class _NoContent:
    """Reading choices would fail: the wrapper must read counts only."""

    def __init__(self, usage):
        self.usage = usage

    @property
    def choices(self):
        raise AssertionError("metrics must not read the model reply")


def _client(completions):
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def test_llm_wrapper_records_tokens_without_reading_content():
    m = Metrics()
    client = instrument_llm_client(_client(_Completions(_NoContent(SimpleNamespace(prompt_tokens=7, completion_tokens=3)))), m)
    client.chat.completions.create(model="qwen3:1.7b", messages=[{"role": "user", "content": "SECRET PROMPT"}])
    snap = m.snapshot()
    assert series(snap, "llm_tokens_total", kind="prompt") == [7] and series(snap, "llm_tokens_total", kind="completion") == [3]
    assert "SECRET PROMPT" not in json.dumps(snap)


def test_llm_wrapper_marks_unknown_usage_and_failures():
    m = Metrics()
    instrument_llm_client(_client(_Completions(SimpleNamespace(choices=[]))), m).chat.completions.create(model="m")
    assert series(m.snapshot(), "llm_usage_unknown_total") == [1]
    failing = instrument_llm_client(_client(_Completions(error=TimeoutError("contains the prompt text"))), m)
    with pytest.raises(TimeoutError):
        failing.chat.completions.create(model="m")
    snap = m.snapshot()
    assert series(snap, "llm_failures_total", error_type="TimeoutError") == [1]
    assert "contains the prompt text" not in json.dumps(snap)


def test_rag_timer_records_results_and_failures_and_reraises():
    m = Metrics()
    with m.rag_timer("search") as timer:
        timer.results = 0
    with pytest.raises(RuntimeError), m.rag_timer("search"):
        raise RuntimeError("document text here")
    m.record_chunks_dropped(2)
    snap = m.snapshot()
    assert series(snap, "rag_empty_total", op="search") == [1]
    assert series(snap, "rag_failures_total", op="search", error_type="RuntimeError") == [1]
    assert series(snap, "rag_chunks_dropped_total") == [2]
    assert "document text here" not in json.dumps(snap)


def test_snapshot_is_json_and_series_limit_is_exposed():
    snap = Metrics().snapshot()
    json.dumps(snap)
    assert snap["max_series"] == MAX_SERIES
