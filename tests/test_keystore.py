"""API key store: hashed storage, expiry, revocation and rotation, the command line, and how stored keys
authenticate through the real middleware and routes.

Time is a fake clock, so nothing sleeps. The database is an in-memory SQLite engine. No model is involved.
"""
import io
from datetime import UTC, datetime, timedelta
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
from app.security import keystore
from app.security.hardening import (
    install_security,
    load_security_settings,
    parse_user_keys,
)
from app.security.identity import Principal, get_principal
from app.security.keystore import KEY_PREFIX, ApiKey, KeyStore, KeyStoreError, hash_key

OPERATOR_KEY = "operator-key-0123456789-abcdef"
ENV_USER_KEY = "env-user-key-0123456789-abcdefg"
ENV_KEYS = ("NEXUS_DEMO_MODE", "APP_ENV", "NEXUS_API_KEY", "NEXUS_API_KEYS", "NEXUS_REQUIRE_API_KEY",
            "NEXUS_KEYSTORE_ENABLED")


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture
def store(engine, clock):
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return KeyStore(session_factory=factory, clock=clock, initialize=False)


def _rows(engine):
    with engine.connect() as connection:
        return [dict(row._mapping) for row in connection.exec_driver_sql("SELECT * FROM api_keys")]


# ---- storage --------------------------------------------------------------------------------------

def test_a_key_is_stored_only_as_a_digest(store, engine):
    issued = store.issue("alice", label="laptop")
    assert issued.key.startswith(KEY_PREFIX) and len(issued.key) >= 40
    (row,) = _rows(engine)
    assert row["key_hash"] == hash_key(issued.key)
    assert all(issued.key not in str(value) for value in row.values())  # the raw key is in no column
    assert "key=<hidden>" in repr(issued) and issued.key not in repr(issued)


def test_every_issue_gives_a_fresh_key_and_id(store):
    first, second = store.issue("alice"), store.issue("alice")
    assert first.key != second.key and first.key_id != second.key_id


def test_listing_shows_metadata_and_never_a_key_or_digest(store, clock):
    live = store.issue("alice", label="laptop")
    revoked = store.issue("bob")
    store.revoke(revoked.key_id)
    store.issue("carol", ttl_days=1)
    clock.advance(days=2)
    everything = store.list_keys(include_inactive=True)
    assert sorted(item["status"] for item in everything) == ["active", "expired", "revoked"]
    assert [item["key_id"] for item in store.list_keys()] == [live.key_id]  # inactive keys are hidden by default
    assert set(everything[0]) == {"key_id", "user_id", "role", "label", "status", "created_at", "expires_at",
                                  "revoked_at"}
    blob = repr(everything)
    assert live.key not in blob and hash_key(live.key) not in blob


# ---- authentication -------------------------------------------------------------------------------

def test_user_and_admin_keys_give_the_matching_principal(store):
    user = store.authenticate(store.issue("alice").key)
    assert user == Principal(user_id="alice", is_admin=False) and user.scoped and user.role == "user"
    admin = store.authenticate(store.issue("ops", role="admin").key)
    assert admin.is_admin and not admin.scoped and admin.role == "admin"


def test_the_default_role_is_user_not_admin(store):
    assert store.authenticate(store.issue("alice").key).role == "user"


@pytest.mark.parametrize("supplied", [
    "", "nxk_unknown-key", KEY_PREFIX + "a" * 300, ENV_USER_KEY, "eyJhbGciOiJIUzI1NiJ9.e30.sig",
    "nxk_é-non-ascii", None, 12345,
])
def test_unknown_malformed_and_foreign_keys_are_refused(store, supplied):
    store.issue("alice")
    assert store.authenticate(supplied) is None


def test_every_key_expires_at_the_boundary(store, clock):
    issued = store.issue("alice", ttl_days=1)
    clock.advance(hours=23, minutes=59)
    assert store.authenticate(issued.key) is not None
    clock.advance(minutes=1)  # exactly one day: expired
    assert store.authenticate(issued.key) is None


