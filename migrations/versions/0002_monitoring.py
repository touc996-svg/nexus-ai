"""Persistent metrics (counters, histograms, flush ledger) and alert state/events.

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

BUCKETS = 14  # len(app.core.metrics.BUCKETS_MS) + 1; a test keeps the two in step


def upgrade() -> None:
    op.create_table(
        "metric_counters",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("labels_key", sa.String(400), primary_key=True),
        sa.Column("window_start", sa.BigInteger(), primary_key=True),
        sa.Column("value", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "metric_histograms",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("labels_key", sa.String(400), primary_key=True),
        sa.Column("window_start", sa.BigInteger(), primary_key=True),
        sa.Column("count", sa.BigInteger(), nullable=False),
        sa.Column("sum_ms", sa.Float(), nullable=False),
        *[sa.Column(f"b{index}", sa.BigInteger(), nullable=False) for index in range(BUCKETS)],
    )
    op.create_table(
        "metric_flushes",
        sa.Column("flush_id", sa.String(40), primary_key=True),
        sa.Column("applied_at", sa.BigInteger(), nullable=False),
        sa.Column("series", sa.Integer(), nullable=False),
    )
    op.create_index("ix_metric_flushes_applied_at", "metric_flushes", ["applied_at"])

    op.create_table(
        "alert_state",
        sa.Column("rule_id", sa.String(64), primary_key=True),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("since", sa.Float(), nullable=True),
        sa.Column("last_value", sa.Float(), nullable=True),
        sa.Column("last_samples", sa.BigInteger(), nullable=True),
        sa.Column("last_evaluated", sa.Float(), nullable=True),
        sa.Column("last_fired_at", sa.Float(), nullable=True),
        sa.Column("last_resolved_at", sa.Float(), nullable=True),
        sa.Column("fire_count", sa.BigInteger(), nullable=False),
        sa.Column("resolve_count", sa.BigInteger(), nullable=False),
    )
    op.create_table(
        "alert_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("rule_id", sa.String(64), nullable=False),
        sa.Column("event", sa.String(16), nullable=False),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("window_seconds", sa.Integer(), nullable=False),
        sa.Column("ts", sa.Float(), nullable=False),
    )
    op.create_index("ix_alert_events_rule_id", "alert_events", ["rule_id"])
    op.create_index("ix_alert_events_ts", "alert_events", ["ts"])


def downgrade() -> None:
    # Drops persisted metrics and alert history only; application data from 0001 is untouched.
    op.drop_table("alert_events")
    op.drop_table("alert_state")
    op.drop_table("metric_flushes")
    op.drop_table("metric_histograms")
    op.drop_table("metric_counters")
