"""C. Authorization and privilege escalation over HTTP. Administrator routes accept only administrator credentials."""

import pytest

from tests.security.helpers import ALICE_KEY, BOB_KEY, OPERATOR_KEY, auth, make_jwt

ADMIN_ROUTES = [
    ("GET", "/activity"), ("GET", "/activity/summary"), ("GET", "/dashboard/activity"),
    ("GET", "/decision-engine/status"), ("GET", "/memory/status"), ("GET", "/system/metrics"),
    ("GET", "/system/llm-health"), ("GET", "/system/rag-health"), ("GET", "/system/memory-health"),
    ("GET", "/system/evaluation/latest"), ("GET", "/integrations/aegis-x/status"),
    ("GET", "/integrations/aegis-x/incidents"), ("POST", "/integrations/aegis-x/findings/query"),
    ("POST", "/integrations/aegis-x/incidents/summary"), ("POST", "/integrations/aegis-x/evidence/correlate"),
    ("POST", "/integrations/aegis-x/risk/explain"), ("POST", "/integrations/aegis-x/recommendations/text"),
]


def call(client, method, path, headers=None, **extra):
    return client.request(method, path, headers=headers or {}, json={} if method == "POST" else None, **extra)


@pytest.mark.parametrize("method,path", ADMIN_ROUTES)
def test_environment_user_keys_cannot_use_admin_routes(app_ctx, method, path):
    assert call(app_ctx.client, method, path, auth(ALICE_KEY)).status_code == 403


@pytest.mark.parametrize("method,path", ADMIN_ROUTES)
def test_stored_user_keys_and_user_tokens_cannot_use_admin_routes(make_app, method, path):
    ctx = make_app(jwt=True)
    stored = ctx.store.issue("carol", ttl_days=7)
    for credential in (stored.key, make_jwt({"sub": "carol"})):
        assert call(ctx.client, method, path, auth(credential)).status_code == 403


@pytest.mark.parametrize("method,path", ADMIN_ROUTES)
def test_admin_routes_require_authentication_at_all(app_ctx, method, path):
    assert call(app_ctx.client, method, path).status_code == 401


def test_the_operator_key_does_reach_admin_routes(app_ctx):
    assert app_ctx.client.get("/system/metrics", headers=auth(OPERATOR_KEY)).status_code == 200


def test_a_stored_admin_key_is_administrator_but_a_user_key_cannot_mint_one(app_ctx):
    admin = app_ctx.store.issue("ops", role="admin", ttl_days=7)
    assert app_ctx.client.get("/system/metrics", headers=auth(admin.key)).status_code == 200
    # there is no HTTP route that issues keys, so a user cannot create an administrator credential over the API
    routes = {route.path for route in app_ctx.api.routes if hasattr(route, "path")}
    assert not any("key" in path.lower() and path.lower().startswith(("/admin", "/keys", "/auth")) for path in routes)


FORGED_HEADERS = [
    {"X-Admin": "true"}, {"X-Role": "admin"}, {"X-User-Role": "administrator"}, {"X-User-Id": "operator"},
    {"X-Forwarded-User": "admin"}, {"X-Authenticated-User": "admin"}, {"X-Is-Admin": "1"}, {"Remote-User": "admin"},
    {"X-HTTP-Method-Override": "GET"}, {"X-Original-URL": "/system/metrics"}, {"X-Rewrite-URL": "/system/metrics"},
]


@pytest.mark.parametrize("extra", FORGED_HEADERS)
def test_forged_identity_and_role_headers_are_ignored(app_ctx, extra):
    c = app_ctx.client
    assert c.get("/system/metrics", headers={**auth(ALICE_KEY), **extra}).status_code == 403
    assert c.get("/system/metrics", headers=extra).status_code == 401
    assert c.get("/profile/bob", headers={**auth(ALICE_KEY), **extra}).status_code == 403


