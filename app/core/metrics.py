"""In-process, bounded, thread-safe metrics. Nothing here stores or accepts user text.

Counters and latency histograms are keyed by a fixed metric name and a small set of label values that callers
sanitise (route templates, status classes, agent names from a fixed list, error *type* names). The number of distinct
series is capped; past the cap new series fold into one ``overflow`` series per metric name, so memory cannot grow with traffic.
This object is the per-process *recorder*. Every update is also kept as a pending, time-windowed delta; the persistence
layer (``app/core/metrics_store.py`` and ``app/core/monitoring.py``) drains those deltas into the SQL database about every
10 seconds and at graceful shutdown, so totals survive restarts and aggregate across workers. Without that layer the
process-local snapshot below is all there is.
"""

import re
import threading
import time
from time import perf_counter
from types import SimpleNamespace

BUCKETS_MS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000)
MAX_SERIES = 1000
KNOWN_AGENTS = {"enterprise", "personal", "cybersecurity"}
KNOWN_STATUSES = {"success", "failed", "rejected"}
KNOWN_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_ERROR_TYPE = re.compile(r"^[A-Za-z]{1,60}(Error|Exception|Timeout|Busy)$")

# metric name -> label names. Unknown metric names are refused, so no caller can invent a series.
LABELS = {
    "http_requests_total": ("method", "route", "status_class"),
    "http_errors_total": ("route",),
    "llm_calls_total": ("model", "outcome"),
    "llm_failures_total": ("model", "error_type"),
    "llm_tokens_total": ("model", "kind"),
    "llm_usage_unknown_total": ("model",),
    "rag_operations_total": ("op", "outcome"),
    "rag_failures_total": ("op", "error_type"),
    "rag_results_total": ("op",),
    "rag_empty_total": ("op",),
    "rag_chunks_dropped_total": (),
    "agent_runs_total": ("agent", "status"),
    "agent_failures_total": ("agent", "error_type"),
    "http_throttled_total": ("route", "status"),
    "qdrant_unavailable_total": (),
    "alerts_fired_total": ("rule",),
    "alerts_resolved_total": ("rule",),
    "metrics_flush_failures_total": (),
    "metrics_flush_dropped_total": (),
}
THROTTLE_STATUSES = {429, 503}
WINDOW_SECONDS = 60
MAX_PENDING_PER_SERIES_WINDOWS = 30  # bounds the unflushed backlog if the database stays unreachable
HISTOGRAMS = {
    "http_request_duration_ms": ("route",),
    "llm_duration_ms": ("model",),
    "rag_duration_ms": ("op",),
    "agent_duration_ms": ("agent",),
}


def bucket_index(value_ms: float) -> int:
    for index, bound in enumerate(BUCKETS_MS):
        if value_ms <= bound:
            return index
    return len(BUCKETS_MS)


def safe_label(value, limit=80):
    text = str(value) if value is not None else "unknown"
    return "".join(ch if ch.isalnum() or ch in "/{}_-.:" else "_" for ch in text)[:limit] or "unknown"


def safe_error_type(value):
    return value if isinstance(value, str) and _ERROR_TYPE.match(value) else "other"


