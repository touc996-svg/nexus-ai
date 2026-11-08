from qdrant_client import QdrantClient

from app.services.rag import NexusRAG


def build_rag(monkeypatch, score_threshold=0.0):
    monkeypatch.setenv("NEXUS_EMBEDDING_DIMENSION", "768")
    client = QdrantClient(location=":memory:")
    instance = NexusRAG(client=client, demo_mode=False, score_threshold=score_threshold)
    monkeypatch.setattr(instance, "embed", lambda _text: [1.0] + [0.0] * 767)
    return instance


def test_rag_ingests_and_retrieves_with_source_metadata(monkeypatch):
    rag = build_rag(monkeypatch)
    report = rag.ingest_text(
        "The NEXUS enterprise platform stores knowledge for workflow analysis.",
        "operations.txt",
        user_id="user-a",
        metadata={"department": "operations"}
    )
    results = rag.search("enterprise workflow", user_id="user-a")
    assert report["status"] == "indexed"
    assert report["chunks_ingested"] == 1
    assert results
    assert results[0]["source"] == "operations.txt"
    assert results[0]["metadata"]["department"] == "operations"
    assert results[0]["score"] > 0.9


def test_rag_replaces_old_source_chunks_without_duplicates(monkeypatch):
    rag = build_rag(monkeypatch)
    rag.ingest_text("old " * 1600, "policy.txt", user_id="user-a")
    assert rag.list_sources("user-a")[0]["chunks"] > 1
    rag.ingest_text("new policy content", "policy.txt", user_id="user-a")
    latest = rag.list_sources("user-a")
    assert len(latest) == 1
    assert latest[0]["chunks"] == 1


def test_rag_search_and_delete_are_isolated_by_user(monkeypatch):
    rag = build_rag(monkeypatch)
    report = rag.ingest_text("Private customer support policy.", "policy.txt", user_id="alice")
    rag.ingest_text("Different user's knowledge base.", "policy.txt", user_id="bob")
    assert rag.search("support policy", user_id="alice")
    assert rag.search("support policy", user_id="bob")
    assert rag.delete_source(report["source_id"], user_id="alice") is True
    assert rag.list_sources("alice") == []
    assert len(rag.list_sources("bob")) == 1


def test_rag_health_check_reports_collection(monkeypatch):
    rag = build_rag(monkeypatch)
    assert rag.health_check()["qdrant"] == "online"


def test_failed_embedding_keeps_the_previous_document(monkeypatch):
    rag = build_rag(monkeypatch)
    rag.ingest_text("previous indexed content", "guide.txt", user_id="user-a")

    def fail_embedding(_text):
        raise RuntimeError("embedding unavailable")

    monkeypatch.setattr(rag, "embed", fail_embedding)
    try:
        rag.ingest_text("replacement content", "guide.txt", user_id="user-a")
    except RuntimeError:
        pass
    else:
        raise AssertionError("The embedding failure should propagate")

    monkeypatch.setattr(rag, "embed", lambda _text: [1.0] + [0.0] * 767)
    results = rag.search("previous indexed content", user_id="user-a")
    assert results and results[0]["text"] == "previous indexed content"