@pytest.mark.parametrize("params", [{"admin": "true"}, {"role": "admin"}, {"is_admin": "1"}, {"user": "operator"},
                                    {"principal": "admin"}, {"debug": "1"}])
def test_forged_role_query_parameters_are_ignored(app_ctx, params):
    assert app_ctx.client.get("/system/metrics", params=params, headers=auth(ALICE_KEY)).status_code == 403


@pytest.mark.parametrize("path", ["/chat", "/auto", "/team", "/agents/enterprise"])
def test_role_and_identity_fields_in_json_bodies_cannot_override_the_principal(app_ctx, path):
    body = {"message": "hello", "role": "admin", "is_admin": True, "principal": {"is_admin": True, "user_id": "operator"},
            "user_id": "bob", "admin": True}
    response = app_ctx.client.post(path, json=body, headers=auth(ALICE_KEY))
    assert response.status_code in {403, 422, 200}
    if path == "/chat":
        assert response.status_code == 403  # user_id=bob is refused for alice, whatever else is in the body
    assert "operator" not in response.text


def test_forged_fields_in_a_profile_update_do_not_escalate_or_cross_tenants(app_ctx):
    c = app_ctx.client
    response = c.put("/profile/alice", json={"goals": ["g"], "is_admin": True, "role": "admin", "user_id": "bob"},
                     headers=auth(ALICE_KEY))
    assert response.status_code == 200
    assert c.get("/system/metrics", headers=auth(ALICE_KEY)).status_code == 403
    assert c.get("/profile/bob", headers=auth(OPERATOR_KEY)).status_code == 404


@pytest.mark.parametrize("path", ["//system/metrics", "/system/metrics/", "/SYSTEM/metrics", "/system/./metrics",
                                  "/%73ystem/metrics", "/health/live/../../system/metrics", "/system%2fmetrics",
                                  "/health/live/", "/health/live%2f..%2fsystem/metrics", "/ /system/metrics"])
def test_path_tricks_do_not_reach_admin_data_without_credentials(app_ctx, path):
    response = app_ctx.client.get(path)
    assert response.status_code in {307, 308, 400, 401, 404, 405}, path
    assert "counters" not in response.text


def test_http_method_override_headers_do_not_change_the_method(app_ctx):
    c = app_ctx.client
    c.post("/memory/s", json={"role": "user", "content": "keep"}, headers=auth(ALICE_KEY))
    c.post("/memory/s", json={"role": "user", "content": "x"}, headers={**auth(ALICE_KEY), "X-HTTP-Method-Override": "DELETE"})
    assert len(c.get("/memory/s", headers=auth(ALICE_KEY)).json()["history"]) == 2


def test_cors_does_not_grant_cross_origin_access_by_default(app_ctx):
    response = app_ctx.client.options("/chat", headers={"Origin": "https://evil.example",
                                                         "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in {k.lower() for k in response.headers}
    simple = app_ctx.client.get("/agents/enterprise", headers={**auth(ALICE_KEY), "Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in simple.headers}


def test_cors_allows_only_the_configured_origin(make_app):
    ctx = make_app(env={"NEXUS_CORS_ORIGINS": "https://app.example"})
    good = ctx.client.get("/agents/enterprise", headers={**auth(ALICE_KEY), "Origin": "https://app.example"})
    bad = ctx.client.get("/agents/enterprise", headers={**auth(ALICE_KEY), "Origin": "https://evil.example"})
    assert good.headers.get("access-control-allow-origin") == "https://app.example"
    assert "access-control-allow-origin" not in {k.lower() for k in bad.headers}
    assert good.headers.get("access-control-allow-origin") != "*"


def test_security_headers_are_present_on_authenticated_and_rejected_responses(app_ctx):
    for response in (app_ctx.client.get("/agents/enterprise", headers=auth(BOB_KEY)), app_ctx.client.get("/profile/alice")):
        assert response.headers["x-content-type-options"] == "nosniff" and response.headers["x-frame-options"] == "DENY"
        assert response.headers.get("x-request-id")
