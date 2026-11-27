"""Rule-based internal alerting over the persisted metrics. Delivery is logs and ``GET /system/alerts`` only.

Rules read rolling windows (whole 60-second windows) of the shared metrics tables, so every worker sees the same numbers.
State lives in ``alert_state``; a transition (ok -> firing, firing -> ok) is a compare-and-swap ``UPDATE ... WHERE status =
<expected>``, so when several workers evaluate at the same moment exactly one of them performs the transition, writes the
event row and emits the ``alert_fired`` / ``alert_resolved`` log line. A firing alert is not re-announced on each
evaluation (deduplication), and after a resolution the same rule cannot fire again until its cooldown has passed
(flapping protection). Too little traffic (below ``min_samples``) counts as healthy, so an alert resolves when load stops.

Configuration: ``NEXUS_ALERTS_ENABLED`` (default true), ``NEXUS_ALERT_EVAL_SECONDS`` (default 30), and
``NEXUS_ALERT_RULES``, a JSON object ``{"rule_id": {"threshold": ..., "window_seconds": ..., "min_samples": ...,
"cooldown_seconds": ..., "enabled": ...}}`` that overrides the defaults below. Unknown rules or fields are refused.
"""

import json
import logging
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import cast

from sqlalchemy import BigInteger, Float, Integer, String, delete, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.core.database import Base, SessionLocal
from app.core.metrics import WINDOW_SECONDS, metrics
from app.core.metrics_store import MetricsStore, _insert, percentile_upper_bound

logger = logging.getLogger("nexus.alerts")

EVENT_RETENTION_SECONDS = 14 * 24 * 3600
MAX_EVENTS_RETURNED = 50


class AlertStateRow(Base):
    __tablename__ = "alert_state"

    rule_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(8), nullable=False, default="ok")  # ok | firing
    since: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_samples: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_evaluated: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_fired_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_resolved_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    fire_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    resolve_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class AlertEventRow(Base):
    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(16), nullable=False)  # fired | resolved
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    ts: Mapped[float] = mapped_column(Float, nullable=False, index=True)


@dataclass(frozen=True)
class AlertRule:
    rule_id: str
    description: str
    kind: str  # ratio | p95 | count
    threshold: float
    window_seconds: int = 300
    min_samples: int = 0
    cooldown_seconds: int = 300
    severity: str = "warning"
    enabled: bool = True
    metric: str = ""  # counter (numerator), or histogram for p95
    denominator: str = ""
    where: dict = field(default_factory=dict)
    denominator_where: dict = field(default_factory=dict)
    unit: str = ""


DEFAULT_RULES: tuple[AlertRule, ...] = (
    AlertRule("http_5xx_rate", "Share of HTTP responses that were 5xx", "ratio", 0.05, 300, 20, severity="critical",
              metric="http_errors_total", denominator="http_requests_total", unit="ratio"),
    AlertRule("http_p95_latency", "95th percentile HTTP request latency", "p95", 5000, 300, 20,
              metric="http_request_duration_ms", unit="ms"),
    AlertRule("qdrant_unavailable", "Qdrant unavailable after retries", "count", 3, 300, 0, severity="critical",
              metric="qdrant_unavailable_total", unit="events"),
    AlertRule("llm_failure_rate", "Share of language-model calls that failed", "ratio", 0.2, 300, 5, severity="critical",
              metric="llm_calls_total", where={"outcome": "failure"}, denominator="llm_calls_total", unit="ratio"),
    AlertRule("llm_p95_latency", "95th percentile language-model call latency", "p95", 30000, 300, 5,
              metric="llm_duration_ms", unit="ms"),
    AlertRule("extraction_rejections", "Document ingestion answered 429 or 503", "count", 5, 300, 0,
              metric="http_throttled_total", where={"route": "/knowledge/ingest"}, unit="responses"),
    AlertRule("rag_failure_rate", "Share of RAG operations that failed", "ratio", 0.25, 300, 5,
              metric="rag_operations_total", where={"outcome": "failure"}, denominator="rag_operations_total",
              unit="ratio"),
    AlertRule("agent_failure_rate", "Share of agent runs that did not succeed", "ratio", 0.25, 300, 5,
              metric="agent_failures_total", denominator="agent_runs_total", unit="ratio"),
)
RULE_FIELDS = {"threshold", "window_seconds", "min_samples", "cooldown_seconds", "enabled"}


