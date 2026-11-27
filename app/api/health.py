"""Liveness and dependency-aware readiness endpoints."""

import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(tags=["Health"])


@router.get("/health/live")
async def liveness():
    """Return success while the API process is serving requests.

    ``async def`` on purpose: it runs on the event loop, not in the 40-thread pool that synchronous handlers share. When slow
    model calls occupy every pool thread, a synchronous liveness route would queue behind them, time out, and make the proxy
    (and an orchestrator) conclude that a busy-but-alive process is dead (measured in docs/LOAD_TEST_REPORT.md)."""
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

    from app.core.database import engine
    from app.core.llm import get_ollama_health
    from app.core.memory import memory
    from app.core.schema_check import schema_status
    from app.services.rag import rag

    # Each dependency is checked on its own and reported by status only: the unauthenticated response never carries
    # URLs, model lists, hostnames or exception messages.
    try:
        llm_raw = get_ollama_health(timeout=2.0)
    except Exception as exc:
        llm_raw = {"status": "offline", "error_type": type(exc).__name__}
    llm = {key: llm_raw[key] for key in ("status", "chat_model_available", "embedding_model_available", "error_type")
           if key in llm_raw}
    try:
        rag_raw = rag.health_check()
        rag_status = {"status": rag_raw.get("status"), "qdrant": "online" if rag_raw.get("qdrant") == "online" else "offline"}
    except Exception as exc:
        rag_status = {"status": "offline", "qdrant": "offline", "error_type": type(exc).__name__}
    try:
        memory_status = memory.health_check()
        memory_status = {"status": memory_status.get("status")}
    except Exception as exc:
        memory_status = {"status": "offline", "error_type": type(exc).__name__}
    try:
        schema = schema_status(engine)
    except Exception as exc:
        schema = {"status": "unknown", "error_type": type(exc).__name__}

    llm_ready = (
        llm.get("status") == "online"
        and llm.get("chat_model_available", True)
        and llm.get("embedding_model_available", True)
    )
    require_llm = os.getenv("NEXUS_READINESS_REQUIRE_LLM", "true").strip().lower() not in {"0", "false", "no", "off"}
    rag_ready = rag_status.get("status") == "healthy"
    memory_ready = memory_status.get("status") == "online"
    schema_ready = schema.get("status") in {"current", "not_managed"}
    ready = (llm_ready or not require_llm) and rag_ready and memory_ready and schema_ready
    report = {
        "status": "ready" if ready else "not_ready",
        "mode": "production",
        "checks": {
            "api": "ready",
            "llm": {**llm, "required": require_llm},
            "rag": rag_status,
            "memory": memory_status,
            "schema": {key: schema[key] for key in ("status", "error_type") if key in schema},
        },
    }
    return report if ready else JSONResponse(status_code=503, content=report)
