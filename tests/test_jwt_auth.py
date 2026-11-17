"""JWT (HS256) verification: the verifier itself, its configuration, then the real middleware and routes.

Tokens are forged by hand here, which is the point: every way a token can be wrong is built explicitly and must
be refused. Time is a fixed clock, so nothing sleeps. This is not OIDC and these tests do not claim it is.
"""
import base64
import hashlib
import hmac
import json
import random
import time
from types import SimpleNamespace
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main
from app.core.database import Base
from app.core.memory import NexusMemory
from app.security import jwt_auth
from app.security.hardening import (
    install_security,
    load_security_settings,
    parse_user_keys,
)
from app.security.identity import Principal, get_principal, is_valid_user_id
from app.security.jwt_auth import JwtSettings, JwtVerifier, load_jwt_settings

NOW = 1_800_000_000.0
SECRET = "jwt-secret-0123456789-abcdefghijklmnopqrstuv"
OLD_SECRET = "previous-secret-0123456789-abcdefghijklmnop"
ISSUER = "https://tokens.example.test"
AUDIENCE = "nexus-api"
OPERATOR_KEY = "operator-key-0123456789-abcdef"
ENV_USER_KEY = "env-user-key-0123456789-abcdefg"
ENV_NAMES = ("NEXUS_DEMO_MODE", "APP_ENV", "NEXUS_API_KEY", "NEXUS_API_KEYS", "NEXUS_REQUIRE_API_KEY",
             "NEXUS_KEYSTORE_ENABLED", "NEXUS_JWT_SECRETS", "NEXUS_JWT_ISSUER", "NEXUS_JWT_AUDIENCE",
             "NEXUS_JWT_LEEWAY_SECONDS", "NEXUS_JWT_MAX_LIFETIME_SECONDS")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_token(claims=None, *, header=None, secret=SECRET, drop=(), signature=None, digest=hashlib.sha256,
               header_bytes=None, payload_bytes=None):
    header = {"alg": "HS256", "typ": "JWT"} if header is None else header
    body = {"iss": ISSUER, "aud": AUDIENCE, "sub": "alice", "iat": NOW - 5, "exp": NOW + 600}
    body.update(claims or {})
    for name in drop:
        body.pop(name, None)
    head = _b64(header_bytes if header_bytes is not None else json.dumps(header).encode())
    payload = _b64(payload_bytes if payload_bytes is not None else json.dumps(body).encode())
    if signature is None:
        signature = _b64(hmac.new(secret.encode(), f"{head}.{payload}".encode(), digest).digest())
    return f"{head}.{payload}.{signature}"


def _settings(*secrets, **kwargs):
    return JwtSettings(secrets=tuple(s.encode() for s in (secrets or (SECRET,))), issuer=ISSUER, audience=AUDIENCE,
                       **kwargs)


@pytest.fixture
def verifier():
    return JwtVerifier(_settings(), clock=lambda: NOW)


# ---- accepted ---------------------------------------------------------------------------------------

def test_a_valid_token_gives_a_scoped_user(verifier):
    principal = verifier.verify(make_token())
    assert principal == Principal(user_id="alice", is_admin=False) and principal.scoped


def test_audience_may_be_a_list_and_unknown_claims_and_headers_are_ignored(verifier):
    token = make_token({"aud": ["other", AUDIENCE], "email": "a@example.test", "custom": {"x": 1}},
                       header={"alg": "HS256", "typ": "JWT", "kid": "k1"})
    assert verifier.verify(token).user_id == "alice"


@pytest.mark.parametrize("claims", [
    {"role": "admin"}, {"is_admin": True}, {"admin": True}, {"roles": ["admin"]}, {"scope": "admin"},
    {"scp": ["nexus:admin"]}, {"groups": ["admins"]}, {"user_id": "operator"},
])
def test_no_claim_makes_a_token_an_administrator_or_another_user(verifier, claims):
    principal = verifier.verify(make_token(claims))
    assert principal is not None and principal.is_admin is False and principal.role == "user"
    assert principal.user_id == "alice"