def _truthy(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def alerts_enabled() -> bool:
    return _truthy(os.getenv("NEXUS_ALERTS_ENABLED", "true"))


def eval_interval_seconds() -> int:
    raw = os.getenv("NEXUS_ALERT_EVAL_SECONDS", "").strip()
    if not raw:
        return 30
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("NEXUS_ALERT_EVAL_SECONDS must be an integer") from exc
    if not 5 <= value <= 3600:
        raise ValueError("NEXUS_ALERT_EVAL_SECONDS must be between 5 and 3600")
    return value


def load_rules(raw: str | None = None) -> tuple[AlertRule, ...]:
    raw = os.getenv("NEXUS_ALERT_RULES", "") if raw is None else raw
    if not raw.strip():
        return DEFAULT_RULES
    try:
        overrides = json.loads(raw)
    except ValueError as exc:
        raise ValueError("NEXUS_ALERT_RULES must be valid JSON") from exc
    if not isinstance(overrides, dict):
        raise ValueError("NEXUS_ALERT_RULES must be a JSON object")
    known = {rule.rule_id: rule for rule in DEFAULT_RULES}
    for rule_id, change in overrides.items():
        if rule_id not in known:
            raise ValueError(f"NEXUS_ALERT_RULES: unknown rule '{str(rule_id)[:40]}'")
        if not isinstance(change, dict) or set(change) - RULE_FIELDS:
            raise ValueError(f"NEXUS_ALERT_RULES: '{rule_id}' accepts only {sorted(RULE_FIELDS)}")
        known[rule_id] = _validated(replace(known[rule_id], **change))
    return tuple(known.values())


def _validated(rule: AlertRule) -> AlertRule:
    def number(value) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    if not number(rule.threshold) or rule.threshold <= 0 or (rule.kind == "ratio" and rule.threshold > 1):
        raise ValueError(f"alert rule {rule.rule_id}: threshold out of range")
    if not isinstance(rule.window_seconds, int) or isinstance(rule.window_seconds, bool) \
            or not WINDOW_SECONDS <= rule.window_seconds <= 86400:
        raise ValueError(f"alert rule {rule.rule_id}: window_seconds must be 60-86400")
    if not isinstance(rule.min_samples, int) or isinstance(rule.min_samples, bool) or not 0 <= rule.min_samples <= 1_000_000:
        raise ValueError(f"alert rule {rule.rule_id}: min_samples must be 0-1000000")
    if not isinstance(rule.cooldown_seconds, int) or isinstance(rule.cooldown_seconds, bool) \
            or not 0 <= rule.cooldown_seconds <= 86400:
        raise ValueError(f"alert rule {rule.rule_id}: cooldown_seconds must be 0-86400")
    if not isinstance(rule.enabled, bool):
        raise ValueError(f"alert rule {rule.rule_id}: enabled must be true or false")
    return rule


class AlertEvaluator:
    def __init__(self, store: MetricsStore, session_factory: Callable[[], Session] = SessionLocal,
                 rules: tuple[AlertRule, ...] | None = None, clock: Callable[[], float] = time.time,
                 recorder=metrics):
        self._recorder = recorder
        self._store = store
        self._session_factory = session_factory
        self._rules = rules if rules is not None else load_rules()
        self._clock = clock

    @property
    def rules(self) -> tuple[AlertRule, ...]:
        return self._rules

    # ---- measurement ----------------------------------------------------------------------------------------------

    def measure(self, rule: AlertRule) -> tuple[float | None, int]:
        """``(value, samples)``; ``value`` is None when there is too little data to judge."""
        if rule.kind == "count":
            total = self._store.counter_sum(rule.metric, rule.window_seconds, rule.where)
            return float(total), total
        if rule.kind == "ratio":
            denominator = self._store.counter_sum(rule.denominator, rule.window_seconds, rule.denominator_where)
            if denominator <= 0 or denominator < rule.min_samples:
                return None, denominator
            numerator = self._store.counter_sum(rule.metric, rule.window_seconds, rule.where)
            return numerator / denominator, denominator
        if rule.kind == "p95":
            samples, buckets = self._store.histogram_window(rule.metric, rule.window_seconds)
            if samples <= 0 or samples < rule.min_samples:
                return None, samples
            return percentile_upper_bound(buckets, 0.95), samples
        raise ValueError(f"unknown rule kind {rule.kind}")

    # ---- state machine --------------------------------------------------------------------------------------------

    def evaluate(self) -> list[dict]:
        """Evaluate every enabled rule once. Returns the transitions this call performed (empty when nothing changed)."""
        transitions = []
        for rule in self._rules:
            if not rule.enabled:
                continue
            try:
                outcome = self._evaluate_rule(rule)
            except Exception as exc:  # one bad rule must not stop the others
                logger.warning("alert_evaluation_failed error_type=%s", type(exc).__name__,
                               extra={"event": "alert_evaluation_failed", "rule": rule.rule_id,
                                      "error_type": type(exc).__name__})
                continue
            if outcome:
                transitions.append(outcome)
        self._prune_events()
        return transitions

    def _ensure_row(self, session: Session, rule_id: str) -> None:
        stmt = _insert(session, AlertStateRow.__table__).values(
            rule_id=rule_id, status="ok", fire_count=0, resolve_count=0)
        session.execute(stmt.on_conflict_do_nothing(index_elements=["rule_id"]))

    def _evaluate_rule(self, rule: AlertRule) -> dict | None:
        value, samples = self.measure(rule)
        now = self._clock()
        if value is None:
            breached = False
        elif rule.kind == "count":
            breached = value >= rule.threshold  # "N or more events in the window"
        else:
            breached = value > rule.threshold
        session = self._session_factory()
        try:
            self._ensure_row(session, rule.rule_id)
            state = session.get(AlertStateRow, rule.rule_id, populate_existing=True)
            if state is None:  # the row was just ensured; only a concurrent delete could get here
                raise RuntimeError("alert state row vanished")
            status, resolved_at = state.status, state.last_resolved_at
            transition = None
            if breached and status == "ok":
                cooling = resolved_at is not None and now - resolved_at < rule.cooldown_seconds
                if not cooling:
                    transition = self._swap(session, rule, "ok", "firing", now, value,
                                            {"since": now, "last_fired_at": now,
                                             "fire_count": AlertStateRow.fire_count + 1})
            elif not breached and status == "firing":
                transition = self._swap(session, rule, "firing", "ok", now, value,
                                        {"since": None, "last_resolved_at": now,
                                         "resolve_count": AlertStateRow.resolve_count + 1})
            session.execute(update(AlertStateRow).where(AlertStateRow.rule_id == rule.rule_id).values(
                last_value=value, last_samples=samples, last_evaluated=now))
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        if transition:
            self._announce(rule, transition, value)
        return transition

    def _swap(self, session: Session, rule: AlertRule, expected: str, new: str, now: float, value,
              extra: dict) -> dict | None:
        result = session.execute(update(AlertStateRow).where(
            AlertStateRow.rule_id == rule.rule_id, AlertStateRow.status == expected).values(status=new, **extra))
        if cast(CursorResult, result).rowcount != 1:
            return None  # another worker performed the transition first
        event = "fired" if new == "firing" else "resolved"
        session.add(AlertEventRow(rule_id=rule.rule_id, event=event, value=value, threshold=rule.threshold,
                                  window_seconds=rule.window_seconds, ts=now))
        return {"rule": rule.rule_id, "event": event, "value": value, "threshold": rule.threshold}

    def _announce(self, rule: AlertRule, transition: dict, value) -> None:
        event = f"alert_{transition['event']}"
        self._recorder.inc("alerts_fired_total" if transition["event"] == "fired" else "alerts_resolved_total", rule.rule_id)
        log = logger.warning if transition["event"] == "fired" else logger.info
        log("%s rule=%s value=%s threshold=%s window_seconds=%s severity=%s", event, rule.rule_id,
            None if value is None else round(value, 4), rule.threshold, rule.window_seconds, rule.severity,
            extra={"event": event, "rule": rule.rule_id, "value": None if value is None else round(value, 4),
                   "threshold": rule.threshold, "window_seconds": rule.window_seconds, "severity": rule.severity})

    def _prune_events(self) -> None:
        session = self._session_factory()
        try:
            session.execute(delete(AlertEventRow).where(AlertEventRow.__table__.c.ts < self._clock() - EVENT_RETENTION_SECONDS))
            session.commit()
        except Exception:
            session.rollback()
        finally:
            session.close()

    # ---- reporting ------------------------------------------------------------------------------------------------

    def report(self) -> dict:
        now = self._clock()
        session = self._session_factory()
        try:
            states = {row.rule_id: row for row in session.execute(select(AlertStateRow)).scalars()}
            events = session.execute(
                select(AlertEventRow).order_by(AlertEventRow.ts.desc(), AlertEventRow.id.desc()).limit(MAX_EVENTS_RETURNED)
            ).scalars().all()
        finally:
            session.close()
        rules = []
        for rule in self._rules:
            row = states.get(rule.rule_id)
            status = row.status if row else "ok"
            cooldown_until = None
            if row and row.last_resolved_at is not None and status == "ok":
                until = row.last_resolved_at + rule.cooldown_seconds
                cooldown_until = until if until > now else None
            rules.append({
                "rule": rule.rule_id, "description": rule.description, "severity": rule.severity,
                "enabled": rule.enabled, "status": status if rule.enabled else "disabled",
                "threshold": rule.threshold, "window_seconds": rule.window_seconds, "min_samples": rule.min_samples,
                "cooldown_seconds": rule.cooldown_seconds, "unit": rule.unit,
                "value": row.last_value if row else None, "samples": row.last_samples if row else None,
                "firing_since": row.since if row and status == "firing" else None,
                "last_evaluated": row.last_evaluated if row else None,
                "last_fired_at": row.last_fired_at if row else None,
                "last_resolved_at": row.last_resolved_at if row else None,
                "fire_count": int(row.fire_count) if row else 0, "resolve_count": int(row.resolve_count) if row else 0,
                "cooldown_until": cooldown_until,
            })
        return {
            "enabled": True,
            "firing": sum(1 for r in rules if r["status"] == "firing"),
            "rules": rules,
            "recent_events": [
                {"rule": e.rule_id, "event": e.event, "value": e.value, "threshold": e.threshold,
                 "window_seconds": e.window_seconds, "ts": e.ts} for e in events
            ],
        }

    def firing_gauges(self) -> dict[str, int]:
        return {rule["rule"]: 1 if rule["status"] == "firing" else 0 for rule in self.report()["rules"]}