class Metrics:
    def __init__(self, max_series: int = MAX_SERIES, clock=time.time):
        self._lock = threading.Lock()
        self._max_series = max_series
        self._clock = clock
        self._counters: dict[tuple, int] = {}
        self._histograms: dict[tuple, dict] = {}
        # Deltas not yet persisted, keyed by (window_start, series key). Cleared by drain_pending().
        self._pending_counters: dict[tuple, int] = {}
        self._pending_hist: dict[tuple, dict] = {}
        self._pending_cap = max_series * MAX_PENDING_PER_SERIES_WINDOWS
        self._started = time.time()

    def _window(self) -> int:
        return int(self._clock() // WINDOW_SECONDS * WINDOW_SECONDS)

    def _pending_full(self, table, pending_key) -> bool:
        return pending_key not in table and len(self._pending_counters) + len(self._pending_hist) >= self._pending_cap

    def _series_key(self, name, labels, table):
        key = (name, *labels)
        if key in table:
            return key
        if len(self._counters) + len(self._histograms) >= self._max_series:
            return (name, "overflow")
        return key

    def inc(self, name: str, *labels, amount: int = 1) -> None:
        if name not in LABELS or len(labels) != len(LABELS[name]):
            raise ValueError(f"unknown metric or label count: {name}")
        with self._lock:
            key = self._series_key(name, labels, self._counters)
            self._counters[key] = self._counters.get(key, 0) + amount
            pending_key = (self._window(), key)
            if self._pending_full(self._pending_counters, pending_key):
                self._dropped_locked()
            else:
                self._pending_counters[pending_key] = self._pending_counters.get(pending_key, 0) + amount

    def observe(self, name: str, value_ms: float, *labels) -> None:
        if name not in HISTOGRAMS or len(labels) != len(HISTOGRAMS[name]):
            raise ValueError(f"unknown histogram or label count: {name}")
        with self._lock:
            key = self._series_key(name, labels, self._histograms)
            hist = self._histograms.setdefault(key, {"count": 0, "sum": 0.0, "buckets": [0] * (len(BUCKETS_MS) + 1)})
            index = bucket_index(value_ms)
            hist["count"] += 1
            hist["sum"] += value_ms
            hist["buckets"][index] += 1
            pending_key = (self._window(), key)
            if self._pending_full(self._pending_hist, pending_key):
                self._dropped_locked()
            else:
                pending = self._pending_hist.setdefault(
                    pending_key, {"count": 0, "sum": 0.0, "buckets": [0] * (len(BUCKETS_MS) + 1)})
                pending["count"] += 1
                pending["sum"] += value_ms
                pending["buckets"][index] += 1

    def _dropped_locked(self) -> None:
        key = (self._window(), ("metrics_flush_dropped_total",))
        self._counters[("metrics_flush_dropped_total",)] = self._counters.get(("metrics_flush_dropped_total",), 0) + 1
        self._pending_counters[key] = self._pending_counters.get(key, 0) + 1  # one tiny fixed series may exceed the cap

    def drain_pending(self) -> tuple[dict, dict]:
        """Atomically take the unpersisted deltas. Returns ``(counters, histograms)`` keyed by ``(window, series key)``."""
        with self._lock:
            counters, hists = self._pending_counters, self._pending_hist
            self._pending_counters, self._pending_hist = {}, {}
        return counters, hists

    def pending_size(self) -> int:
        with self._lock:
            return len(self._pending_counters) + len(self._pending_hist)

    # ---- typed recorders: they sanitise every label --------------------------------------------------------------

    def record_request(self, method, route, status, duration_ms):
        method = method if method in KNOWN_METHODS else "OTHER"
        route = safe_label(route, 100)
        self.inc("http_requests_total", method, route, f"{int(status) // 100}xx")
        self.observe("http_request_duration_ms", duration_ms, route)
        if int(status) >= 500:
            self.inc("http_errors_total", route)
        if int(status) in THROTTLE_STATUSES:
            self.inc("http_throttled_total", route, str(int(status)))

    def record_llm(self, model, ok, duration_ms, prompt_tokens=None, completion_tokens=None, error_type=None):
        model = safe_label(model, 60)
        self.inc("llm_calls_total", model, "success" if ok else "failure")
        self.observe("llm_duration_ms", duration_ms, model)
        if not ok:
            self.inc("llm_failures_total", model, safe_error_type(error_type))
            return
        known = False
        for kind, count in (("prompt", prompt_tokens), ("completion", completion_tokens)):
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                self.inc("llm_tokens_total", model, kind, amount=count)
                known = True
        if not known:
            self.inc("llm_usage_unknown_total", model)

    def record_rag(self, op, ok, duration_ms, results=None, error_type=None):
        op = safe_label(op, 20)
        self.inc("rag_operations_total", op, "success" if ok else "failure")
        self.observe("rag_duration_ms", duration_ms, op)
        if not ok:
            self.inc("rag_failures_total", op, safe_error_type(error_type))
        elif isinstance(results, int):
            self.inc("rag_results_total", op, amount=results)
            if results == 0:
                self.inc("rag_empty_total", op)

    def record_chunks_dropped(self, count: int = 1):
        self.inc("rag_chunks_dropped_total", amount=count)

    def record_agent(self, agent, status, duration_ms, error=None):
        agent = agent if agent in KNOWN_AGENTS else "other"
        status = status if status in KNOWN_STATUSES else "other"
        self.inc("agent_runs_total", agent, status)
        if isinstance(duration_ms, (int, float)):
            self.observe("agent_duration_ms", duration_ms, agent)
        if status != "success":
            self.inc("agent_failures_total", agent, safe_error_type(error))

    def rag_timer(self, op):
        return _RagTimer(self, op)

    # ---- output --------------------------------------------------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            counters = [
                {"name": key[0], "labels": dict(zip(LABELS.get(key[0], ()), key[1:], strict=False)), "value": value}
                for key, value in sorted(self._counters.items(), key=lambda item: item[0])
            ]
            histograms = [
                {
                    "name": key[0],
                    "labels": dict(zip(HISTOGRAMS.get(key[0], ()), key[1:], strict=False)),
                    "count": hist["count"],
                    "sum_ms": round(hist["sum"], 3),
                    "buckets_ms": {**{str(b): c for b, c in zip(BUCKETS_MS, hist["buckets"], strict=False)},
                                   "+Inf": hist["buckets"][-1]},
                }
                for key, hist in sorted(self._histograms.items(), key=lambda item: item[0])
            ]
            return {
                "scope": "process-local; resets on restart; not shared across workers or instances",
                "uptime_seconds": round(time.time() - self._started, 1),
                "series": len(self._counters) + len(self._histograms),
                "max_series": self._max_series,
                "counters": counters,
                "histograms": histograms,
            }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._histograms.clear()
            self._pending_counters.clear()
            self._pending_hist.clear()


