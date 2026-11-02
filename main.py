
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.core.orchestrator import nexus
from app.core.decision_engine import decision_engine
from app.core.memory import memory
from app.core.router import router


app = FastAPI(
    title="NEXUS AI",
    description=(
        "Unified Enterprise, Personal Assistant, "
        "and Cybersecurity AI Platform"
    ),
    version="0.6.0"
)


class AgentRequest(BaseModel):
    message: str


class MemoryRequest(BaseModel):
    role: str
    content: str


@app.get("/")
def home():
    dashboard_path = (
        Path(__file__).parent / "dashboard" / "index.html"
    )

    if not dashboard_path.exists():
        raise HTTPException(
            status_code=404,
            detail="Dashboard not found"
        )

    return FileResponse(dashboard_path)


@app.get("/decision-engine/status")
def decision_engine_status():
    return {
        "name": decision_engine.__class__.__name__,
        "status": "active",
        "mode": "rule_based",
        "capabilities": [
            "Risk detection",
            "Potential contradiction detection",
            "Agent failure detection",
            "Priority classification",
            "Decision generation"
        ]
    }


@app.get("/memory/status")
def memory_status():
    return {
        "name": "NexusMemory",
        "status": "active",
        "storage": "sqlite",
        "database": "nexus.db",
        "max_messages_per_session": memory.max_messages
    }


@app.post("/memory/{session_id}")
def save_memory(session_id: str, request: MemoryRequest):
    if request.role not in ["user", "assistant", "system"]:
        raise HTTPException(
            status_code=400,
            detail="Invalid role"
        )

    result = memory.add_message(
        session_id,
        request.role,
        request.content
    )

    return {
        "status": "saved",
        "session_id": session_id,
        "message": result
    }


@app.get("/memory/{session_id}")
def get_memory(session_id: str):
    return {
        "session_id": session_id,
        "history": memory.get_history(session_id)
    }


@app.delete("/memory/{session_id}")
def delete_memory(session_id: str):
    deleted = memory.clear_history(session_id)

    return {
        "session_id": session_id,
        "deleted": deleted,
        "status": "deleted" if deleted else "not_found"
    }


@app.get("/agents/{domain}")
def get_agent(domain: str):
    result = nexus.get_agent(domain)

    if "error" in result:
        raise HTTPException(
            status_code=404,
            detail=result["error"]
        )

    return result


@app.post("/agents/{domain}")
def run_agent(domain: str, request: AgentRequest):
    result = nexus.run_agent(domain, request.message)

    if "error" in result:
        raise HTTPException(
            status_code=400,
            detail=result["error"]
        )

    return result


@app.post("/team")
def run_team(request: AgentRequest):
    result = nexus.run_team(request.message)

    if "error" in result:
        raise HTTPException(
            status_code=400,
            detail=result["error"]
        )

    return result


@app.post("/auto")
def auto_route(request: AgentRequest):
    routing = router.route(request.message)

    result = nexus.run_agent(
        routing["selected_agent"],
        request.message
    )

    if "error" in result:
        raise HTTPException(
            status_code=400,
            detail=result["error"]
        )

    return {
        "routing": routing,
        "result": result,
        "status": "completed"
    }
