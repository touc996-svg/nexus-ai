"""Role-based authorization through the real routes, authentication middleware and dependencies.

Two roles exist (app/security/identity.py): ``admin`` (the operator key NEXUS_API_KEY, and the
no-authentication development/demo mode) and ``user`` (a scoped key from NEXUS_API_KEYS). System-level routes
(AEGIS-X, activity history, system status) are admin-only. User-data routes stay open to any authenticated
caller and are confined to the caller's own data by their handlers (tests/test_tenant_isolation.py).

``test_every_route_is_classified`` is the privilege-boundary guard: a route added later without an explicit
decision fails it.
"""
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app import main
from app.api import observability
from app.security.hardening import install_security
from app.security.identity import (
    OPERATOR,
    ROLE_ADMIN,
    ROLE_USER,
    UNAUTHENTICATED,
    Principal,
    require_admin,
)

ALICE_KEY = "alice-key-0123456789-abcdefgh"
BOB_KEY = "bob-key-0123456789-abcdefghij"
OPERATOR_KEY = "operator-key-0123456789-abcdef"
ENV_KEYS = ("NEXUS_DEMO_MODE", "APP_ENV", "NEXUS_API_KEY", "NEXUS_API_KEYS", "NEXUS_REQUIRE_API_KEY")

# Every (method, path) the production app serves, each in exactly one set.
PUBLIC = {("GET", "/"), ("GET", "/health/live"), ("GET", "/health/ready")}  # middleware PUBLIC_PATHS

ADMIN = {  # operator credentials only
    ("GET", "/activity"),
    ("GET", "/activity/summary"),
    ("GET", "/dashboard/activity"),
    ("GET", "/decision-engine/status"),
    ("GET", "/memory/status"),
    ("GET", "/integrations/aegis-x/status"),
    ("POST", "/integrations/aegis-x/findings/query"),
    ("POST", "/integrations/aegis-x/incidents/summary"),
    ("POST", "/integrations/aegis-x/evidence/correlate"),
    ("POST", "/integrations/aegis-x/risk/explain"),
    ("POST", "/integrations/aegis-x/recommendations/text"),
    ("GET", "/system/llm-health"),
    ("GET", "/system/rag-health"),
    ("GET", "/system/memory-health"),
    ("GET", "/system/evaluation/latest"),
}

AUTHENTICATED = {  # any valid credential; user data is bound to the caller inside the handler
    ("GET", "/agents/{domain}"),
    ("POST", "/agents/{domain}"),
    ("POST", "/auto"),
    ("POST", "/team"),
    ("POST", "/chat"),
    ("GET", "/memory/{session_id}"),
    ("POST", "/memory/{session_id}"),
    ("DELETE", "/memory/{session_id}"),
    ("GET", "/profile/{user_id}"),
    ("PUT", "/profile/{user_id}"),
    ("DELETE", "/profile/{user_id}"),
    ("POST", "/knowledge/ingest"),
    ("POST", "/knowledge/search"),
    ("GET", "/knowledge/sources"),
    ("DELETE", "/knowledge/sources/{source_id}"),
}

BODIES = {  # minimal valid bodies, so a 403 cannot be mistaken for a validation failure
    ("POST", "/integrations/aegis-x/findings/query"): {},
    ("POST", "/integrations/aegis-x/incidents/summary"): {"incident_id": "inc-1"},
    ("POST", "/integrations/aegis-x/evidence/correlate"): {"finding_ids": ["f-1"]},
    ("POST", "/integrations/aegis-x/risk/explain"): {"finding_id": "f-1"},
    ("POST", "/integrations/aegis-x/recommendations/text"): {"finding_ids": ["f-1"]},
}


class _NullActivitySink:
    def record(self, *args):
        return None


def _build_client(monkeypatch, *, with_keys):
    for name in ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    if with_keys:
        monkeypatch.setenv("NEXUS_API_KEY", OPERATOR_KEY)
        monkeypatch.setenv("NEXUS_API_KEYS", f"{ALICE_KEY}=alice,{BOB_KEY}=bob")

    # The admin-only handlers read the database, Ollama or a vector store; replace those reads.
    monkeypatch.setattr(main.activity_logger, "get_recent", lambda limit=50: [])
    monkeypatch.setattr(main.activity_logger, "get_summary", lambda: {"total_runs": 0})
    monkeypatch.setattr(observability, "get_ollama_health", lambda *args, **kwargs: {"status": "stub"})
    monkeypatch.setattr(observability.rag, "health_check", lambda: {"status": "stub"})
    monkeypatch.setattr(observability.memory, "health_check", lambda: {"status": "stub"})
    # Agents never reach a model.
    for domain, agent in main.nexus.agents.items():
        monkeypatch.setattr(agent, "run", lambda message, *a, _d=domain, **k: {"agent": _d, "response": "stub", "status": "success"})
    monkeypatch.setattr(main.nexus, "activity_sink", _NullActivitySink())

    api = FastAPI()
    api.router.routes.extend(main.app.router.routes)  # the real production routes
    install_security(api)
    return TestClient(api)


@pytest.fixture
def client(monkeypatch):
    return _build_client(monkeypatch, with_keys=True)