class _RagTimer:
    """``with metrics.rag_timer("search") as t: ...; t.results = n``. Records outcome and re-raises; never swallows."""

    def __init__(self, metrics: Metrics, op: str):
        self.metrics, self.op, self.results = metrics, op, None

    def __enter__(self):
        self._started = perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        duration = (perf_counter() - self._started) * 1000
        self.metrics.record_rag(self.op, exc_type is None, duration, self.results,
                                exc_type.__name__ if exc_type else None)
        return False


class _InstrumentedCompletions:
    def __init__(self, inner, metrics: Metrics):
        self._inner, self._metrics = inner, metrics

    def create(self, *args, **kwargs):
        model = kwargs.get("model")
        started = perf_counter()
        try:
            response = self._inner.create(*args, **kwargs)
        except Exception as exc:
            self._metrics.record_llm(model, False, (perf_counter() - started) * 1000, error_type=type(exc).__name__)
            raise
        usage = getattr(response, "usage", None)  # only counts are read; never the messages or the reply
        self._metrics.record_llm(
            model, True, (perf_counter() - started) * 1000,
            getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None),
        )
        return response

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _InstrumentedClient:
    def __init__(self, inner, metrics: Metrics):
        self._inner = inner
        self.chat = SimpleNamespace(completions=_InstrumentedCompletions(inner.chat.completions, metrics))

    def __getattr__(self, name):
        return getattr(self._inner, name)


def instrument_llm_client(client, metrics_instance: Metrics | None = None):
    return _InstrumentedClient(client, metrics_instance or metrics)


metrics = Metrics()
