"""B. Authentication and JWT over HTTP: every credential failure must fail closed (401) and reach no handler or model."""

import time

import pytest

from tests.security.helpers import (
    ALICE_KEY,
    JWT_ISSUER,
    JWT_SECRET,
    OPERATOR_KEY,
    _b64,
    auth,
    make_jwt,
)

PROTECTED = [("GET", "/profile/alice"), ("POST", "/chat"), ("GET", "/memory/s"), ("GET", "/knowledge/sources"),
             ("POST", "/knowledge/search"), ("GET", "/agents/enterprise"), ("GET", "/system/metrics"),
             ("GET", "/openapi.json"), ("GET", "/docs")]


def call(client, method, path, headers=None):
    body = {"json": {"message": "x", "query": "x"}} if method == "POST" else {}
    return client.request(method, path, headers=headers or {}, **body)


@pytest.mark.parametrize("method,path", PROTECTED)
def test_missing_authentication_fails_closed_on_every_protected_route(app_ctx, method, path):
    response = call(app_ctx.client, method, path)
    assert response.status_code == 401 and response.headers.get("www-authenticate") == "Bearer"
    assert app_ctx.llm.calls == []


@pytest.mark.parametrize("headers", [
    {"Authorization": "Bearer "}, {"Authorization": "Bearer"}, {"Authorization": "Basic YWxpY2U6cGFzcw=="},
    {"Authorization": f"Token {ALICE_KEY}"}, {"X-API-Key": ""}, {"X-API-Key": "   "}, {"X-API-Key": "wrong-key"},
    {"Authorization": "Bearer " + "A" * 100_000},
])
def test_malformed_or_wrong_credentials_are_rejected(app_ctx, headers):
    assert app_ctx.client.get("/profile/alice", headers=headers).status_code == 401


def test_credentials_in_the_query_string_or_cookies_are_not_accepted(app_ctx):
    c = app_ctx.client
    assert c.get("/profile/alice", params={"api_key": ALICE_KEY, "token": ALICE_KEY, "key": ALICE_KEY}).status_code == 401
    assert c.get("/profile/alice", headers={"Cookie": f"api_key={ALICE_KEY}; Authorization=Bearer {ALICE_KEY}"}).status_code == 401


def test_a_valid_key_in_either_header_works_and_bearer_takes_precedence(app_ctx):
    c = app_ctx.client
    assert c.get("/agents/enterprise", headers={"X-API-Key": ALICE_KEY}).status_code == 200
    assert c.get("/agents/enterprise", headers=auth(ALICE_KEY)).status_code == 200
    # a bad bearer is not rescued by a good X-API-Key: no credential mixing
    assert c.get("/agents/enterprise", headers={"Authorization": "Bearer nope", "X-API-Key": ALICE_KEY}).status_code == 401


def test_non_ascii_credentials_fail_closed_not_with_a_server_error(app_ctx):
    response = app_ctx.client.get("/agents/enterprise", headers={b"x-api-key": "clé-été-ключ".encode()})
    assert response.status_code == 401


def test_a_failed_authentication_does_not_run_the_handler(app_ctx):
    app_ctx.client.post("/memory/s", json={"role": "user", "content": "WRITE-CANARY"})
    assert app_ctx.memory.get_history("s") == []


# ---- JWT --------------------------------------------------------------------------------------------------------------

@pytest.fixture
def jwt_ctx(make_app):
    return make_app(jwt=True)


def test_a_valid_token_authenticates_as_its_subject(jwt_ctx):
    c = jwt_ctx.client
    c.put("/profile/alice", json={"goals": ["g"]}, headers=auth(make_jwt()))
    assert c.get("/profile/alice", headers=auth(make_jwt())).status_code == 200
    assert c.get("/profile/bob", headers=auth(make_jwt())).status_code == 403  # token is scoped to alice


def test_alg_none_and_other_algorithms_are_refused(jwt_ctx):
    now = int(time.time())
    unsigned = ".".join([_b64(b'{"alg":"none","typ":"JWT"}'),
                         _b64(f'{{"iss":"{JWT_ISSUER}","aud":"nexus-api","sub":"alice","exp":{now + 600}}}'.encode()), ""])
    assert jwt_ctx.client.get("/profile/alice", headers=auth(unsigned)).status_code == 401
    for alg in ("None", "NONE", "HS384", "HS512", "RS256", "ES256", "hs256"):
        token = make_jwt(header={"alg": alg, "typ": "JWT"})
        assert jwt_ctx.client.get("/profile/alice", headers=auth(token)).status_code == 401, alg


def test_invalid_signatures_are_refused(jwt_ctx):
    c = jwt_ctx.client
    assert c.get("/profile/alice", headers=auth(make_jwt(secret="some-other-secret-0123456789-abcdefghijkl"))).status_code == 401
    assert c.get("/profile/alice", headers=auth(make_jwt(signature=_b64(b"x" * 32)))).status_code == 401
    assert c.get("/profile/alice", headers=auth(make_jwt(signature=""))).status_code == 401
    good = make_jwt()
    head, _payload, sig = good.split(".")
    tampered_payload = _b64(b'{"iss":"%s","aud":"nexus-api","sub":"bob","exp":%d}' % (JWT_ISSUER.encode(), int(time.time()) + 600))
    assert c.get("/profile/bob", headers=auth(f"{head}.{tampered_payload}.{sig}")).status_code == 401  # payload swapped, old signature


