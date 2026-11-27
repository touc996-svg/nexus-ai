"""SQL-backed, multi-worker metrics storage (SQLite for development, PostgreSQL for deployment).

Each worker keeps recording into its in-process ``Metrics`` object and periodically hands the *deltas* to this store. Deltas
are bucketed into 60-second windows and added with an atomic ``INSERT ... ON CONFLICT DO UPDATE SET value = value +
excluded.value``, so concurrent workers never overwrite one another and no read-modify-write cycle exists.

Duplicate counting is prevented with a flush id: every batch is written together with a row in ``metric_flushes`` inside
one transaction. If a commit outcome is unknown (for example the connection dropped after COMMIT), the retry reuses the same
id, finds it already recorded, and skips the batch.

Retention: windows older than ``NEXUS_METRICS_RETENTION_HOURS`` (default 72) are deleted, so totals returned by
``snapshot`` cover the retained period (``covers_since`` says from when). Rolling-window queries for alerting read the same
rows. Labels arrive already sanitised by ``Metrics``; this module stores them as a JSON list of at most 400 characters and
caps the number of distinct series like the in-process recorder does (extra series fold into ``overflow``).
"""

import json
import logging
import time
from collections.abc import Callable
from typing import cast

from sqlalchemy import BigInteger, Float, Integer, String, delete, func, select, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.core.database import Base, SessionLocal
from app.core.metrics import BUCKETS_MS, HISTOGRAMS, LABELS, MAX_SERIES, WINDOW_SECONDS

logger = logging.getLogger("nexus.metrics")

BUCKET_COUNT = len(BUCKETS_MS) + 1
OVERFLOW = "overflow"
_BUCKET_COLUMNS = [f"b{index}" for index in range(BUCKET_COUNT)]
DEFAULT_RETENTION_HOURS = 72


class MetricCounterRow(Base):
    __tablename__ = "metric_counters"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    labels_key: Mapped[str] = mapped_column(String(400), primary_key=True)
    window_start: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class MetricHistogramRow(Base):
    __tablename__ = "metric_histograms"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    labels_key: Mapped[str] = mapped_column(String(400), primary_key=True)
    window_start: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sum_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    b0: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    b1: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    b2: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    b3: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    b4: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    b5: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    b6: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    b7: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    b8: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    b9: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    b10: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    b11: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    b12: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    b13: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class MetricFlushRow(Base):
    __tablename__ = "metric_flushes"

    flush_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    applied_at: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    series: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


def _insert(session: Session, table):
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        return sqlite.insert(table)
    if dialect == "postgresql":
        return postgresql.insert(table)
    raise RuntimeError(f"unsupported database dialect for metrics: {dialect}")


def encode_labels(labels: tuple) -> str:
    """Labels as a compact JSON list. Anything that would not fit the 400-character column folds into ``overflow``
    (a truncated JSON string would be unreadable)."""
    encoded = json.dumps([str(label) for label in labels], separators=(",", ":"))
    return encoded if len(encoded) <= 400 else json.dumps([OVERFLOW])


def decode_labels(name: str, labels_key: str, table: dict) -> dict:
    try:
        values = json.loads(labels_key)
    except ValueError:
        values = []
    names = table.get(name, ())
    if values == [OVERFLOW]:
        return {"series": OVERFLOW}
    return dict(zip(names, values, strict=False))


def percentile_upper_bound(buckets: list[int], quantile: float) -> float | None:
    """Upper bound (ms) of the bucket holding ``quantile``. A value past the last bound reports twice that bound so a
    threshold at or above it still fires. ``None`` when there are no samples."""
    total = sum(buckets)
    if total <= 0:
        return None
    rank = quantile * total
    running = 0
    for index, count in enumerate(buckets):
        running += count
        if running >= rank and count > 0:
            return float(BUCKETS_MS[index]) if index < len(BUCKETS_MS) else float(BUCKETS_MS[-1] * 2)
    return float(BUCKETS_MS[-1] * 2)