@pytest.mark.parametrize("claims", [
    {"exp": NOW - 29}, {"nbf": NOW + 30}, {"iat": NOW + 30}, {"exp": NOW + 86_400}, {"aud": ["a", "b", AUDIENCE]},
])
def test_boundaries_that_are_still_accepted(verifier, claims):
    assert verifier.verify(make_token(claims)) is not None


def test_a_secret_can_be_rotated_with_an_overlap():
    both = JwtVerifier(_settings(SECRET, OLD_SECRET), clock=lambda: NOW)
    assert both.verify(make_token(secret=SECRET)) is not None
    assert both.verify(make_token(secret=OLD_SECRET)) is not None
    assert both.verify(make_token(secret="x" * 40)) is None
    assert JwtVerifier(_settings(SECRET), clock=lambda: NOW).verify(make_token(secret=OLD_SECRET)) is None


# ---- refused ----------------------------------------------------------------------------------------

def _tamper(replace_payload=None, replace_header=None):
    genuine = make_token().split(".")
    other = make_token(replace_payload or {}).split(".")
    head = _b64(json.dumps(replace_header).encode()) if replace_header else genuine[0]
    return ".".join([head, other[1] if replace_payload else genuine[1], genuine[2]])


def _long_token():
    return make_token({"pad": "x" * 5000})


REFUSED = {
    # algorithm confusion
    "alg none, empty signature": lambda: make_token(header={"alg": "none", "typ": "JWT"}, signature=""),
    "alg none, junk signature": lambda: make_token(header={"alg": "none"}, signature=_b64(b"junk")),
    "alg HS512, correctly signed": lambda: make_token(header={"alg": "HS512"}, digest=hashlib.sha512),
    "alg RS256": lambda: make_token(header={"alg": "RS256"}, signature=_b64(b"junk")),
    "alg lower case": lambda: make_token(header={"alg": "hs256"}),
    "alg missing": lambda: make_token(header={"typ": "JWT"}),
    "alg not a string": lambda: make_token(header={"alg": ["HS256"]}),
    "header is a list": lambda: make_token(header_bytes=b'["HS256"]'),
    "header is not JSON": lambda: make_token(header_bytes=b"not json"),
    # headers that point at a key or must be understood
    "crit header": lambda: make_token(header={"alg": "HS256", "crit": ["exp"]}),
    "jku header": lambda: make_token(header={"alg": "HS256", "jku": "https://evil.example/keys"}),
    "jwk header": lambda: make_token(header={"alg": "HS256", "jwk": {"kty": "oct", "k": "AAAA"}}),
    "x5u header": lambda: make_token(header={"alg": "HS256", "x5u": "https://evil.example/cert"}),
    "x5c header": lambda: make_token(header={"alg": "HS256", "x5c": ["AAAA"]}),
    # signature
    "wrong secret": lambda: make_token(secret="x" * 40),
    "payload replaced after signing": lambda: _tamper(replace_payload={"sub": "bob"}),
    "header replaced after signing": lambda: _tamper(replace_header={"alg": "HS256", "kid": "other"}),
    "truncated signature": lambda: make_token().rsplit(".", 1)[0] + "." + make_token().rsplit(".", 1)[1][:-4],
    "signature of the wrong algorithm": lambda: make_token(digest=hashlib.sha512),
    # issuer and audience
    "wrong issuer": lambda: make_token({"iss": "https://evil.example"}),
    "issuer with a trailing slash": lambda: make_token({"iss": ISSUER + "/"}),
    "issuer missing": lambda: make_token(drop=("iss",)),
    "wrong audience": lambda: make_token({"aud": "someone-else"}),
    "audience list without ours": lambda: make_token({"aud": ["a", "b"]}),
    "audience list with a non-string": lambda: make_token({"aud": [AUDIENCE, 7]}),
    "audience a number": lambda: make_token({"aud": 7}),
    "audience missing": lambda: make_token(drop=("aud",)),
    # time
    "exp missing": lambda: make_token(drop=("exp",)),
    "exp in the past": lambda: make_token({"exp": NOW - 3600}),
    "exp at the end of the leeway": lambda: make_token({"exp": NOW - 30}),
    "exp as a string": lambda: make_token({"exp": str(NOW + 600)}),
    "exp as a boolean": lambda: make_token({"exp": True}),
    "exp too large for a float": lambda: make_token({"exp": 10 ** 400}),
    "exp infinite": lambda: make_token(payload_bytes=json.dumps({"iss": ISSUER, "aud": AUDIENCE, "sub": "alice"})
                                       .replace("}", ', "exp": 1e999}').encode()),
    "exp beyond the maximum lifetime": lambda: make_token({"exp": NOW + 86_401}),
    "nbf in the future": lambda: make_token({"nbf": NOW + 31}),
    "nbf not a number": lambda: make_token({"nbf": "soon"}),
    "nbf as a boolean": lambda: make_token({"nbf": True}),  # would read as 1.0, long in the past
    "iat in the future": lambda: make_token({"iat": NOW + 31}),
    "iat as a boolean": lambda: make_token({"iat": True}),
    # subject
    "sub missing": lambda: make_token(drop=("sub",)),
    "sub a number": lambda: make_token({"sub": 123}),
    "sub is the reserved shared pool": lambda: make_token({"sub": "system"}),
    "sub with a space": lambda: make_token({"sub": "al ice"}),
    "sub empty": lambda: make_token({"sub": ""}),
    "sub too long": lambda: make_token({"sub": "a" * 129}),
    # structure and encoding
    "two segments": lambda: ".".join(make_token().split(".")[:2]),
    "four segments": lambda: make_token() + ".extra",
    "empty header segment": lambda: "." + ".".join(make_token().split(".")[1:]),
    "characters outside base64url": lambda: "!!!.@@@.###",
    "padding characters": lambda: make_token().replace(".", "=.", 1),
    "non-ASCII": lambda: make_token() + "é",
    "longer than the limit even though correctly signed": _long_token,
    "payload is a list": lambda: make_token(payload_bytes=b"[1, 2]"),
    "payload is not JSON": lambda: make_token(payload_bytes=b"not json"),
    "payload nested too deeply": lambda: make_token(payload_bytes=b"[" * 2000),
    "duplicate keys in the payload": lambda: make_token(payload_bytes=(
        b'{"iss": "%s", "aud": "%s", "sub": "alice", "sub": "bob", "exp": %d}'
        % (ISSUER.encode(), AUDIENCE.encode(), NOW + 600))),
    "NaN anywhere in the payload": lambda: make_token(payload_bytes=json.dumps(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "alice", "exp": NOW + 600}).replace("}", ', "x": NaN}').encode()),
    "empty token": lambda: "",
    "only dots": lambda: "..",
}


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_refused(verifier, name):
    assert verifier.verify(REFUSED[name]()) is None


