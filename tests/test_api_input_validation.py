"""API-boundary input validation, exercised through the real route handlers.

``ChatRequest.message`` and ``AgentRequest.message`` are declared ``min_length=1, max_length=5000`` in
``app/main.py``. FastAPI and Pydantic enforce that before a handler runs (HTTP 422). These tests pin that
behaviour and prove that a rejected request never reaches an agent, the conversation engine, retrieval, the
router, the orchestrator or an outbound model or embedding call.

The layers, in request order (docs/ENGINEERING_AUDIT.md, section 4.1):
  1. API schema: ``min_length=1`` and ``max_length=5000`` -> 422.
  2. Handler check: ``/chat`` also rejects a whitespace-only message with 400.
  3. Later layers decline a whitespace-only message that the schema allows: the orchestrator guardrail on
     ``/agents/{domain}`` and ``/team`` (400), the router on ``/auto`` (a clarification, no agent runs).

Exactly 5000 characters is exercised as a positive control, so the rejections above cannot pass vacuously.

Not covered, because not implemented: prompt-injection detection, delimiter neutralization, PII handling and
validation of free-text model output.

Nothing here needs or contacts a model: every agent ``run`` is a recorder, the conversation engine and retrieval
are recorders, and a real outbound HTTP request (which is how a model or embedding call is made) raises.
"""
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main
from app.security.hardening import install_security

MAX_MESSAGE_CHARS = 5000
ENV_KEYS = ("NEXUS_DEMO_MODE", "APP_ENV", "NEXUS_API_KEY", "NEXUS_API_KEYS", "NEXUS_REQUIRE_API_KEY")
AGENT_REQUEST_ENDPOINTS = ("/agents/enterprise", "/team", "/auto")
WHITESPACE_ONLY = " \t\n  "


class _NullActivitySink:
    def record(self, *args):
        return None


class Recorder:
    """What a request reached. Every list stays empty when a request is rejected at the API boundary."""

    def __init__(self):
        self.agent_runs = []              # domain of each agent.run() (a model call in production)
        self.understand = []              # conversation_engine.understand() (a model call in production)
        self.retrieval = []               # rag.search() (an embedding call in production)
        self.routing = []                 # router.route()
        self.orchestrator_run_agent = []  # nexus.run_agent(); also counts the calls run_team() makes
        self.orchestrator_run_team = []   # nexus.run_team()
        self.outbound_http = []           # a real outbound HTTP request: how a model call is made

    def reached(self):
        """Only the recorders that were hit; an empty dict means the request got nowhere."""
        return {name: value for name, value in vars(self).items() if value}


@pytest.fixture
def harness(monkeypatch):
    for name in ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    calls = Recorder()

    # Agents: run() is a recorder, the same technique as tests/test_agent_api_integration.py.
    for domain, agent in main.nexus.agents.items():
        def fake_run(message, *args, _domain=domain, **kwargs):
            calls.agent_runs.append(_domain)
            return {"agent": _domain, "response": f"{_domain} stub response", "status": "success"}
        monkeypatch.setattr(agent, "run", fake_run)
    monkeypatch.setattr(main.nexus, "activity_sink", _NullActivitySink())

    # Orchestrator and router entry points are spies around the real code, so the real guardrail and the
    # real routing still decide what happens and the tests can tell which layer rejected a request.
    real_run_agent, real_run_team, real_route = main.nexus.run_agent, main.nexus.run_team, main.router.route

    def spy_run_agent(domain, *args, **kwargs):
        calls.orchestrator_run_agent.append(domain)
        return real_run_agent(domain, *args, **kwargs)

    def spy_run_team(message, *args, **kwargs):
        calls.orchestrator_run_team.append(len(message))
        return real_run_team(message, *args, **kwargs)

    def spy_route(message):
        calls.routing.append(len(message))
        return real_route(message)

    monkeypatch.setattr(main.nexus, "run_agent", spy_run_agent)
    monkeypatch.setattr(main.nexus, "run_team", spy_run_team)
    monkeypatch.setattr(main.router, "route", spy_route)

    # /chat: the conversation engine (one LLM call in production) and retrieval (one embedding call).
    def fake_understand(message, history=None, user_profile=None, knowledge_context=None):
        calls.understand.append(len(message))
        return {"intent_type": "agent_task", "agent": "enterprise"}

    def fake_search(*args, query="", **kwargs):
        calls.retrieval.append(len(query))
        return []

    monkeypatch.setattr(main.conversation_engine, "understand", fake_understand)
    monkeypatch.setattr(main.rag, "search", fake_search)
    monkeypatch.setattr(main, "DEMO_MODE", True)  # /chat then touches no memory store or profile

    # Tripwire: models and embeddings are reached over real HTTP, so any real outbound request is a failure.
    # (The test client uses its own in-process transport, which this does not affect.)
    def forbid_network(self, request):
        calls.outbound_http.append(request.url.host)
        raise AssertionError("an outbound HTTP request was attempted")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbid_network)

    api = FastAPI()
    api.router.routes.extend(main.app.router.routes)  # the real production routes
    install_security(api)
    return SimpleNamespace(client=TestClient(api), calls=calls)


