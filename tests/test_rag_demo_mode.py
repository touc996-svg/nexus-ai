from app.services.rag import NexusRAG


def test_demo_rag_starts_without_external_qdrant(monkeypatch):
    monkeypatch.setenv("NEXUS_DEMO_MODE", "true")
    rag = NexusRAG()

    assert rag.demo_mode is True
    assert rag.client is not None


def test_demo_search_does_not_call_ollama(monkeypatch):
    monkeypatch.setenv("NEXUS_DEMO_MODE", "true")
    rag = NexusRAG()

    def unexpected_embedding(_query):
        raise AssertionError("Demo search must not call Ollama")

    monkeypatch.setattr(rag, "embed", unexpected_embedding)

    assert rag.search("test knowledge query") == []


def test_demo_ingestion_is_explicitly_disabled(monkeypatch):
    monkeypatch.setenv("NEXUS_DEMO_MODE", "true")
    rag = NexusRAG()

    result = rag.ingest_text("sample document", "sample.txt")

    assert result["status"] == "disabled"
    assert result["chunks_ingested"] == 0