class MetricsStore:
    def __init__(self, session_factory: Callable[[], Session] = SessionLocal, max_series: int = MAX_SERIES,
                 clock: Callable[[], float] = time.time):
        self._session_factory = session_factory
        self._max_series = max_series
        self._clock = clock
        self._known: set[tuple[str, str]] = set()

    # ---- writing -----------------------------------------------------------------------------------------------

    def apply(self, flush_id: str, counters: dict, histograms: dict) -> bool:
        """Persist one batch atomically. Returns False when this ``flush_id`` was already applied (nothing is added)."""
        session = self._session_factory()
        try:
            if session.get(MetricFlushRow, flush_id) is not None:
                return False
            incoming = ({(k[0], encode_labels(k[1:])) for (_, k) in counters}
                        | {(k[0], encode_labels(k[1:])) for (_, k) in histograms})
            folded = self._folded_series(session, incoming)
            for (window, key), delta in counters.items():
                name, labels = key[0], key[1:]
                if delta == 0:
                    continue
                stored = self._stored_labels(folded, name, labels)
                stmt = _insert(session, MetricCounterRow.__table__).values(
                    name=name, labels_key=stored, window_start=window, value=int(delta))
                session.execute(stmt.on_conflict_do_update(
                    index_elements=["name", "labels_key", "window_start"],
                    set_={"value": MetricCounterRow.__table__.c.value + stmt.excluded.value}))
            for (window, key), hist in histograms.items():
                name, labels = key[0], key[1:]
                stored = self._stored_labels(folded, name, labels)
                values = {"count": int(hist["count"]), "sum_ms": float(hist["sum"])}
                values.update({col: int(n) for col, n in zip(_BUCKET_COLUMNS, hist["buckets"], strict=True)})
                table = MetricHistogramRow.__table__
                stmt = _insert(session, table).values(name=name, labels_key=stored, window_start=window, **values)
                session.execute(stmt.on_conflict_do_update(
                    index_elements=["name", "labels_key", "window_start"],
                    set_={col: table.c[col] + stmt.excluded[col] for col in values}))
            session.add(MetricFlushRow(flush_id=flush_id, applied_at=int(self._clock()),
                                       series=len(counters) + len(histograms)))
            session.commit()
            self._known.update(incoming - folded)
            return True
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _stored_labels(folded: set, name: str, labels: tuple) -> str:
        key = encode_labels(labels)
        return json.dumps([OVERFLOW]) if (name, key) in folded else key

    def _folded_series(self, session: Session, incoming: set) -> set[tuple[str, str]]:
        """Series of this batch that must fold into ``overflow`` because the global series cap is reached."""
        fresh = incoming - self._known
        if not fresh:
            return set()
        existing = {(row.name, row.labels_key) for row in session.execute(
            select(MetricCounterRow.name, MetricCounterRow.labels_key).distinct())}
        existing |= {(row.name, row.labels_key) for row in session.execute(
            select(MetricHistogramRow.name, MetricHistogramRow.labels_key).distinct())}
        room = max(0, self._max_series - len(existing))
        folded: set[tuple[str, str]] = set()
        for item in sorted(fresh - existing):
            if room > 0:
                room -= 1
            else:
                folded.add(item)
        return folded

    def prune(self, retention_seconds: int | None = None) -> int:
        """Delete windows older than the retention period. Idempotent and safe to run from every worker."""
        retention = retention_seconds if retention_seconds is not None else DEFAULT_RETENTION_HOURS * 3600
        now = int(self._clock())
        cutoff = (now - retention) // WINDOW_SECONDS * WINDOW_SECONDS
        session = self._session_factory()
        try:
            removed = 0
            for table in (MetricCounterRow, MetricHistogramRow):
                removed += cast(CursorResult, session.execute(delete(table).where(table.window_start < cutoff))).rowcount or 0
            session.execute(delete(MetricFlushRow).where(MetricFlushRow.applied_at < now - retention))
            session.commit()
            return removed
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ---- reading -----------------------------------------------------------------------------------------------

    def _since(self, window_seconds: int | None) -> int:
        if window_seconds is None:
            return 0
        return (int(self._clock()) - window_seconds) // WINDOW_SECONDS * WINDOW_SECONDS

    def counter_sum(self, name: str, window_seconds: int, where: dict | None = None) -> int:
        """Sum of a counter over the rolling window (whole 60 s windows). ``where`` filters on label names."""
        since = self._since(window_seconds)
        session = self._session_factory()
        try:
            rows = session.execute(
                select(MetricCounterRow.labels_key, func.sum(MetricCounterRow.value))
                .where(MetricCounterRow.name == name, MetricCounterRow.window_start >= since)
                .group_by(MetricCounterRow.labels_key)).all()
        finally:
            session.close()
        total = 0
        for labels_key, value in rows:
            if where:
                labels = decode_labels(name, labels_key, LABELS)
                if any(labels.get(field) != wanted for field, wanted in where.items()):
                    continue
            total += int(value or 0)
        return total

    def histogram_window(self, name: str, window_seconds: int) -> tuple[int, list[int]]:
        """``(sample count, merged bucket counts)`` over the rolling window, all label values merged."""
        since = self._since(window_seconds)
        columns = [func.sum(getattr(MetricHistogramRow, col)) for col in _BUCKET_COLUMNS]
        session = self._session_factory()
        try:
            row = session.execute(
                select(func.sum(MetricHistogramRow.count), *columns)
                .where(MetricHistogramRow.name == name, MetricHistogramRow.window_start >= since)).one()
        finally:
            session.close()
        return int(row[0] or 0), [int(value or 0) for value in row[1:]]

    def snapshot(self) -> dict:
        """Totals across all workers for the retained period, in the same shape as ``Metrics.snapshot``."""
        session = self._session_factory()
        try:
            counters = session.execute(
                select(MetricCounterRow.name, MetricCounterRow.labels_key, func.sum(MetricCounterRow.value))
                .group_by(MetricCounterRow.name, MetricCounterRow.labels_key)).all()
            columns = [func.sum(getattr(MetricHistogramRow, col)) for col in _BUCKET_COLUMNS]
            hists = session.execute(
                select(MetricHistogramRow.name, MetricHistogramRow.labels_key, func.sum(MetricHistogramRow.count),
                       func.sum(MetricHistogramRow.sum_ms), *columns)
                .group_by(MetricHistogramRow.name, MetricHistogramRow.labels_key)).all()
            oldest = session.execute(select(func.min(MetricCounterRow.window_start))).scalar()
            oldest_h = session.execute(select(func.min(MetricHistogramRow.window_start))).scalar()
        finally:
            session.close()
        starts = [v for v in (oldest, oldest_h) if v is not None]
        counter_out = [
            {"name": name, "labels": decode_labels(name, key, LABELS), "value": int(value or 0)}
            for name, key, value in sorted(counters, key=lambda r: (r[0], r[1]))
        ]
        hist_out = []
        for name, key, count, total, *buckets in sorted(hists, key=lambda r: (r[0], r[1])):
            values = [int(b or 0) for b in buckets]
            hist_out.append({
                "name": name, "labels": decode_labels(name, key, HISTOGRAMS), "count": int(count or 0),
                "sum_ms": round(float(total or 0.0), 3),
                "buckets_ms": {**{str(b): c for b, c in zip(BUCKETS_MS, values, strict=False)}, "+Inf": values[-1]},
            })
        return {
            "series": len(counter_out) + len(hist_out),
            "max_series": self._max_series,
            "covers_since": min(starts) if starts else None,
            "counters": counter_out,
            "histograms": hist_out,
        }

    def ping(self) -> None:
        session = self._session_factory()
        try:
            session.execute(text("SELECT 1"))
        finally:
            session.close()
