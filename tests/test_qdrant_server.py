"""Against real Qdrant servers. Skipped unless NEXUS_TEST_QDRANT_URL (open server) and, for the key tests,
NEXUS_TEST_QDRANT_AUTH_URL + NEXUS_TEST_QDRANT_API_KEY (server started with that API key) are set. Use throwaway servers.
CI provides both as service containers."""

import os
import socket
import threading
import time
import uuid

import pytest
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse

from app.services.qdrant_config import (
    QdrantSettings,
    QdrantUnavailable,
    build_qdrant_client,
)
from app.services.rag import NexusRAG

OPEN_URL = os.getenv("NEXUS_TEST_QDRANT_URL", "")
AUTH_URL = os.getenv("NEXUS_TEST_QDRANT_AUTH_URL", "")
API_KEY = os.getenv("NEXUS_TEST_QDRANT_API_KEY", "")
needs_open = pytest.mark.skipif(not OPEN_URL, reason="set NEXUS_TEST_QDRANT_URL to a throwaway Qdrant server")
needs_auth = pytest.mark.skipif(not (AUTH_URL and API_KEY), reason="set NEXUS_TEST_QDRANT_AUTH_URL and _API_KEY")

DIM = 8


def _vector(text: str) -> list[float]:
    values = [float((hash(text) >> shift) % 7 + 1) for shift in range(DIM)]
    norm = sum(v * v for v in values) ** 0.5
    return [v / norm for v in values]


def _rag(url, api_key=None):
    rag = NexusRAG(client=build_qdrant_client(QdrantSettings(url=url, api_key=api_key, timeout_seconds=10, max_retries=0)),
                   demo_mode=False, score_threshold=-1.0)
    rag.COLLECTION = f"nexus_test_{uuid.uuid4().hex[:10]}"
    rag.vector_size = DIM
    rag.embed = _vector  # the embedding model is not under test; vectors are deterministic
    return rag


@pytest.fixture
def rag():
    instance = _rag(OPEN_URL)
    yield instance
    instance.client.delete_collection(instance.COLLECTION)


@needs_open
def test_tenants_only_ever_see_their_own_chunks(rag):
    rag.ingest_text("alice private roadmap alpha", source="a.txt", user_id="alice")
    rag.ingest_text("bob private roadmap beta", source="b.txt", user_id="bob")
    alice = rag.search("roadmap", user_id="alice", limit=10)
    bob = rag.search("roadmap", user_id="bob", limit=10)
    assert alice and bob
    assert all("alice" in hit["text"] for hit in alice) and all("bob" in hit["text"] for hit in bob)
    assert [s["source"] for s in rag.list_sources("alice")] == ["a.txt"]
    bob_source = rag.list_sources("bob")[0]["source_id"]
    rag.delete_source(bob_source, user_id="alice")  # the delete is filtered by tenant, so it must remove nothing of bob's
    assert rag.search("roadmap", user_id="bob", limit=10)


@needs_open
def test_user_id_is_indexed_as_the_tenant_key(rag):
    rag.ingest_text("anything", source="x.txt", user_id="alice")
    schema = rag.client.get_collection(rag.COLLECTION).payload_schema
    assert "user_id" in schema and schema["user_id"].data_type.value == "keyword"
    assert schema["user_id"].params.is_tenant is True


@needs_open
def test_the_tenant_index_setup_is_repeatable(rag):
    rag.ingest_text("anything", source="x.txt", user_id="alice")
    again = NexusRAG(client=rag.client, demo_mode=False)
    again.COLLECTION = rag.COLLECTION
    again.vector_size = DIM
    assert again.client is not None  # second start-up: collection and index already exist


@needs_auth
def test_requests_without_or_with_a_wrong_key_are_refused():
    for key in (None, "wrong-key-0123456789"):
        rag = _rag(AUTH_URL, key)
        with pytest.raises(UnexpectedResponse) as caught:
            rag.client  # noqa: B018 - first access creates the collection
        assert caught.value.status_code in {401, 403}
        assert "wrong-key" not in str(caught.value)


@needs_auth
def test_the_right_key_works_end_to_end():
    rag = _rag(AUTH_URL, API_KEY)
    try:
        rag.ingest_text("keyed content", source="k.txt", user_id="alice")
        assert rag.search("keyed", user_id="alice", limit=3)
        assert rag.health_check()["qdrant"] == "online"
    finally:
        rag.client.delete_collection(rag.COLLECTION)


def test_an_unresponsive_server_fails_within_the_timeout_with_a_safe_error():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = listener.getsockname()[1]
    held = []
    def accept_forever():
        try:
            while True:
                held.append(listener.accept())  # accept and never answer
        except OSError:
            return  # listener closed at the end of the test

    threading.Thread(target=accept_forever, daemon=True).start()
    try:
        client = build_qdrant_client(QdrantSettings(url=f"http://127.0.0.1:{port}", timeout_seconds=1, max_retries=1))
        started = time.monotonic()
        with pytest.raises(QdrantUnavailable) as caught:
            client.get_collection("anything")
        assert time.monotonic() - started < 6
        assert str(port) not in str(caught.value)
    finally:
        listener.close()


def test_health_check_reports_an_unreachable_server_without_raising():
    rag = NexusRAG(client=QdrantClient(location=":memory:"), demo_mode=False)
    rag._client = build_qdrant_client(QdrantSettings(url="http://127.0.0.1:9", timeout_seconds=1, max_retries=0))
    rag._client_ready = True
    report = rag.health_check()
    assert report["status"] == "degraded" and report["qdrant"].startswith("offline:")