@pytest.mark.parametrize("claims,drop", [
    ({"exp": int(time.time()) - 3600}, ()), ({}, ("exp",)), ({"exp": "soon"}, ()), ({"exp": int(time.time()) + 10 * 86400}, ()),
    ({"nbf": int(time.time()) + 3600}, ()), ({"iss": "https://evil.example"}, ()), ({"aud": "other-api"}, ()),
    ({}, ("aud",)), ({}, ("iss",)), ({}, ("sub",)), ({"sub": ""}, ()), ({"sub": "system"}, ()), ({"sub": "a b"}, ()),
    ({"sub": ["alice"]}, ()), ({"sub": "x" * 500}, ()),
])
def test_tokens_with_bad_claims_are_refused(jwt_ctx, claims, drop):
    assert jwt_ctx.client.get("/profile/alice", headers=auth(make_jwt(claims, drop=drop))).status_code == 401


@pytest.mark.parametrize("header", [{"alg": "HS256", "jku": "https://evil.example/keys"},
                                    {"alg": "HS256", "jwk": {"kty": "oct"}}, {"alg": "HS256", "x5u": "https://evil.example"}])
def test_headers_that_point_at_a_key_are_refused(jwt_ctx, header):
    assert jwt_ctx.client.get("/profile/alice", headers=auth(make_jwt(header=header))).status_code == 401


def test_a_kid_header_cannot_select_a_different_key(jwt_ctx):
    token = make_jwt(header={"alg": "HS256", "kid": "../../dev/null"}, secret="some-other-secret-0123456789-abcdefghijkl")
    assert jwt_ctx.client.get("/profile/alice", headers=auth(token)).status_code == 401


@pytest.mark.parametrize("token", ["", "abc", "a.b", "a.b.c", "a.b.c.d", "....", "!!!.@@@.###", "e30.e30.e30",
                                   "eyJhbGciOiJIUzI1NiJ9.bm90LWpzb24.sig", "." * 3 + "x" * 50_000])
def test_malformed_tokens_are_refused_without_a_server_error(jwt_ctx, token):
    response = jwt_ctx.client.get("/profile/alice", headers=auth(token))
    assert response.status_code == 401


def test_a_token_cannot_claim_a_role_or_admin_privilege(jwt_ctx):
    token = make_jwt({"role": "admin", "is_admin": True, "admin": True, "scope": "admin", "roles": ["admin"]})
    assert jwt_ctx.client.get("/system/metrics", headers=auth(token)).status_code == 403
    assert jwt_ctx.client.get("/profile/bob", headers=auth(token)).status_code == 403


def test_the_signing_secret_is_never_an_api_key(jwt_ctx):
    assert jwt_ctx.client.get("/profile/alice", headers=auth(JWT_SECRET)).status_code == 401


# ---- stored (keystore) keys: revoked and expired ---------------------------------------------------------------------

def test_revoked_stored_keys_stop_working_immediately(app_ctx):
    issued = app_ctx.store.issue("carol", ttl_days=30)
    assert app_ctx.client.get("/profile/carol", headers=auth(issued.key)).status_code == 404  # authenticated (no profile yet)
    app_ctx.store.revoke(issued.key_id)
    assert app_ctx.client.get("/profile/carol", headers=auth(issued.key)).status_code == 401


def test_expired_stored_keys_are_refused(app_ctx):
    issued = app_ctx.store.issue("carol", ttl_days=1)
    app_ctx.clock.advance(days=2)
    assert app_ctx.client.get("/profile/carol", headers=auth(issued.key)).status_code == 401


def test_a_rotated_key_keeps_working_only_through_its_overlap(app_ctx):
    old = app_ctx.store.issue("carol", ttl_days=30)
    new = app_ctx.store.rotate(old.key_id, overlap_days=1)
    assert app_ctx.client.get("/profile/carol", headers=auth(new.key)).status_code == 404
    assert app_ctx.client.get("/profile/carol", headers=auth(old.key)).status_code == 404  # inside the overlap
    app_ctx.clock.advance(days=2)
    assert app_ctx.client.get("/profile/carol", headers=auth(old.key)).status_code == 401
    assert app_ctx.client.get("/profile/carol", headers=auth(new.key)).status_code == 404


def test_forged_stored_key_shapes_are_refused(app_ctx):
    for key in ("nxk_" + "0" * 40, "nxk_", "nxk_" + "a" * 5000):
        assert app_ctx.client.get("/profile/carol", headers=auth(key)).status_code == 401
    odd = {b"authorization": b"Bearer nxk_" + "é".encode() * 10}
    assert app_ctx.client.get("/profile/carol", headers=odd).status_code == 401


def test_a_keystore_outage_fails_closed_with_503_not_open(app_ctx, monkeypatch):
    issued = app_ctx.store.issue("carol", ttl_days=30)

    def broken(_key):
        raise RuntimeError("db down")

    monkeypatch.setattr(app_ctx.store, "authenticate", broken)
    response = app_ctx.client.get("/profile/carol", headers=auth(issued.key))
    assert response.status_code == 503 and "db down" not in response.text


def test_authentication_is_not_disabled_by_a_missing_configuration_in_production_mode(make_app, monkeypatch):
    with pytest.raises(RuntimeError, match="NEXUS_API_KEY"):
        make_app(env={"APP_ENV": "production"}, keys=False)


def test_the_operator_key_is_distinct_from_user_keys(app_ctx):
    assert app_ctx.client.get("/system/metrics", headers=auth(OPERATOR_KEY)).status_code == 200
    assert app_ctx.client.get("/system/metrics", headers=auth(ALICE_KEY)).status_code == 403