def test_revocation_is_immediate_and_idempotent(store):
    issued = store.issue("alice")
    assert store.authenticate(issued.key) is not None
    assert store.revoke(issued.key_id) is True
    assert store.authenticate(issued.key) is None
    assert store.revoke(issued.key_id) is False
    with pytest.raises(KeyStoreError, match="unknown key id"):
        store.revoke("k_does-not-exist")


def test_an_unrecognised_role_in_the_table_grants_nothing(store, engine, clock):
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(ApiKey(id="k_odd", key_hash=hash_key("nxk_odd-role-key"), user_id="mallory", role="superuser",
                      created_at=clock(), expires_at=clock() + timedelta(days=1)))
        db.commit()
    assert store.authenticate("nxk_odd-role-key") is None


# ---- rotation -------------------------------------------------------------------------------------

def test_rotation_overlaps_then_cuts_over(store, clock):
    old = store.issue("alice", label="laptop")
    new = store.rotate(old.key_id, overlap_days=7)
    assert (new.user_id, new.role) == ("alice", "user") and new.key != old.key and new.key_id != old.key_id
    assert store.authenticate(old.key) is not None and store.authenticate(new.key) is not None
    clock.advance(days=6, hours=23)
    assert store.authenticate(old.key) is not None
    clock.advance(hours=1)  # the seventh day: the old key is gone
    assert store.authenticate(old.key) is None
    assert store.authenticate(new.key) is not None
    assert [item["label"] for item in store.list_keys()] == ["laptop"]


def test_rotation_never_lengthens_the_old_key(store, clock):
    old = store.issue("alice", ttl_days=3)
    store.rotate(old.key_id, overlap_days=30)  # a longer overlap than the key has left changes nothing
    clock.advance(days=3)
    assert store.authenticate(old.key) is None


def test_rotation_with_no_overlap_retires_the_old_key_at_once(store):
    old = store.issue("alice")
    new = store.rotate(old.key_id, overlap_days=0)
    assert store.authenticate(old.key) is None and store.authenticate(new.key) is not None


def test_rotation_keeps_the_role(store):
    old = store.issue("ops", role="admin")
    assert store.authenticate(store.rotate(old.key_id).key).is_admin


def test_only_an_active_key_can_be_rotated(store, clock):
    revoked = store.issue("alice")
    store.revoke(revoked.key_id)
    expiring = store.issue("bob", ttl_days=1)
    clock.advance(days=2)
    for key_id in (revoked.key_id, expiring.key_id):
        with pytest.raises(KeyStoreError, match="only an active key"):
            store.rotate(key_id)
    with pytest.raises(KeyStoreError, match="unknown key id"):
        store.rotate("k_does-not-exist")
    assert store.list_keys() == []  # a refused rotation issued nothing


# ---- validation -----------------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs", [
    {"user_id": "system"}, {"user_id": ""}, {"user_id": "has space"}, {"user_id": "x" * 129},
    {"user_id": "alice", "role": "root"}, {"user_id": "alice", "role": "ADMIN"},
    {"user_id": "alice", "ttl_days": 0}, {"user_id": "alice", "ttl_days": 367}, {"user_id": "alice", "ttl_days": True},
    {"user_id": "alice", "ttl_days": "30"}, {"user_id": "alice", "label": "x" * 121},
])
def test_invalid_requests_issue_nothing(store, kwargs):
    with pytest.raises(KeyStoreError):
        store.issue(**kwargs)
    assert store.list_keys(include_inactive=True) == []


@pytest.mark.parametrize("candidate", [
    "alice", "system", "has space", "", "x" * 128, "x" * 129, "a/b", "ünï", "tab\there",
])
def test_user_id_rules_match_the_environment_key_rules(store, candidate):
    try:
        parse_user_keys(f"{'k' * 24}={candidate}")
        env_accepts = True
    except ValueError:
        env_accepts = False
    try:
        store.issue(candidate)
        store_accepts = True
    except KeyStoreError:
        store_accepts = False
    assert store_accepts == env_accepts, repr(candidate)


