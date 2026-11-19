"""The /chat flow through the real routes: success, retrieved material that fits the agent guardrail, invalid
input rejected before any model call, authentication, and agent or model failures.

No model is reached: every agent run, the conversation engine and retrieval are recorders, and any real
outbound HTTP request (httpx or httpx2, which the OpenAI SDK uses) raises.
"""
import importlib
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main
from app.core.database import Base
from app.core.memory import NexusMemory
from app.security.guardrails import guardrails
from app.security.hardening import install_security

ALICE_KEY = "alice-key-0123456789-abcdefgh"
BOB_KEY = "bob-key-0123456789-abcdefghij"
OPERATOR_KEY = "operator-key-0123456789-abcdef"
ENV_KEYS = ("NEXUS_DEMO_MODE", "APP_ENV", "NEXUS_API_KEY", "NEXUS_API_KEYS", "NEXUS_REQUIRE_API_KEY",
            "NEXUS_KEYSTORE_ENABLED", "NEXUS_JWT_SECRETS")
LIMIT = guardrails.MAX_MESSAGE_LENGTH
HEADER = main.REFERENCE_MATERIAL_HEADER


class _NullSink:
    def record(self, *args):
        return None


def _http_libraries():
    libraries = [httpx]
    try:
        libraries.append(importlib.import_module("httpx2"))
    except ImportError:
        pass
    return libraries


@pytest.fixture
def harness(monkeypatch):
    for name in ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NEXUS_API_KEY", OPERATOR_KEY)
    monkeypatch.setenv("NEXUS_API_KEYS", f"{ALICE_KEY}=alice,{BOB_KEY}=bob")

    calls = SimpleNamespace(agent_runs=[], understand=[], retrieval=[], outbound=[])
    state = SimpleNamespace(intent="agent_task", agent="enterprise", answer="a stub answer",
                            retrieved=[], agent_raises=False, understand_raises=False)

    for domain, agent in main.nexus.agents.items():
        def fake_run(message, *args, _domain=domain, **kwargs):
            calls.agent_runs.append((_domain, message))
            if state.agent_raises:
                raise RuntimeError("model down")
            return {"agent": _domain, "response": state.answer, "status": "success"}
        monkeypatch.setattr(agent, "run", fake_run)
    monkeypatch.setattr(main.nexus, "activity_sink", _NullSink())

    def fake_understand(message, history=None, user_profile=None, knowledge_context=None):
        calls.understand.append(message)
        if state.understand_raises:
            raise RuntimeError("model down")
        if state.intent == "agent_task":
            return {"intent_type": "agent_task", "agent": state.agent}
        return {"intent_type": "conversation", "agent": None, "response": "chatty reply"}

    def fake_search(*args, query="", **kwargs):
        calls.retrieval.append(query)
        return state.retrieved

    monkeypatch.setattr(main.conversation_engine, "understand", fake_understand)
    monkeypatch.setattr(main.rag, "search", fake_search)
    monkeypatch.setattr(main.profile_learning, "extract_updates", lambda *a, **k: {})

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    store = NexusMemory(max_messages=20, history_limit=10, session_factory=factory, initialize=False)
    monkeypatch.setattr(main, "memory", store)
    monkeypatch.setattr(main, "DEMO_MODE", False)

    def forbid_network(self, request):
        calls.outbound.append(request.url.host)
        raise AssertionError("an outbound HTTP request was attempted")

    for library in _http_libraries():
        monkeypatch.setattr(library.HTTPTransport, "handle_request", forbid_network)

    api = FastAPI()
    api.router.routes.extend(main.app.router.routes)
    install_security(api)
    return SimpleNamespace(client=TestClient(api, raise_server_exceptions=False), calls=calls, state=state,
                           store=store)


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _chat(h, message, key=ALICE_KEY, **extra):
    return h.client.post("/chat", json={"message": message, **extra}, headers=_auth(key) if key else {})


