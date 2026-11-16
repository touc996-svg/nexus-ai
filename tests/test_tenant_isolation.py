"""Tenant isolation through the real route handlers, authentication middleware and stores.

A fresh FastAPI app is assembled from the production routes, so these tests exercise the
actual request path (API key -> principal -> handler -> memory / vector store).
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main
from app.api import knowledge
from app.core.database import Base
from app.core.memory import NexusMemory
from app.security.hardening import install_security, parse_user_keys
from app.services.rag import NexusRAG

ALICE_KEY = "alice-key-0123456789-abcdefgh"
BOB_KEY = "bob-key-0123456789-abcdefghij"
OPERATOR_KEY = "operator-key-0123456789-abcdef"
ENV_KEYS = ("NEXUS_DEMO_MODE", "APP_ENV", "NEXUS_API_KEY", "NEXUS_API_KEYS", "NEXUS_REQUIRE_API_KEY")


def auth(key):
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def client(monkeypatch):
    for name in ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NEXUS_API_KEY", OPERATOR_KEY)
    monkeypatch.setenv("NEXUS_API_KEYS", f"{ALICE_KEY}=alice,{BOB_KEY}=bob")

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    store = NexusMemory(max_messages=20, history_limit=10, session_factory=factory, initialize=False)

    monkeypatch.setenv("NEXUS_EMBEDDING_DIMENSION", "768")
    rag = NexusRAG(client=QdrantClient(location=":memory:"), demo_mode=False, score_threshold=0.0)
    monkeypatch.setattr(rag, "embed", lambda _text: [1.0] + [0.0] * 767)

    monkeypatch.setattr(main, "DEMO_MODE", False)
    monkeypatch.setattr(main, "memory", store)
    monkeypatch.setattr(main, "rag", rag)
    monkeypatch.setattr(knowledge, "rag", rag)

    api = FastAPI()
    api.router.routes.extend(main.app.router.routes)  # the real production routes
    install_security(api)
    test_client = TestClient(api)
    test_client.store, test_client.rag = store, rag
    return test_client


# ---- memory ---------------------------------------------------------------------------------

def test_users_with_the_same_session_id_do_not_see_each_other(client):
    client.post("/memory/shared-id", json={"role": "user", "content": "alice secret"}, headers=auth(ALICE_KEY))
    client.post("/memory/shared-id", json={"role": "user", "content": "bob note"}, headers=auth(BOB_KEY))
    alice = client.get("/memory/shared-id", headers=auth(ALICE_KEY)).json()
    bob = client.get("/memory/shared-id", headers=auth(BOB_KEY)).json()
    assert [m["content"] for m in alice["history"]] == ["alice secret"]
    assert [m["content"] for m in bob["history"]] == ["bob note"]
    assert alice["session_id"] == "shared-id"  # clients never see the internal namespace


def test_a_user_cannot_delete_another_users_session(client):
    client.post("/memory/s1", json={"role": "user", "content": "keep me"}, headers=auth(ALICE_KEY))
    deleted = client.delete("/memory/s1", headers=auth(BOB_KEY)).json()
    assert deleted["deleted"] is False
    still = client.get("/memory/s1", headers=auth(ALICE_KEY)).json()
    assert [m["content"] for m in still["history"]] == ["keep me"]


def test_operator_still_reaches_unscoped_sessions(client):
    client.store.add_message("legacy-session", "user", "old data")
    got = client.get("/memory/legacy-session", headers=auth(OPERATOR_KEY)).json()
    assert [m["content"] for m in got["history"]] == ["old data"]
    # ... and scoped users cannot reach that legacy session by guessing its name
    assert client.get("/memory/legacy-session", headers=auth(ALICE_KEY)).json()["history"] == []


# ---- profiles -------------------------------------------------------------------------------

def test_profile_access_is_limited_to_the_owner(client):
    assert client.put("/profile/alice", json={"goals": ["learn"]}, headers=auth(ALICE_KEY)).status_code == 200
    assert client.get("/profile/alice", headers=auth(ALICE_KEY)).status_code == 200
    for method, kwargs in (("get", {}), ("put", {"json": {"goals": ["x"]}}), ("delete", {})):
        response = getattr(client, method)("/profile/alice", headers=auth(BOB_KEY), **kwargs)
        assert response.status_code == 403, method
    assert client.get("/profile/alice", headers=auth(ALICE_KEY)).json()["profile"]["goals"] == ["learn"]


# ---- knowledge ------------------------------------------------------------------------------

def _ingest(client, key, text, user_id=None):
    data = {"user_id": user_id} if user_id else {}
    return client.post("/knowledge/ingest", files={"file": ("note.txt", text.encode(), "text/plain")},
                       data=data, headers=auth(key))


def test_ingest_search_and_list_are_confined_to_the_caller(client):
    assert _ingest(client, ALICE_KEY, "alice private roadmap document").status_code == 200
    assert _ingest(client, BOB_KEY, "bob budget document").status_code == 200

    alice = client.post("/knowledge/search", json={"query": "document"}, headers=auth(ALICE_KEY)).json()
    bob = client.post("/knowledge/search", json={"query": "document"}, headers=auth(BOB_KEY)).json()
    assert alice["results_count"] == 1 and "alice" in alice["results"][0]["text"]
    assert bob["results_count"] == 1 and "bob" in bob["results"][0]["text"]
    assert len(client.get("/knowledge/sources", headers=auth(ALICE_KEY)).json()["sources"]) == 1


def test_naming_another_user_is_rejected_everywhere(client):
    _ingest(client, ALICE_KEY, "alice private roadmap document")
    assert client.post("/knowledge/search", json={"query": "document", "user_id": "alice"},
                       headers=auth(BOB_KEY)).status_code == 403
    assert client.get("/knowledge/sources", params={"user_id": "alice"}, headers=auth(BOB_KEY)).status_code == 403
    assert client.delete("/knowledge/sources/anything", params={"user_id": "alice"},
                         headers=auth(BOB_KEY)).status_code == 403
    assert _ingest(client, BOB_KEY, "planted document", user_id="alice").status_code == 403
    # nothing was planted in Alice's index
    alice = client.post("/knowledge/search", json={"query": "document"}, headers=auth(ALICE_KEY)).json()
    assert alice["results_count"] == 1 and "planted" not in alice["results"][0]["text"]


def test_a_user_can_name_themselves(client):
    ok = client.post("/knowledge/search", json={"query": "x", "user_id": "alice"}, headers=auth(ALICE_KEY))
    assert ok.status_code == 200


def test_scoped_users_cannot_read_the_shared_system_pool(client):
    _ingest(client, OPERATOR_KEY, "company wide policy document")  # operator default user is "system"
    system = client.post("/knowledge/search", json={"query": "policy"}, headers=auth(OPERATOR_KEY)).json()
    assert system["results_count"] == 1
    assert client.post("/knowledge/search", json={"query": "policy", "user_id": "system"},
                       headers=auth(ALICE_KEY)).status_code == 403
    assert client.post("/knowledge/search", json={"query": "policy"},
                       headers=auth(ALICE_KEY)).json()["results_count"] == 0


# ---- chat -----------------------------------------------------------------------------------

def test_chat_acts_as_the_authenticated_user_and_rejects_impersonation(client, monkeypatch):
    seen = {}

    def fake_search(query, user_id="system", limit=5, **kwargs):
        seen["rag_user"] = user_id
        return []

    monkeypatch.setattr(main.rag, "search", fake_search)
    monkeypatch.setattr(main.nexus, "run_agent", lambda *a, **k: {"response": "ok", "status": "success"})

    impersonate = client.post("/chat", json={"message": "hi", "user_id": "alice"}, headers=auth(BOB_KEY))
    assert impersonate.status_code == 403
    assert "rag_user" not in seen

    own = client.post("/chat", json={"message": "hi", "session_id": "c1"}, headers=auth(ALICE_KEY))
    assert own.status_code == 200, own.text
    assert seen["rag_user"] == "alice"  # no user_id sent: the authenticated identity is used
    stored = client.store.get_history("c1")
    assert stored == []  # the conversation is not stored under the bare client-chosen session id


# ---- authentication behaviour ---------------------------------------------------------------

def test_unauthenticated_and_wrong_keys_are_rejected(client):
    assert client.get("/memory/x").status_code == 401
    assert client.get("/memory/x", headers=auth("not-a-real-key-0123456789ab")).status_code == 401


def test_operator_key_is_unrestricted_as_before(client):
    assert client.put("/profile/alice", json={"goals": ["a"]}, headers=auth(OPERATOR_KEY)).status_code == 200
    assert client.get("/profile/alice", headers=auth(OPERATOR_KEY)).status_code == 200


# ---- key configuration is validated -----------------------------------------------------------

@pytest.mark.parametrize("raw, message", [
    ("shortkey=alice", "at least"),
    (f"{ALICE_KEY}", "key=user_id"),
    (f"{ALICE_KEY}=", "key=user_id"),
    (f"{ALICE_KEY}=alice,{ALICE_KEY}=bob", "duplicated"),
    (f"{ALICE_KEY}=has space", "printable"),
])
def test_malformed_user_keys_are_refused(raw, message):
    with pytest.raises(ValueError, match=message):
        parse_user_keys(raw)


def test_user_key_may_not_reuse_the_operator_key():
    with pytest.raises(ValueError, match="must not reuse"):
        parse_user_keys(f"{OPERATOR_KEY}=alice", operator_key=OPERATOR_KEY)


def test_missing_principal_fails_closed_when_auth_is_required(monkeypatch):
    """A handler reached without a principal must be refused when authentication is required."""
    from starlette.requests import Request

    from app.security.identity import get_principal

    class Settings:
        require_api_key, demo_mode = True, False

    class State:
        security_settings = Settings()

    class Application:
        state = State()

    scope = {"type": "http", "app": Application(), "headers": [], "method": "GET", "path": "/"}
    with pytest.raises(Exception) as caught:
        get_principal(Request(scope))
    assert getattr(caught.value, "status_code", None) == 401