@pytest.mark.parametrize("value", [None, 123, b"bytes", ["a", "b", "c"], {"a": 1}])
def test_a_non_string_is_refused(verifier, value):
    assert verifier.verify(value) is None


def test_a_parser_recursion_error_is_a_refusal_not_a_crash(verifier, monkeypatch):
    # Whether deeply nested JSON raises RecursionError depends on the Python version (the C scanner's limit
    # changed), so the condition is forced here instead of relying on the interpreter.
    def too_deep(*args, **kwargs):
        raise RecursionError

    monkeypatch.setattr(jwt_auth, "json", SimpleNamespace(loads=too_deep))
    assert verifier.verify(make_token()) is None


def test_garbage_is_refused_without_an_exception(verifier):
    rng = random.Random(7)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-.="
    garbage = ["", ".", "..", "...", "a.b.c", "eyJ.eyJ.sig", "\x00\x00\x00", " ", "Bearer x", "{}", "null"]
    garbage += ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 200))) for _ in range(500)]
    assert all(verifier.verify(item) is None for item in garbage)


def test_the_user_id_rule_is_the_same_everywhere():
    for candidate in ["alice", "system", "has space", "", "x" * 128, "x" * 129, "a/b", "ünï", "tab\there"]:
        try:
            parse_user_keys(f"{'k' * 24}={candidate}")
            env_accepts = True
        except ValueError:
            env_accepts = False
        assert is_valid_user_id(candidate) == env_accepts, repr(candidate)


# ---- configuration ------------------------------------------------------------------------------------