def _snippets(total_chars, count=5):
    return [{"source": f"doc{i}.txt", "text": "w" * (total_chars // count)} for i in range(count)]


# ---- success -----------------------------------------------------------------------------------------

def test_chat_runs_the_selected_agent_and_stores_both_turns(harness):
    response = _chat(harness, "Summarise our roadmap.", session_id="s1")
    body = response.json()
    assert response.status_code == 200
    assert (body["status"], body["agent"], body["response"]) == ("completed", "enterprise", "a stub answer")
    assert [d for d, _ in harness.calls.agent_runs] == ["enterprise"]
    history = harness.store.get_history(main.scope_session_id(main.Principal("alice", False), "s1"))
    assert [(m["role"], m["content"]) for m in history] == [("user", "Summarise our roadmap."),
                                                            ("assistant", "a stub answer")]
    assert harness.calls.outbound == []


@pytest.mark.parametrize("agent", ["enterprise", "personal", "cybersecurity"])
def test_the_agent_chosen_by_the_conversation_engine_is_the_one_invoked(harness, agent):
    harness.state.agent = agent
    body = _chat(harness, "Please help with this.").json()
    assert body["agent"] == agent and [d for d, _ in harness.calls.agent_runs] == [agent]


def test_a_conversational_intent_runs_no_agent(harness):
    harness.state.intent = "conversation"
    body = _chat(harness, "Hello there").json()
    assert (body["status"], body["agent"], body["response"]) == ("completed", None, "chatty reply")
    assert harness.calls.agent_runs == []


# ---- retrieved material fits the agent guardrail ------------------------------------------------------

def test_small_retrieved_material_is_appended_to_the_agent_message(harness):
    harness.state.retrieved = _snippets(1000)
    assert _chat(harness, "What does the handbook say?").json()["status"] == "completed"
    (_, message), = harness.calls.agent_runs
    assert message.startswith("What does the handbook say?" + HEADER)
    assert "doc0.txt" in message and len(message) <= LIMIT


def test_a_full_size_retrieved_block_no_longer_fails_the_request(harness):
    # Regression: 6,000 characters of retrieved text plus the message exceeded the 5,000-character guardrail,
    # and /chat answered status "failed", "Message exceeds maximum length" without running the agent.
    harness.state.retrieved = _snippets(12_000)
    body = _chat(harness, "What does the handbook say?").json()
    assert body["status"] == "completed" and body["response"] == "a stub answer"
    (_, message), = harness.calls.agent_runs
    assert len(message) <= LIMIT and HEADER in message


def test_a_maximum_length_message_with_retrieved_material_still_reaches_the_agent(harness):
    harness.state.retrieved = _snippets(1000)
    message = "q" * LIMIT
    assert _chat(harness, message).json()["status"] == "completed"
    (_, composed), = harness.calls.agent_runs
    assert composed == message  # no room for reference material: it is left out, the request is not failed


@pytest.mark.parametrize("message_len", [0, 100, 4000, LIMIT - len(HEADER) - 200, LIMIT - len(HEADER) - 199, LIMIT])
def test_the_composed_message_never_exceeds_the_guardrail(message_len):
    message = "m" * message_len
    composed, bounded = main.compose_agent_message(message, _snippets(12_000))
    assert len(composed) <= LIMIT
    assert composed.startswith(message)
    if bounded:
        assert composed == message + HEADER + bounded and len(bounded) >= main.MIN_USEFUL_KNOWLEDGE_CHARS
    else:
        assert composed == message


def test_the_retrieved_block_is_still_capped_at_its_own_limit():
    composed, bounded = main.compose_agent_message("short", _snippets(30_000, count=10))
    assert len(bounded) <= main.MAX_KNOWLEDGE_CONTEXT_CHARS
    assert LIMIT - len(composed) >= 0


# ---- invalid input is rejected before any model, embedding or storage call -----------------------------

@pytest.mark.parametrize("message, status", [("", 422), ("a" * (LIMIT + 1), 422), ("   \n\t ", 400)])
def test_invalid_messages_reach_nothing(harness, message, status):
    assert _chat(harness, message).status_code == status
    assert harness.calls.understand == [] and harness.calls.retrieval == [] and harness.calls.agent_runs == []
    assert harness.store.get_history("s1") == []


@pytest.mark.parametrize("field, value", [("session_id", "a\tb"), ("session_id", " "), ("user_id", "x\x00y")])
def test_control_characters_in_identifiers_are_a_400_not_a_500(harness, field, value):
    # Regression: the memory layer refused these and the handler turned that into HTTP 500.
    response = _chat(harness, "Summarise our roadmap.", key=OPERATOR_KEY, **{field: value})
    assert response.status_code == 400
    assert harness.calls.understand == [] and harness.calls.agent_runs == []


def test_a_very_long_answer_is_returned_in_full_and_stored_bounded(harness):
    # Regression: an answer over 12,000 characters made the memory write raise after the agent had run.
    harness.state.answer = "y" * 20_000
    response = _chat(harness, "Write a lot.", session_id="s2")
    assert response.status_code == 200 and len(response.json()["response"]) == 20_000
    history = harness.store.get_history(main.scope_session_id(main.Principal("alice", False), "s2"))
    assert len(history[-1]["content"]) == main.MAX_STORED_RESPONSE_CHARS


# ---- authentication and isolation are unchanged ---------------------------------------------------------

def test_missing_and_wrong_credentials_get_401_before_anything_runs(harness):
    assert _chat(harness, "hello", key=None).status_code == 401
    assert _chat(harness, "hello", key="wrong-key-0123456789-abcdefgh").status_code == 401
    assert harness.calls.understand == [] and harness.calls.retrieval == []


def test_a_scoped_user_cannot_chat_as_another_user(harness):
    assert _chat(harness, "hello", key=BOB_KEY, user_id="alice").status_code == 403
    assert harness.calls.understand == []


# ---- agent and model failures ----------------------------------------------------------------------------

def test_an_agent_exception_is_a_failed_status_not_a_500(harness):
    harness.state.agent_raises = True
    response = _chat(harness, "Summarise our roadmap.")
    body = response.json()
    assert response.status_code == 200 and body["status"] == "failed"
    assert "Check server logs" in body["response"] and "model down" not in response.text


def test_an_agent_error_result_is_a_failed_status(harness, monkeypatch):
    monkeypatch.setattr(main.nexus.agents["enterprise"], "run",
                        lambda message, *a, **k: {"error": "Enterprise agent could not complete the request.",
                                                  "status": "failed"})
    body = _chat(harness, "Summarise our roadmap.").json()
    assert body["status"] == "failed" and "could not complete" in body["response"]


def test_a_conversation_engine_exception_is_a_500_that_leaks_nothing(harness):
    harness.state.understand_raises = True
    response = _chat(harness, "Summarise our roadmap.")
    assert response.status_code == 500 and "model down" not in response.text
    assert harness.calls.agent_runs == []