def _post(harness, path, message):
    return harness.client.post(path, json={"message": message})


def _assert_schema_rejection(response, error_type, limit_name):
    """The 422 comes from the request model on body.message, not from handler code."""
    assert response.status_code == 422
    (error,) = response.json()["detail"]
    assert error["loc"] == ["body", "message"]
    assert error["type"] == error_type
    expected_limit = 1 if limit_name == "min_length" else MAX_MESSAGE_CHARS
    assert error["ctx"][limit_name] == expected_limit


# ---- ChatRequest.message: min_length=1, max_length=5000 -----------------------------------------

def test_chat_request_rejects_an_empty_message(harness):
    response = _post(harness, "/chat", "")
    _assert_schema_rejection(response, "string_too_short", "min_length")
    assert harness.calls.reached() == {}


@pytest.mark.parametrize("length", [MAX_MESSAGE_CHARS + 1, 100_000])
def test_chat_request_rejects_a_message_over_5000_characters(harness, length):
    response = _post(harness, "/chat", "a" * length)
    _assert_schema_rejection(response, "string_too_long", "max_length")
    assert harness.calls.reached() == {}


# ---- AgentRequest.message: min_length=1, max_length=5000 (/agents/{domain}, /team, /auto) -------

@pytest.mark.parametrize("path", AGENT_REQUEST_ENDPOINTS)
def test_agent_request_rejects_an_empty_message(harness, path):
    response = _post(harness, path, "")
    _assert_schema_rejection(response, "string_too_short", "min_length")
    assert harness.calls.reached() == {}


@pytest.mark.parametrize("length", [MAX_MESSAGE_CHARS + 1, 100_000])
@pytest.mark.parametrize("path", AGENT_REQUEST_ENDPOINTS)
def test_agent_request_rejects_a_message_over_5000_characters(harness, path, length):
    response = _post(harness, path, "a" * length)
    _assert_schema_rejection(response, "string_too_long", "max_length")
    assert harness.calls.reached() == {}


# ---- invalid requests do not invoke an agent or a model ----------------------------------------

def test_no_schema_invalid_request_reaches_an_agent_or_a_model(harness):
    invalid_messages = ("", "a" * (MAX_MESSAGE_CHARS + 1))
    for path in ("/chat", *AGENT_REQUEST_ENDPOINTS):
        for message in invalid_messages:
            assert _post(harness, path, message).status_code == 422, (path, len(message))
    # a missing or non-text message is rejected by the same request models
    for path in ("/chat", *AGENT_REQUEST_ENDPOINTS):
        assert harness.client.post(path, json={}).status_code == 422, path
        assert harness.client.post(path, json={"message": 12345}).status_code == 422, path
    assert harness.calls.reached() == {}
    assert harness.calls.agent_runs == []
    assert harness.calls.understand == []
    assert harness.calls.outbound_http == []


