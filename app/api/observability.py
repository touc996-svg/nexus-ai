import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import SQLAlchemyError

from app.core.llm import get_ollama_health
from app.core.memory import memory
from app.core.metrics import metrics
from app.core.monitoring import monitoring, persistence_enabled
from app.security.identity import require_admin
from app.services.rag import rag

# System status describes the deployment, not a user's data: administrator credentials only.
router = APIRouter(tags=["Observability"], dependencies=[Depends(require_admin)])


logger = logging.getLogger("nexus.api")


@router.get("/system/metrics")
def system_metrics():
    """Counters and latency histograms. No user text is ever stored in them.

    With persistence on (the default) the figures are the totals stored in the database by every worker, over the retained
    period; this worker first flushes its own pending deltas so the answer is current. If the database cannot be read the
    process-local snapshot is returned and ``persistence.status`` says so.
    """
    local = metrics.snapshot()
    if not (persistence_enabled() and monitoring.active):
        return local
    persistence = {"enabled": True, "flush_seconds": monitoring.flush_seconds,
                   "retention_hours": monitoring.retention_hours}
    try:
        monitoring.flush()
        stored = monitoring.store.snapshot()
        gauges = {}
        try:
            gauges = {"alerts_firing": monitoring.evaluator.firing_gauges()}
        except Exception:  # alert state is optional context for the metrics view
            pass
        persistence.update(status="ok", covers_since=stored.pop("covers_since"),
                           unflushed_batches=monitoring.retry_backlog())
        return {
            "scope": "persistent; aggregated across workers; survives restarts; covers the retention period",
            "uptime_seconds": local["uptime_seconds"],
            **stored,
            "gauges": gauges,
            "persistence": persistence,
        }
    except Exception as exc:
        logger.warning("metrics_store_read_failed error_type=%s", type(exc).__name__,
                       extra={"event": "metrics_store_read_failed", "error_type": type(exc).__name__})
        persistence.update(status="unavailable", error_type=type(exc).__name__)
        return {**local, "persistence": persistence}


@router.get("/system/alerts")
def system_alerts():
    """Persisted alert state: each rule's status, last value, thresholds and counts, plus recent fired/resolved events."""
    from app.core.alerts import alerts_enabled

    if not alerts_enabled():
        return {"enabled": False, "firing": 0, "rules": [], "recent_events": []}
    try:
        return monitoring.evaluator.report()
    except SQLAlchemyError as exc:
        logger.warning("alert_state_read_failed error_type=%s", type(exc).__name__,
                       extra={"event": "alert_state_read_failed", "error_type": type(exc).__name__})
        raise HTTPException(status_code=503, detail="Alert state is unavailable") from exc


@router.get("/system/llm-health")
def llm_health():
    return get_ollama_health()


@router.get("/system/rag-health")
def rag_health():
    report = rag.health_check()
    if not rag.demo_mode:
        llm_report = get_ollama_health()
        report["embedding_provider"] = llm_report.get("status")
        report["embedding_model_available"] = llm_report.get("embedding_model_available")
    return report


@router.get("/system/memory-health")
def memory_health():
    return memory.health_check()


@router.get("/system/evaluation/latest")
def latest_evaluation():
    path = Path(os.getenv(
        "NEXUS_EVALUATION_PATH",
        "artifacts/agent_evaluation_latest.json"
    ))
    if not path.is_file():
        return {
            "status": "not_run",
            "message": "Run python scripts/evaluate_agents.py to create a benchmark report."
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unavailable"}
