import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.integrations.aegis_x.store import ENV_VAR, load_snapshot
from app.integrations.aegis_x.tools import READ_ONLY_TOOLS, AegisXReadOnlyTools

FIXTURES = Path(__file__).parent / "fixtures" / "aegis_x"
PKG = Path(__file__).resolve().parents[1] / "app" / "integrations" / "aegis_x"
API = Path(__file__).resolve().parents[1] / "app" / "api" / "aegis_x.py"


@pytest.fixture
def tools():
    return AegisXReadOnlyTools(lambda: load_snapshot(FIXTURES))


@pytest.fixture
def client(monkeypatch):
    from app.main import app
    monkeypatch.setenv(ENV_VAR, str(FIXTURES))
    return TestClient(app)


def test_loader_validates_and_quarantines():
    snap = load_snapshot(FIXTURES)
    assert set(snap.findings) == {"f-aws-001", "f-aws-002", "f-az-001", "f-gcp-001"}
    assert set(snap.incidents) == {"inc-1", "inc-empty"}
    reasons = " | ".join(q.reason for q in snap.quarantine)
    assert len(snap.quarantine) == 5
    assert "invalid JSON" in reasons and "duplicate id" in reasons and "schema validation failed" in reasons
    assert snap.findings["f-aws-001"].severity == "high"  # first duplicate kept


def test_loader_unconfigured_and_missing_dir(monkeypatch, tmp_path):
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert load_snapshot().configured is False
    assert load_snapshot(tmp_path / "nope").error


def test_query_filters(tools):
    r = tools.query_findings(cloud="aws")
    assert {f["finding_id"] for f in r["findings"]} == {"f-aws-001", "f-aws-002"}
    assert tools.query_findings(severity="critical")["findings"][0]["finding_id"] == "f-gcp-001"
    assert tools.query_findings(resource="ALICE")["total_matches"] == 2
    from datetime import datetime, timezone
    r = tools.query_findings(since=datetime(2026, 11, 1, 9, 45, tzinfo=timezone.utc))
    assert "f-aws-002" not in {f["finding_id"] for f in r["findings"]}
    r = tools.query_findings(cloud="azure")
    assert r["evidence"] == [{"finding_id": "f-az-001", "native_id": None, "cloud": "azure", "provider": "defender"}]
    assert any("native_id" in u for u in r["uncertainty"])
    assert any("quarantined" in u for u in r["uncertainty"])


def test_empty_result_states_uncertainty(tools):
    r = tools.query_findings(resource="does-not-exist")
    assert r["findings"] == [] and any("absence" in u for u in r["uncertainty"])


def test_incident_summary_flags_missing_findings(tools):
    r = tools.summarize_incident("inc-1")
    assert r["finding_count"] == 2
    assert {e["finding_id"] for e in r["evidence"]} == {"f-aws-001", "f-aws-002"}
    assert any("f-missing-9" in u for u in r["uncertainty"])
    assert tools.summarize_incident("nope")["status"] == "not_found"
    assert any("no finding_ids" in u for u in tools.summarize_incident("inc-empty")["uncertainty"])


def test_correlate_and_no_overclaim(tools):
    r = tools.correlate_evidence(["f-aws-001", "f-aws-002"])
    shared = {(l["shared"], l["value"]) for l in r["links"]}
    assert ("mitre_technique", "T1078") in shared
    assert any("causation" in u for u in r["uncertainty"])
    r = tools.correlate_evidence(["f-aws-001", "f-gcp-001", "nope"])
    assert r["links"] == [] and any("nope" in u for u in r["uncertainty"])


def test_explain_risk_and_recommend_are_text_only(tools):
    r = tools.explain_risk("f-aws-001")
    assert r["evidence"][0]["native_id"] == "gd-abc123"
    assert any("not determinable" in u for u in r["uncertainty"])
    rec = tools.recommend_actions(["f-aws-001"])
    assert rec["executes_actions"] is False
    assert all(isinstance(x, str) for x in rec["recommendations"])
    assert any("human" in x for x in rec["recommendations"])
    assert tools.explain_risk("zzz")["status"] == "not_found"


