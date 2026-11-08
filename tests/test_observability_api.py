from fastapi.testclient import TestClient

from app.main import app


def test_observability_routes_are_registered(monkeypatch):
    from app.api import observability

    monkeypatch.setattr(observability, "get_ollama_health", lambda: {"status": "offline"})
    monkeypatch.setattr(observability.rag, "health_check", lambda: {"status": "degraded"})
    monkeypatch.setattr(observability.memory, "health_check", lambda: {"status": "online"})
    client = TestClient(app)
    assert client.get("/system/llm-health").status_code == 200
    assert client.get("/system/rag-health").status_code == 200
    assert client.get("/system/memory-health").status_code == 200
    assert client.get("/system/evaluation/latest").status_code == 200


def test_knowledge_source_route_is_registered():
    client = TestClient(app)
    result = client.get("/knowledge/sources")
    assert result.status_code == 200
    assert "sources" in result.json()