# ---- through the real middleware -------------------------------------------------------------------

def _probe_client(monkeypatch, store):
    for name in ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NEXUS_API_KEY", OPERATOR_KEY)
    monkeypatch.setenv("NEXUS_API_KEYS", f"{ENV_USER_KEY}=env-user")
    api = FastAPI()

    @api.get("/whoami")
    def whoami(principal: Annotated[Principal, Depends(get_principal)]):
        return {"role": principal.role, "user_id": principal.user_id, "scoped": principal.scoped}

    install_security(api, key_store=store)
    return TestClient(api)


def _bearer(key):
    return {"Authorization": f"Bearer {key}"}


def test_a_stored_user_key_authenticates_as_a_scoped_user(monkeypatch, store):
    client = _probe_client(monkeypatch, store)
    response = client.get("/whoami", headers=_bearer(store.issue("alice").key))
    assert response.status_code == 200
    assert response.json() == {"role": "user", "user_id": "alice", "scoped": True}


def test_a_stored_admin_key_authenticates_as_admin(monkeypatch, store):
    client = _probe_client(monkeypatch, store)
    response = client.get("/whoami", headers={"X-API-Key": store.issue("ops", role="admin").key})
    assert response.json() == {"role": "admin", "user_id": None, "scoped": False}


def test_environment_keys_keep_working_beside_the_store(monkeypatch, store):
    client = _probe_client(monkeypatch, store)
    assert client.get("/whoami", headers=_bearer(OPERATOR_KEY)).json()["role"] == "admin"
    assert client.get("/whoami", headers=_bearer(ENV_USER_KEY)).json()["user_id"] == "env-user"


def test_revoked_expired_and_unknown_stored_keys_get_401(monkeypatch, store, clock):
    client = _probe_client(monkeypatch, store)
    revoked, expiring = store.issue("alice"), store.issue("bob", ttl_days=1)
    store.revoke(revoked.key_id)
    clock.advance(days=2)
    for key in (revoked.key, expiring.key, "nxk_never-issued"):
        response = client.get("/whoami", headers=_bearer(key))
        assert response.status_code == 401, key
        assert response.headers["WWW-Authenticate"] == "Bearer"
    assert client.get("/whoami").status_code == 401
    # a non-ASCII stored-key-shaped credential is a clean 401, not a 500 (bytes, as in test_tenant_isolation.py)
    assert client.get("/whoami", headers={"Authorization": b"Bearer nxk_\xe9-non-ascii"}).status_code == 401


def test_revocation_applies_to_the_very_next_request(monkeypatch, store):
    client = _probe_client(monkeypatch, store)
    issued = store.issue("alice")
    assert client.get("/whoami", headers=_bearer(issued.key)).status_code == 200
    store.revoke(issued.key_id)
    assert client.get("/whoami", headers=_bearer(issued.key)).status_code == 401


def test_rotation_through_the_middleware(monkeypatch, store, clock):
    client = _probe_client(monkeypatch, store)
    old = store.issue("alice")
    new = store.rotate(old.key_id, overlap_days=2)
    assert client.get("/whoami", headers=_bearer(old.key)).status_code == 200
    assert client.get("/whoami", headers=_bearer(new.key)).status_code == 200
    clock.advance(days=2)
    assert client.get("/whoami", headers=_bearer(old.key)).status_code == 401
    assert client.get("/whoami", headers=_bearer(new.key)).status_code == 200


