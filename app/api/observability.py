import json
import os
from pathlib import Path

from fastapi import APIRouter, Depends

from app.core.llm import get_ollama_health
from app.core.metrics import metrics
from app.core.memory import memory
from app.security.identity import require_admin
from app.services.rag import rag

# System status describes the deployment, not a user's data: administrator credentials only.
router = APIRouter(tags=["Observability"], dependencies=[Depends(require_admin)])


@router.get("/system/metrics")
def system_metrics():
    """Process-local counters and latency histograms. No user text is ever stored in them."""
    return metrics.snapshot()


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
