from unittest.mock import Mock

from app.core import llm


def test_ollama_health_reports_configured_models(monkeypatch):
    response = Mock()
    response.json.return_value = {
        "models": [{"name": llm.MODEL}, {"name": llm.EMBEDDING_MODEL}]
    }
    response.raise_for_status.return_value = None
    monkeypatch.setattr(llm, "DEMO_MODE", False)
    monkeypatch.setattr("app.core.llm.httpx.get", lambda *args, **kwargs: response)
    result = llm.get_ollama_health()
    assert result["status"] == "online"
    assert result["reachable"] is True
    assert result["chat_model_available"] is True
    assert result["embedding_model_available"] is True


def test_ollama_health_returns_safe_offline_diagnostic(monkeypatch):
    monkeypatch.setattr(llm, "DEMO_MODE", False)

    def fail(*args, **kwargs):
        raise TimeoutError("private address and request details")

    monkeypatch.setattr("app.core.llm.httpx.get", fail)
    result = llm.get_ollama_health()
    assert result["status"] == "offline"
    assert result["error_type"] == "TimeoutError"
    assert "private address" not in str(result)


def test_demo_mode_health_does_not_call_ollama(monkeypatch):
    monkeypatch.setattr(llm, "DEMO_MODE", True)

    def fail(*args, **kwargs):
        raise AssertionError("Demo mode must not call Ollama")

    monkeypatch.setattr("app.core.llm.httpx.get", fail)
    assert llm.get_ollama_health()["status"] == "demo"