def test_a_store_failure_fails_closed_with_503(monkeypatch, clock):
    def broken_session():
        raise RuntimeError("database is down")

    broken = KeyStore(session_factory=broken_session, clock=clock, initialize=False)
    client = _probe_client(monkeypatch, broken)
    response = client.get("/whoami", headers=_bearer("nxk_any-stored-looking-key"))
    assert response.status_code == 503
    assert response.json() == {"detail": "Authentication is temporarily unavailable."}
    assert response.headers["Retry-After"] == "5"
    assert "database is down" not in response.text
    # Environment keys do not depend on the store, and a key that is not a stored-key shape never reaches it.
    assert client.get("/whoami", headers=_bearer(OPERATOR_KEY)).status_code == 200
    assert client.get("/whoami", headers=_bearer("not-a-stored-key-0123456789")).status_code == 401


def test_enabling_the_store_turns_authentication_on_and_still_needs_an_environment_key(monkeypatch):
    # Without this, a development setup that enables the store but sets no environment key would run with
    # authentication off (the development default) and never consult the store.
    for name in ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NEXUS_KEYSTORE_ENABLED", "true")
    with pytest.raises(RuntimeError, match="NEXUS_API_KEY or NEXUS_API_KEYS is required"):
        load_security_settings()
    monkeypatch.setenv("NEXUS_API_KEY", OPERATOR_KEY)
    settings = load_security_settings()
    assert settings.require_api_key is True and settings.keystore_enabled is True
    monkeypatch.setenv("NEXUS_REQUIRE_API_KEY", "false")  # an explicit opt-out does not switch it back off
    assert load_security_settings().require_api_key is True
    monkeypatch.setenv("NEXUS_DEMO_MODE", "true")  # the public demo has no authentication by design
    assert load_security_settings().require_api_key is False


def test_the_store_is_created_only_when_enabled(monkeypatch):
    created = []

    class FakeStore:
        def __init__(self):
            created.append(True)

        def authenticate(self, raw_key):
            return None

    monkeypatch.setattr(keystore, "KeyStore", FakeStore)
    for name in ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NEXUS_API_KEY", OPERATOR_KEY)
    install_security(FastAPI())
    assert created == []  # default: no store, no database access
    monkeypatch.setenv("NEXUS_KEYSTORE_ENABLED", "true")
    install_security(FastAPI())
    assert created == [True]
    assert load_security_settings().keystore_enabled is True
    monkeypatch.delenv("NEXUS_KEYSTORE_ENABLED")
    assert load_security_settings().keystore_enabled is False


# ---- stored keys obey the same isolation and role rules as environment keys ---------------------------

@pytest.fixture
def real_client(monkeypatch, store, engine):
    for name in ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NEXUS_API_KEY", OPERATOR_KEY)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(main, "memory", NexusMemory(max_messages=20, history_limit=10, session_factory=factory,
                                                    initialize=False))
    monkeypatch.setattr(main, "DEMO_MODE", False)
    monkeypatch.setattr(main.activity_logger, "get_summary", lambda: {"total_runs": 0})
    api = FastAPI()
    api.router.routes.extend(main.app.router.routes)  # the real production routes
    install_security(api, key_store=store)
    return TestClient(api)


def test_stored_user_keys_are_confined_to_their_own_data(real_client, store):
    alice, bob = store.issue("alice").key, store.issue("bob").key
    assert real_client.put("/profile/alice", json={"goals": ["learn"]}, headers=_bearer(alice)).status_code == 200
    assert real_client.get("/profile/alice", headers=_bearer(alice)).status_code == 200
    assert real_client.get("/profile/alice", headers=_bearer(bob)).status_code == 403
    assert real_client.delete("/profile/alice", headers=_bearer(bob)).status_code == 403


def test_stored_keys_follow_the_role_gate(real_client, store):
    user, admin = store.issue("alice").key, store.issue("ops", role="admin").key
    assert real_client.get("/activity/summary", headers=_bearer(user)).status_code == 403
    assert real_client.get("/activity/summary", headers=_bearer(admin)).status_code == 200
    # an admin key is unrestricted across users, like the operator key
    real_client.put("/profile/alice", json={"goals": ["learn"]}, headers=_bearer(user))
    assert real_client.get("/profile/alice", headers=_bearer(admin)).status_code == 200


# ---- command line ---------------------------------------------------------------------------------

