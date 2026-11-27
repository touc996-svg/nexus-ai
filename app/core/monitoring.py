"""Per-worker background flusher and alert evaluator.

Every worker process runs one daemon thread. About every ``NEXUS_METRICS_FLUSH_SECONDS`` (default 10) it drains the
in-process deltas into the shared SQL store; every ``NEXUS_ALERT_EVAL_SECONDS`` it evaluates the alert rules; roughly once
an hour it prunes expired windows. ``stop()`` (called from the application's shutdown) makes a final flush.

Monitoring must never take the API down. A failed flush keeps its batch (same flush id, so a retry cannot double count)
and tries again on the next cycle; the backlog is bounded and overflow is counted in ``metrics_flush_dropped_total``.

Switches: ``NEXUS_METRICS_PERSISTENCE`` (default true), ``NEXUS_METRICS_FLUSH_SECONDS`` (1-300),
``NEXUS_METRICS_RETENTION_HOURS`` (1-2160, default 72).
"""

import logging
import os
import threading
import time
import uuid

from app.core import alerts as alerts_module
from app.core.database import init_db
from app.core.metrics import Metrics, metrics
from app.core.metrics_store import DEFAULT_RETENTION_HOURS, MetricsStore

logger = logging.getLogger("nexus.monitoring")
MAX_RETRY_BATCHES = 30
PRUNE_INTERVAL_SECONDS = 3600
SHUTDOWN_JOIN_SECONDS = 10


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def persistence_enabled() -> bool:
    return os.getenv("NEXUS_METRICS_PERSISTENCE", "true").strip().lower() in {"1", "true", "yes", "on"}


class Monitoring:
    def __init__(self, recorder: Metrics = metrics, store: MetricsStore | None = None,
                 evaluator: alerts_module.AlertEvaluator | None = None, flush_seconds: int | None = None,
                 retention_hours: int | None = None, clock=time.time):
        self._recorder = recorder
        self._store = store
        self._evaluator = evaluator
        self._flush_seconds = flush_seconds
        self._retention_hours = retention_hours
        self._clock = clock
        self._lock = threading.Lock()  # serialises flushes (thread and on-demand callers)
        self._retry: list[tuple[str, dict, dict]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_eval = 0.0
        self._last_prune = 0.0
        self.active = False
        self.last_flush_ok: bool | None = None

    # ---- lazy wiring (so importing this module never touches the database) -------------------------------------

    @property
    def store(self) -> MetricsStore:
        if self._store is None:
            self._store = MetricsStore()
        return self._store

    @property
    def evaluator(self) -> alerts_module.AlertEvaluator:
        if self._evaluator is None:
            self._evaluator = alerts_module.AlertEvaluator(self.store, recorder=self._recorder)
        return self._evaluator

    @property
    def flush_seconds(self) -> int:
        return self._flush_seconds or _env_int("NEXUS_METRICS_FLUSH_SECONDS", 10, 1, 300)

    @property
    def retention_hours(self) -> int:
        return self._retention_hours or _env_int("NEXUS_METRICS_RETENTION_HOURS", DEFAULT_RETENTION_HOURS, 1, 2160)

    # ---- lifecycle ---------------------------------------------------------------------------------------------

    def start(self) -> bool:
        """Start the background thread. Returns False when persistence is switched off or already running."""
        if not persistence_enabled() or self._thread is not None:
            return False
        flush_seconds, _ = self.flush_seconds, self.retention_hours  # validate configuration at start-up
        if alerts_module.alerts_enabled():
            _ = self.evaluator  # validates NEXUS_ALERT_RULES at start-up
        try:
            init_db()  # creates the tables on SQLite; PostgreSQL uses `alembic upgrade head`
        except Exception as exc:
            logger.warning("monitoring_init_failed error_type=%s", type(exc).__name__,
                           extra={"event": "monitoring_init_failed", "error_type": type(exc).__name__})
        self._stop.clear()
        self.active = True
        self._thread = threading.Thread(target=self._run, args=(flush_seconds,), name="nexus-monitoring", daemon=True)
        self._thread.start()
        logger.info("monitoring_started flush_seconds=%s", flush_seconds, extra={"event": "monitoring_started"})
        return True

    def stop(self) -> None:
        """Graceful shutdown: stop the thread, then make a final flush so the last seconds of data are not lost."""
        thread, self._thread = self._thread, None
        self._stop.set()
        if thread is not None:
            thread.join(timeout=SHUTDOWN_JOIN_SECONDS)
        if self.active:
            self.flush()
        self.active = False

    def _run(self, flush_seconds: int) -> None:
        while not self._stop.wait(flush_seconds):
            try:
                self.flush()
                self._maintain()
            except Exception as exc:  # the loop itself must never die
                logger.warning("monitoring_cycle_failed error_type=%s", type(exc).__name__,
                               extra={"event": "monitoring_cycle_failed", "error_type": type(exc).__name__})

    def _maintain(self) -> None:
        now = self._clock()
        if alerts_module.alerts_enabled() and now - self._last_eval >= alerts_module.eval_interval_seconds():
            self._last_eval = now
            self.evaluator.evaluate()
        if now - self._last_prune >= PRUNE_INTERVAL_SECONDS:
            self._last_prune = now
            try:
                self.store.prune(self.retention_hours * 3600)
            except Exception as exc:
                logger.warning("metrics_prune_failed error_type=%s", type(exc).__name__,
                               extra={"event": "metrics_prune_failed", "error_type": type(exc).__name__})

    # ---- flushing ----------------------------------------------------------------------------------------------

    def flush(self) -> bool:
        """Persist everything pending. True when nothing is left unpersisted. Never raises."""
        with self._lock:
            counters, hists = self._recorder.drain_pending()
            if counters or hists:
                self._retry.append((uuid.uuid4().hex, counters, hists))
            ok = True
            while self._retry:
                flush_id, batch_counters, batch_hists = self._retry[0]
                try:
                    self.store.apply(flush_id, batch_counters, batch_hists)
                except Exception as exc:
                    ok = False
                    self._recorder.inc("metrics_flush_failures_total")
                    logger.warning("metrics_flush_failed error_type=%s pending_batches=%s", type(exc).__name__,
                                   len(self._retry), extra={"event": "metrics_flush_failed",
                                                            "error_type": type(exc).__name__, "count": len(self._retry)})
                    break
                self._retry.pop(0)
            if len(self._retry) > MAX_RETRY_BATCHES:
                dropped = len(self._retry) - MAX_RETRY_BATCHES
                del self._retry[:dropped]  # oldest first
                self._recorder.inc("metrics_flush_dropped_total", amount=dropped)
            self.last_flush_ok = ok
            return ok

    def retry_backlog(self) -> int:
        return len(self._retry)


monitoring = Monitoring()