@pytest.fixture
def open_client(monkeypatch):
    return _build_client(monkeypatch, with_keys=False)


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _call(client, method, path, key=None, **extra):
    headers = {**(_auth(key) if key else {}), **extra.pop("headers", {})}
    json = extra.pop("json", BODIES.get((method, path)))
    return client.request(method, path, headers=headers, json=json)


# ---- the privilege boundary is explicit ---------------------------------------------------------

def _served_operations():
    paths = main.app.openapi()["paths"]
    return {(method.upper(), path) for path, item in paths.items() for method in item}


def test_every_route_is_classified():
    served = _served_operations()
    classified = PUBLIC | ADMIN | AUTHENTICATED
    assert served - classified == set(), "route(s) with no authorization decision: add them to PUBLIC, ADMIN or AUTHENTICATED"
    assert classified - served == set(), "classified route(s) that the app no longer serves"


def test_classification_sets_do_not_overlap():
    assert not (PUBLIC & ADMIN) and not (PUBLIC & AUTHENTICATED) and not (ADMIN & AUTHENTICATED)


def test_aegis_x_exposes_only_read_verbs():
    # Checked through the OpenAPI schema: on the pinned FastAPI the routes sit inside included-router objects
    # that a loop over app.routes does not see.
    aegis = {(method, path) for method, path in _served_operations() if path.startswith("/integrations/aegis-x")}
    assert len(aegis) == 6
    assert {method for method, _ in aegis} <= {"GET", "POST"}


def test_principal_roles():
    assert OPERATOR.role == UNAUTHENTICATED.role == ROLE_ADMIN
    assert Principal(user_id="alice", is_admin=False).role == ROLE_USER


# ---- admin-only routes: 401 without a credential, 403 for a scoped user, success for the operator ----

@pytest.mark.parametrize("method, path", sorted(ADMIN))
def test_admin_routes_refuse_a_scoped_user(client, method, path):
    for key in (ALICE_KEY, BOB_KEY):
        response = _call(client, method, path, key)
        assert response.status_code == 403, (path, key[:5])
        assert response.json() == {"detail": "Administrator credentials required."}


@pytest.mark.parametrize("method, path", sorted(ADMIN))
def test_admin_routes_require_a_credential(client, method, path):
    assert _call(client, method, path).status_code == 401
    assert _call(client, method, path, "not-a-real-key-0123456789ab").status_code == 401


@pytest.mark.parametrize("method, path", sorted(ADMIN))
def test_admin_routes_accept_the_operator(client, method, path):
    assert _call(client, method, path, OPERATOR_KEY).status_code == 200


def test_authorization_is_decided_before_the_request_body_is_validated(client):
    # A scoped user learns nothing about the request schema of an admin route: 403, not 422.
    response = client.post("/integrations/aegis-x/findings/query", json={"cloud": "oracle"}, headers=_auth(ALICE_KEY))
    assert response.status_code == 403


def test_a_role_cannot_be_claimed_through_request_fields(client):
    forged = {"X-Role": "admin", "X-Admin": "true", "X-User-Id": "operator", "X-Forwarded-User": "admin"}
    for method, path in sorted(ADMIN):
        response = _call(client, method, path, ALICE_KEY, headers=forged, json={**(BODIES.get((method, path)) or {}), "role": "admin", "is_admin": True})
        assert response.status_code == 403, path
    assert client.get("/activity?role=admin&is_admin=true", headers=_auth(ALICE_KEY)).status_code == 403


# ---- the gate does not over-restrict -------------------------------------------------------------

def test_a_scoped_user_keeps_access_to_the_agent_routes(client):
    assert client.get("/agents/enterprise", headers=_auth(ALICE_KEY)).status_code == 200
    assert client.post("/agents/enterprise", json={"message": "hello"}, headers=_auth(ALICE_KEY)).status_code == 200
    assert client.post("/team", json={"message": "review operations"}, headers=_auth(ALICE_KEY)).status_code == 200
    assert client.post("/auto", json={"message": "security review"}, headers=_auth(ALICE_KEY)).status_code == 200


def test_public_routes_need_no_credential(client):
    assert client.get("/health/live").status_code == 200


# ---- authentication switched off: unchanged (no identity, no enforcement) --------------------------

def test_with_authentication_off_admin_routes_stay_open(open_client):
    # Local development and the public demo have no identity; the demo additionally blocks activity history in
    # the middleware (tests/test_security_hardening.py). This pins that the role gate did not change that.
    for method, path in sorted(ADMIN):
        assert _call(open_client, method, path).status_code == 200, path


# ---- fail closed ---------------------------------------------------------------------------------

def test_require_admin_fails_closed_when_nothing_authenticated_the_request():
    # Authentication is required but the middleware is not installed (or was bypassed): no principal exists.
    app = FastAPI()
    app.state.security_settings = SimpleNamespace(require_api_key=True, demo_mode=False)

    @app.get("/system-only", dependencies=[Depends(require_admin)])
    def system_only():
        return {"reached": True}

    response = TestClient(app).get("/system-only")
    assert response.status_code == 401
    assert "reached" not in response.text
