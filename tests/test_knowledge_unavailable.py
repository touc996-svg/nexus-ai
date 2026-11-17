"""When the vector store cannot be reached, the knowledge routes answer 503 with Retry-After, not 500.

A real NexusRAG pointed at a closed port, through the real routes. No model, no network beyond the refused
local connection.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient

from app import main
from app.api import knowledge
from app.services.rag import NexusRAG


@pytest.fixture
def client(monkeypatch):
    for name in ("NEXUS_DEMO_MODE", "APP_ENV", "NEXUS_API_KEY", "NEXUS_API_KEYS", "NEXUS_REQUIRE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NEXUS_EMBEDDING_DIMENSION", "768")
    unreachable = QdrantClient(url="http://127.0.0.1:9", timeout=1)
    rag = NexusRAG(client=unreachable, demo_mode=False)
    monkeypatch.setattr(rag, "embed", lambda _text: [1.0] + [0.0] * 767)
    monkeypatch.setattr(knowledge, "rag", rag)
    monkeypatch.setattr(main, "rag", rag)
    monkeypatch.setattr(main, "DEMO_MODE", False)
    api = FastAPI()
    api.router.routes.extend(main.app.router.routes)
    return TestClient(api, raise_server_exceptions=False)


def _assert_unavailable(response):
    assert response.status_code == 503
    assert response.json() == {"detail": "Knowledge store is unavailable. Retry shortly."}
    assert response.headers["Retry-After"] == "5"
    assert "127.0.0.1" not in response.text and "ResponseHandling" not in response.text


def test_search_reports_the_store_as_unavailable(client):
    _assert_unavailable(client.post("/knowledge/search", json={"query": "anything"}))


def test_ingest_reports_the_store_as_unavailable(client):
    _assert_unavailable(client.post("/knowledge/ingest", files={"file": ("a.txt", b"text " * 20, "text/plain")}))


def test_listing_and_deleting_report_the_store_as_unavailable(client):
    _assert_unavailable(client.get("/knowledge/sources"))
    _assert_unavailable(client.delete("/knowledge/sources/abc123"))


def test_chat_still_answers_without_retrieval(client, monkeypatch):
    # /chat already treats retrieval as optional: the store being down must not fail the conversation.
    monkeypatch.setattr(main.conversation_engine, "understand",
                        lambda *a, **k: {"intent_type": "conversation", "agent": None, "response": "hi"})
    monkeypatch.setattr(main, "DEMO_MODE", True)
    response = client.post("/chat", json={"message": "hello"})
    assert response.status_code == 200 and response.json()["response"] == "hi"


def test_other_failures_are_still_500_without_details(client, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("secret internal detail")
    monkeypatch.setattr(knowledge.rag, "search", boom)
    response = client.post("/knowledge/search", json={"query": "anything"})
    assert response.status_code == 500
    assert response.json() == {"detail": "Knowledge search failed: RuntimeError"}
    assert "secret internal detail" not in response.text


def test_readiness_reports_the_store_down(client):
    response = client.get("/health/ready")
    assert response.status_code == 503 and response.json()["status"] == "not_ready"