def test_unavailable_when_unconfigured():
    t = AegisXReadOnlyTools(lambda: load_snapshot(None))
    r = t.query_findings()
    assert r["status"] == "unavailable" and r["uncertainty"]


# ---- Guard tests: no write/exec path, no cloud SDKs, no network
FORBIDDEN_IMPORTS = {"boto3", "botocore", "azure", "google", "requests", "httpx", "urllib",
                     "urllib3", "socket", "subprocess", "aiohttp", "http", "shutil", "os.system"}
FORBIDDEN_CALLS = {"write_text", "write_bytes", "unlink", "rmdir", "mkdir", "rename",
                   "touch", "remove", "system", "popen", "run", "Popen", "write", "writelines", "exec", "eval"}


def _guard_files():
    return list(PKG.glob("*.py")) + [API]


def test_no_cloud_sdk_network_or_process_imports():
    for path in _guard_files():
        for node in ast.walk(ast.parse(path.read_text())):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            for m in mods:
                assert m.split(".")[0] not in FORBIDDEN_IMPORTS, f"{path.name} imports {m}"


def test_no_write_or_exec_calls_and_files_open_read_only():
    for path in _guard_files():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                assert name not in FORBIDDEN_CALLS, f"{path.name} calls {name}"
                if name == "open":
                    mode = [a.value for a in node.args[1:2] if isinstance(a, ast.Constant)]
                    assert not mode or set(mode[0]) <= set("rb"), f"{path.name} opens a file for writing"


def test_tool_surface_is_allowlisted_and_api_has_no_mutating_verbs():
    public = {n for n in dir(AegisXReadOnlyTools) if not n.startswith("_")}
    assert public == set(READ_ONLY_TOOLS)
    from app.main import app
    for route in app.routes:
        if getattr(route, "path", "").startswith("/integrations/aegis-x"):
            assert not (route.methods & {"PUT", "PATCH", "DELETE"}), route.path


def test_agent_rejects_unknown_tool():
    from app.agents.cybersecurity_agent import cybersecurity_agent
    assert cybersecurity_agent.run_aegis_tool("delete_findings")["status"] == "rejected"
    assert cybersecurity_agent.run_aegis_tool("__init__")["status"] == "rejected"


# ---- Endpoints
def test_endpoints(client):
    s = client.get("/integrations/aegis-x/status").json()
    assert s["mode"] == "read-only" and s["findings_loaded"] == 4 and len(s["quarantined"]) == 5
    r = client.post("/integrations/aegis-x/findings/query", json={"cloud": "aws", "severity": "high"})
    assert r.status_code == 200 and r.json()["evidence"][0]["finding_id"] == "f-aws-001"
    assert client.post("/integrations/aegis-x/findings/query", json={"cloud": "oracle"}).status_code == 422
    assert client.post("/integrations/aegis-x/incidents/summary", json={"incident_id": "inc-1"}).json()["finding_count"] == 2
    assert client.post("/integrations/aegis-x/evidence/correlate", json={"finding_ids": ["f-aws-001", "f-aws-002"]}).status_code == 200
    assert client.post("/integrations/aegis-x/risk/explain", json={"finding_id": "f-gcp-001"}).json()["evidence"]
    assert client.post("/integrations/aegis-x/recommendations/text", json={"finding_ids": ["f-aws-001"]}).json()["executes_actions"] is False
    assert client.delete("/integrations/aegis-x/status").status_code == 405


def test_endpoint_unconfigured(monkeypatch):
    from app.main import app
    monkeypatch.delenv(ENV_VAR, raising=False)
    r = TestClient(app).post("/integrations/aegis-x/findings/query", json={})
    assert r.status_code == 200 and r.json()["status"] == "unavailable"