def _configure(monkeypatch, **env):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def test_without_secrets_token_verification_is_off(monkeypatch):
    _configure(monkeypatch)
    assert load_jwt_settings() is None


def test_a_valid_configuration_is_read_and_never_shown(monkeypatch):
    _configure(monkeypatch, NEXUS_JWT_SECRETS=f" {SECRET} , {OLD_SECRET} ", NEXUS_JWT_ISSUER=ISSUER,
               NEXUS_JWT_AUDIENCE=AUDIENCE, NEXUS_JWT_LEEWAY_SECONDS="10", NEXUS_JWT_MAX_LIFETIME_SECONDS="3600")
    settings = load_jwt_settings()
    assert settings.secrets == (SECRET.encode(), OLD_SECRET.encode())
    assert (settings.issuer, settings.audience, settings.leeway_seconds, settings.max_lifetime_seconds) == (
        ISSUER, AUDIENCE, 10, 3600)
    assert SECRET not in repr(settings) and OLD_SECRET not in repr(settings)


@pytest.mark.parametrize("env, error, match", [
    ({"NEXUS_JWT_SECRETS": SECRET}, RuntimeError, "NEXUS_JWT_ISSUER and NEXUS_JWT_AUDIENCE are required"),
    ({"NEXUS_JWT_SECRETS": SECRET, "NEXUS_JWT_ISSUER": ISSUER}, RuntimeError, "NEXUS_JWT_ISSUER and"),
    ({"NEXUS_JWT_SECRETS": "short", "NEXUS_JWT_ISSUER": ISSUER, "NEXUS_JWT_AUDIENCE": AUDIENCE}, ValueError,
     "at least 32 bytes"),
    ({"NEXUS_JWT_SECRETS": f"{SECRET},{SECRET}", "NEXUS_JWT_ISSUER": ISSUER, "NEXUS_JWT_AUDIENCE": AUDIENCE},
     ValueError, "duplicated"),
    ({"NEXUS_JWT_SECRETS": f"{SECRET},{OLD_SECRET},{'z' * 40}", "NEXUS_JWT_ISSUER": ISSUER,
      "NEXUS_JWT_AUDIENCE": AUDIENCE}, ValueError, "at most 2"),
    ({"NEXUS_JWT_SECRETS": SECRET, "NEXUS_JWT_ISSUER": ISSUER, "NEXUS_JWT_AUDIENCE": AUDIENCE,
      "NEXUS_JWT_LEEWAY_SECONDS": "301"}, ValueError, "NEXUS_JWT_LEEWAY_SECONDS must be from 0 to 300"),
    ({"NEXUS_JWT_SECRETS": SECRET, "NEXUS_JWT_ISSUER": ISSUER, "NEXUS_JWT_AUDIENCE": AUDIENCE,
      "NEXUS_JWT_LEEWAY_SECONDS": "soon"}, ValueError, "must be a whole number"),
    ({"NEXUS_JWT_SECRETS": SECRET, "NEXUS_JWT_ISSUER": ISSUER, "NEXUS_JWT_AUDIENCE": AUDIENCE,
      "NEXUS_JWT_MAX_LIFETIME_SECONDS": "59"}, ValueError, "NEXUS_JWT_MAX_LIFETIME_SECONDS must be from"),
])
def test_a_partial_or_invalid_configuration_fails_at_startup(monkeypatch, env, error, match):
    _configure(monkeypatch, **env)
    with pytest.raises(error, match=match):
        load_jwt_settings()


def test_enabling_tokens_turns_authentication_on_and_still_needs_an_environment_key(monkeypatch):
    _configure(monkeypatch, NEXUS_JWT_SECRETS=SECRET, NEXUS_JWT_ISSUER=ISSUER, NEXUS_JWT_AUDIENCE=AUDIENCE)
    with pytest.raises(RuntimeError, match="NEXUS_API_KEY or NEXUS_API_KEYS is required"):
        load_security_settings()
    monkeypatch.setenv("NEXUS_API_KEY", OPERATOR_KEY)
    settings = load_security_settings()
    assert settings.require_api_key is True and settings.jwt is not None
    monkeypatch.setenv("NEXUS_REQUIRE_API_KEY", "false")  # an explicit opt-out does not switch it back off
    assert load_security_settings().require_api_key is True