def _cli(store, *argv):
    out, err = io.StringIO(), io.StringIO()
    code = keystore.main(list(argv), store=store, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def _printed_key(output):
    return next(line.split()[1] for line in output.splitlines() if line.startswith("key:"))


def test_cli_issue_list_rotate_revoke(store, monkeypatch):
    monkeypatch.setenv("NEXUS_KEYSTORE_ENABLED", "true")
    code, out, err = _cli(store, "issue", "--user", "alice", "--label", "ci", "--ttl-days", "30")
    assert code == 0 and "Store this key now" in err
    key = _printed_key(out)
    assert store.authenticate(key).user_id == "alice"
    key_id = next(line.split()[1] for line in out.splitlines() if line.startswith("key_id:"))

    code, listing, _ = _cli(store, "list")
    assert code == 0 and key_id in listing and "alice" in listing and key not in listing

    code, out, _ = _cli(store, "rotate", key_id, "--overlap-days", "1")
    assert code == 0
    new_key = _printed_key(out)
    assert store.authenticate(new_key) is not None and store.authenticate(key) is not None

    new_id = next(line.split()[1] for line in out.splitlines() if line.startswith("key_id:"))
    assert _cli(store, "revoke", new_id)[1].strip() == "revoked"
    assert _cli(store, "revoke", new_id)[1].strip() == "already revoked"
    assert store.authenticate(new_key) is None
    assert "revoked" in _cli(store, "list", "--all")[1]


def test_cli_defaults_to_the_user_role(store):
    _, out, _ = _cli(store, "issue", "--user", "alice")
    assert "role:       user" in out
    _, out, _ = _cli(store, "issue", "--user", "ops", "--role", "admin")
    assert "role:       admin" in out


def test_cli_reports_errors_without_a_traceback(store):
    code, out, err = _cli(store, "revoke", "k_does-not-exist")
    assert (code, out) == (1, "") and err.startswith("error: unknown key id")
    assert _cli(store, "issue", "--user", "system")[0] == 1
    assert _cli(store, "issue", "--user", "alice", "--ttl-days", "0")[0] == 1
    with pytest.raises(SystemExit) as raised:
        _cli(store, "issue", "--user", "alice", "--role", "root")
    assert raised.value.code == 2


def test_cli_says_when_stored_keys_are_not_enabled(store, monkeypatch):
    monkeypatch.delenv("NEXUS_KEYSTORE_ENABLED", raising=False)
    assert "NEXUS_KEYSTORE_ENABLED" in _cli(store, "issue", "--user", "alice")[2]
    monkeypatch.setenv("NEXUS_KEYSTORE_ENABLED", "true")
    assert "NEXUS_KEYSTORE_ENABLED" not in _cli(store, "issue", "--user", "alice")[2]


def test_the_cli_names_the_database_it_changes_without_its_password(monkeypatch, store):
    assert keystore.describe_database("postgresql://nexus:s3cret@db.internal:5432/nexus") == (
        "postgresql://nexus:***@db.internal:5432/nexus"
    )
    monkeypatch.setattr(keystore, "KeyStore", lambda: store)  # the default-store path
    monkeypatch.setattr(keystore, "DATABASE_URL", "postgresql://nexus:s3cret@db.internal/nexus")
    err = io.StringIO()
    assert keystore.main(["issue", "--user", "alice"], out=io.StringIO(), err=err) == 0
    assert "database: postgresql://nexus:***@db.internal/nexus" in err.getvalue()
    assert "s3cret" not in err.getvalue()


def test_cli_reports_a_store_that_will_not_open_without_details(monkeypatch):
    def refuse():
        raise RuntimeError("could not connect to postgres://user:s3cret@db.internal/nexus")

    monkeypatch.setattr(keystore, "KeyStore", refuse)
    err = io.StringIO()
    assert keystore.main(["list"], out=io.StringIO(), err=err) == 1
    assert "RuntimeError" in err.getvalue() and "s3cret" not in err.getvalue()