def test_the_outbound_http_tripwire_is_live(harness):
    # Self-test of the fixture: if any code under test tried to reach a model over HTTP, it would be recorded
    # and raise. The request below never leaves the process; the tripwire stops it first.
    with pytest.raises(AssertionError, match="outbound HTTP request"):
        httpx.post("http://127.0.0.1:9/never-sent", json={})
    assert harness.calls.outbound_http == ["127.0.0.1"]


# ---- positive controls: exactly 5000 characters is accepted and does reach the stubs -------------
# These prove the recorders work, so the empty-recorder assertions above are not vacuous, and that the
# limit is exactly 5000 (a regression that tightened or loosened it would fail one side or the other).

def test_chat_request_accepts_a_message_of_exactly_5000_characters(harness):
    response = _post(harness, "/chat", "a" * MAX_MESSAGE_CHARS)
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert harness.calls.understand == [MAX_MESSAGE_CHARS]
    assert harness.calls.retrieval == [MAX_MESSAGE_CHARS]
    assert harness.calls.agent_runs == ["enterprise"]
    assert harness.calls.outbound_http == []


@pytest.mark.parametrize("path, message, expected_agents", [
    ("/agents/enterprise", "a" * MAX_MESSAGE_CHARS, ["enterprise"]),
    ("/team", "a" * MAX_MESSAGE_CHARS, ["enterprise", "personal", "cybersecurity"]),
    ("/auto", "security " + "a" * (MAX_MESSAGE_CHARS - len("security ")), ["cybersecurity"]),
])
def test_agent_request_accepts_a_message_of_exactly_5000_characters(harness, path, message, expected_agents):
    assert len(message) == MAX_MESSAGE_CHARS
    response = _post(harness, path, message)
    assert response.status_code == 200
    assert harness.calls.agent_runs == expected_agents
    assert harness.calls.outbound_http == []


# ---- whitespace-only: allowed by the schema, declined by a later layer ---------------------------
# min_length counts characters, not content, so "   " passes the request model. These tests record which
# later layer declines it, so documentation of the layers rests on a test and not on reading the code.

def test_chat_rejects_a_whitespace_only_message_in_the_handler(harness):
    response = _post(harness, "/chat", WHITESPACE_ONLY)
    assert response.status_code == 400  # the handler's strip() check, not the schema's 422
    assert response.json()["detail"] == "Message cannot be empty"
    assert harness.calls.reached() == {}


def test_agents_endpoint_rejects_a_whitespace_only_message_in_the_orchestrator_guardrail(harness):
    response = _post(harness, "/agents/enterprise", WHITESPACE_ONLY)
    assert response.status_code == 400
    assert response.json()["detail"] == "Message cannot be empty"
    assert harness.calls.reached() == {"orchestrator_run_agent": ["enterprise"]}  # rejected inside run_agent
    assert harness.calls.agent_runs == []


def test_team_endpoint_rejects_a_whitespace_only_message_in_the_orchestrator_guardrail(harness):
    response = _post(harness, "/team", WHITESPACE_ONLY)
    assert response.status_code == 400
    assert response.json()["detail"] == "Message cannot be empty"
    assert harness.calls.reached() == {"orchestrator_run_team": [len(WHITESPACE_ONLY)]}  # rejected before any agent
    assert harness.calls.agent_runs == []


def test_auto_endpoint_asks_for_clarification_on_a_whitespace_only_message(harness):
    response = _post(harness, "/auto", WHITESPACE_ONLY)
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "needs_clarification"
    assert body["routing"]["requires_clarification"] is True
    assert harness.calls.reached() == {"routing": [len(WHITESPACE_ONLY)]}  # no agent, no orchestrator call