# ---- through the real middleware and routes -------------------------------------------------------------

def _probe(monkeypatch, *, verifier=None, env_jwt=False):
    _configure(monkeypatch, NEXUS_API_KEY=OPERATOR_KEY, NEXUS_API_KEYS=f"{ENV_USER_KEY}=env-user")
    if env_jwt:
        monkeypatch.setenv("NEXUS_JWT_SECRETS", SECRET)
        monkeypatch.setenv("NEXUS_JWT_ISSUER", ISSUER)
        monkeypatch.setenv("NEXUS_JWT_AUDIENCE", AUDIENCE)
    api = FastAPI()

    @api.get("/whoami")
    def whoami(principal: Annotated[Principal, Depends(get_principal)]):
        return {"role": principal.role, "user_id": principal.user_id, "scoped": principal.scoped}

    install_security(api, jwt_verifier=verifier)
    return TestClient(api)


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_a_valid_token_authenticates_as_a_scoped_user(monkeypatch, verifier):
    client = _probe(monkeypatch, verifier=verifier)
    response = client.get("/whoami", headers=_bearer(make_token()))
    assert response.status_code == 200
    assert response.json() == {"role": "user", "user_id": "alice", "scoped": True}


def test_bad_tokens_get_401(monkeypatch, verifier):
    client = _probe(monkeypatch, verifier=verifier)
    for token in (make_token({"exp": NOW - 3600}), make_token(secret="x" * 40), make_token({"aud": "other"}),
                  make_token(header={"alg": "none"}, signature=""), "not.a.token"):
        response = client.get("/whoami", headers=_bearer(token))
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"


def test_a_token_is_accepted_only_in_the_authorization_header(monkeypatch, verifier):
    client = _probe(monkeypatch, verifier=verifier)
    assert client.get("/whoami", headers={"X-API-Key": make_token()}).status_code == 401


def test_api_keys_still_work_beside_tokens(monkeypatch, verifier):
    client = _probe(monkeypatch, verifier=verifier)
    assert client.get("/whoami", headers=_bearer(OPERATOR_KEY)).json()["role"] == "admin"
    assert client.get("/whoami", headers=_bearer(ENV_USER_KEY)).json()["user_id"] == "env-user"


def test_the_verifier_is_built_from_the_environment_only_when_configured(monkeypatch):
    live = make_token({"iat": time.time() - 5, "exp": time.time() + 600})
    assert _probe(monkeypatch, env_jwt=True).get("/whoami", headers=_bearer(live)).status_code == 200
    assert _probe(monkeypatch).get("/whoami", headers=_bearer(live)).status_code == 401


@pytest.fixture
def real_client(monkeypatch, verifier):
    _configure(monkeypatch, NEXUS_API_KEY=OPERATOR_KEY)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(main, "memory", NexusMemory(max_messages=20, history_limit=10, session_factory=factory,
                                                    initialize=False))
    monkeypatch.setattr(main, "DEMO_MODE", False)
    api = FastAPI()
    api.router.routes.extend(main.app.router.routes)  # the real production routes
    install_security(api, jwt_verifier=verifier)
    return TestClient(api)


def test_token_identities_are_confined_to_their_own_data(real_client):
    alice, bob = make_token(), make_token({"sub": "bob"})
    assert real_client.put("/profile/alice", json={"goals": ["learn"]}, headers=_bearer(alice)).status_code == 200
    assert real_client.get("/profile/alice", headers=_bearer(alice)).status_code == 200
    assert real_client.get("/profile/alice", headers=_bearer(bob)).status_code == 403
    assert real_client.delete("/profile/alice", headers=_bearer(bob)).status_code == 403


def test_a_token_never_reaches_administrator_routes_whatever_it_claims(real_client):
    for claims in ({}, {"role": "admin"}, {"is_admin": True}, {"scope": "admin"}, {"sub": "operator"}):
        assert real_client.get("/activity/summary", headers=_bearer(make_token(claims))).status_code == 403, claims
        assert real_client.get("/integrations/aegis-x/status", headers=_bearer(make_token(claims))).status_code == 403
