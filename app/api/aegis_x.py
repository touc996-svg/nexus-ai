"""READ-ONLY AEGIS-X endpoints. No endpoint executes or mutates anything.

POST is used only to carry query bodies; every route reads local data.
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.orchestrator import nexus
from app.integrations.aegis_x.store import load_snapshot

router = APIRouter(prefix="/integrations/aegis-x", tags=["AEGIS-X (read-only)"])


class FindingsQuery(BaseModel):
    cloud: str | None = Field(default=None, pattern="^(aws|azure|gcp)$")
    severity: str | None = Field(default=None, pattern="^(critical|high|medium|low|informational)$")
    resource: str | None = Field(default=None, max_length=512)
    since: datetime | None = None
    until: datetime | None = None
    limit: int = Field(default=50, ge=1, le=200)


class IncidentQuery(BaseModel):
    incident_id: str = Field(min_length=1, max_length=256)


class FindingIdsQuery(BaseModel):
    finding_ids: list[str] = Field(min_length=1, max_length=50)


class FindingQuery(BaseModel):
    finding_id: str = Field(min_length=1, max_length=256)


def _run(tool, **params):
    result = nexus.run_aegis_x_tool(tool, **params)
    if result.get("status") in {"failed", "rejected"}:
        raise HTTPException(status_code=500, detail=result.get("error", "AEGIS-X read-only tool failed."))
    return result


@router.get("/status")
def aegis_x_status():
    snap = load_snapshot()
    return {
        "mode": "read-only", "configured": snap.configured, "error": snap.error,
        "files_read": snap.files_read, "findings_loaded": len(snap.findings),
        "incidents_loaded": len(snap.incidents), "quarantined": [q.as_dict() for q in snap.quarantine],
    }


@router.post("/findings/query")
def query_findings(request: FindingsQuery):
    return _run("query_findings", **request.model_dump())


@router.post("/incidents/summary")
def incident_summary(request: IncidentQuery):
    return _run("summarize_incident", incident_id=request.incident_id)


@router.post("/evidence/correlate")
def correlate(request: FindingIdsQuery):
    return _run("correlate_evidence", finding_ids=request.finding_ids)


@router.post("/risk/explain")
def explain_risk(request: FindingQuery):
    return _run("explain_risk", finding_id=request.finding_id)


@router.post("/recommendations/text")
def recommend(request: FindingIdsQuery):
    return _run("recommend_actions", finding_ids=request.finding_ids)
