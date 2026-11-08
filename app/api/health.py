"""Liveness and dependency-aware readiness endpoints."""

import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(tags=["Health"])


@router.get("/health/live")
def liveness():
    """Return success while the API process is serving requests."""
    return {"status": "alive"}


@router.get("/health/ready")
def readiness():
    """Report whether the configured runtime dependencies are ready."""
    demo_mode = os.getenv("NEXUS_DEMO_MODE", "false").lower() in {
        "1", "true", "yes", "on"
    }
    if demo_mode:
        return {
            "status": "ready",
            "mode": "demo",
            "checks": {
                "api": "ready",
                "llm": "disabled_by_demo_mode",
                "rag": "disabled_by_demo_mode",
                "memory": "stateless_public_demo",
            },
        }

    from app.core.llm import get_ollama_health
    from app.core.memory import memory
    from app.services.rag import rag

    try:
        llm = get_ollama_health(timeout=2.0)
    except Exception as exc:
        llm = {"status": "offline", "error_type": type(exc).__name__}
    try:
        rag_status = rag.health_check()
    except Exception as exc:
        rag_status = {"status": "offline", "error_type": type(exc).__name__}
    try:
        memory_status = memory.health_check()
    except Exception as exc:
        memory_status = {"status": "offline", "error_type": type(exc).__name__}

    llm_ready = (
        llm.get("status") == "online"
        and llm.get("chat_model_available", True)
        and llm.get("embedding_model_available", True)
    )
    rag_ready = rag_status.get("status") == "healthy"
    memory_ready = memory_status.get("status") == "online"
    ready = llm_ready and rag_ready and memory_ready
    report = {
        "status": "ready" if ready else "not_ready",
        "mode": "production",
        "checks": {
            "api": "ready",
            "llm": llm,
            "rag": rag_status,
            "memory": memory_status,
        },
    }
    return report if ready else JSONResponse(status_code=503, content=report)
