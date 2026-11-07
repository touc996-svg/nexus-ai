import pytest
from fastapi.testclient import TestClient
from app.main import app, nexus

@pytest.fixture
def client():
    return TestClient(app)

@pytest.fixture
def mock_agents(monkeypatch):
    calls = []

    for domain, agent in nexus.agents.items():
        def fake_run(message, *args, _domain=domain, **kwargs):
            calls.append(_domain)
            return {
                "agent": _domain,
                "response": f"{_domain} integration test response",
                "status": "success"
            }

        monkeypatch.setattr(agent, "run", fake_run)

    return calls

@pytest.mark.parametrize(
    "domain",
    ["enterprise", "personal", "cybersecurity"]
)
def test_individual_agent_api(client, mock_agents, domain):
    response = client.post(
        f"/agents/{domain}",
        json={"message": "Analyze business security operations"}
    )

    assert response.status_code == 200
    assert domain in response.text

def test_complete_team_workflow(client, mock_agents):
    response = client.post(
        "/team",
        json={
            "message": "Review company security operations and recommend defensive improvements"
        }
    )

    assert response.status_code == 200

    result = response.json()

    assert result["status"] == "completed"
    assert set(result["agents"]) == {
        "enterprise",
        "personal",
        "cybersecurity"
    }
    assert result["failed_agents"] == []
    assert "final_report" in result

    assert mock_agents == [
        "enterprise",
        "personal",
        "cybersecurity"
    ]
