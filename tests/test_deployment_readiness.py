"""Deployment preparation: secret files, production validation, readiness, schema revision, host allow-list."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.core.production_check import (
    ProductionConfigError,
    production_problems,
    validate_production_settings,
)
from app.core.secret_files import SECRET_VARIABLES, SecretFileError, load_secret_files

ROOT = Path(__file__).resolve().parents[1]
GOOD = {
    "APP_ENV": "production", "NEXUS_API_KEY": "k" * 40, "DATABASE_URL": "postgresql://u:p@db:5432/n",
    "QDRANT_URL": "http://qdrant:6333", "NEXUS_ALLOWED_HOSTS": "localhost",
}


# ---- *_FILE secrets ----------------------------------------------------------------------------------------------

def test_secret_file_fills_the_variable_and_strips_whitespace(tmp_path):
    f = tmp_path / "key"
    f.write_text("  s3cret-value-123 \n")
    env = {"NEXUS_API_KEY_FILE": str(f)}
    assert load_secret_files(env) == ["NEXUS_API_KEY"] and env["NEXUS_API_KEY"] == "s3cret-value-123"


def test_an_explicit_variable_wins_over_the_file(tmp_path):
    f = tmp_path / "key"
    f.write_text("from-file")
    env = {"NEXUS_API_KEY": "explicit", "NEXUS_API_KEY_FILE": str(f)}
    assert load_secret_files(env) == [] and env["NEXUS_API_KEY"] == "explicit"


@pytest.mark.parametrize("setup", ["missing", "directory", "empty", "huge", "binary"])
def test_bad_secret_files_fail_without_revealing_content(tmp_path, setup):
    target = tmp_path / "s"
    if setup == "directory":
        target.mkdir()
    elif setup == "empty":
        target.write_text("  \n")
    elif setup == "huge":
        target.write_text("x" * 9000)
    elif setup == "binary":
        target.write_bytes(b"\xff\xfe\x00\x80")
    env = {"DATABASE_URL_FILE": str(target)}
    with pytest.raises(SecretFileError) as info:
        load_secret_files(env)
    assert "DATABASE_URL_FILE" in str(info.value) and "xxxx" not in str(info.value)
    assert "DATABASE_URL" not in env


def test_only_allow_listed_variables_are_loaded(tmp_path):
    f = tmp_path / "x"
    f.write_text("value")
    env = {"PATH_FILE": str(f), "NEXUS_DEMO_MODE_FILE": str(f)}
    assert load_secret_files(env) == [] and "PATH" not in env
    assert set(SECRET_VARIABLES) >= {"DATABASE_URL", "NEXUS_API_KEY", "QDRANT_API_KEY", "NEXUS_JWT_SECRETS"}


def test_package_import_loads_secret_files(tmp_path):
    f = tmp_path / "k"
    f.write_text("a-file-supplied-api-key-0123456789abcdef")
    env = {**os.environ, "NEXUS_API_KEY_FILE": str(f), "NEXUS_DEMO_MODE": "true"}
    env.pop("NEXUS_API_KEY", None)
    out = subprocess.run([sys.executable, "-c", "import app, os; print(os.environ['NEXUS_API_KEY'][:6])"], env=env, cwd=ROOT,
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "a-file"


# ---- production validation ---------------------------------------------------------------------------------------

def test_a_complete_production_configuration_passes():
    assert production_problems(GOOD) == []
    validate_production_settings(GOOD)


@pytest.mark.parametrize("remove", ["NEXUS_API_KEY", "DATABASE_URL", "QDRANT_URL", "NEXUS_ALLOWED_HOSTS"])
def test_each_missing_required_setting_is_reported_by_name(remove):
    env = {k: v for k, v in GOOD.items() if k != remove}
    with pytest.raises(ProductionConfigError) as info:
        validate_production_settings(env)
    assert remove in str(info.value) and GOOD["NEXUS_API_KEY"] not in str(info.value)


@pytest.mark.parametrize("change,expected", [
    ({"NEXUS_API_KEY": "short"}, "NEXUS_API_KEY"),
    ({"DATABASE_URL": "sqlite:///./nexus.db"}, "PostgreSQL"),
    ({"DATABASE_URL": "mysql://u:p@h/db"}, "PostgreSQL"),
    ({"NEXUS_CORS_ORIGINS": "https://a.example,*"}, "NEXUS_CORS_ORIGINS"),
    ({"NEXUS_REQUIRE_API_KEY": "false"}, "NEXUS_REQUIRE_API_KEY"),
    ({"QDRANT_URL": "ftp://q"}, "QDRANT_URL"),
])
def test_unsafe_production_values_are_refused(change, expected):
    assert any(expected in p for p in production_problems({**GOOD, **change}))


def test_sqlite_needs_an_explicit_opt_in_and_demo_or_dev_are_exempt():
    assert production_problems({**GOOD, "DATABASE_URL": "sqlite:///x.db", "NEXUS_ALLOW_SQLITE_IN_PRODUCTION": "true"}) == []
    assert production_problems({"APP_ENV": "production", "NEXUS_DEMO_MODE": "true"}) == []
    assert production_problems({"APP_ENV": "development"}) == []
    assert production_problems({}) == []


def test_the_application_refuses_to_start_on_a_bad_production_configuration(monkeypatch):
    from app.main import app

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("NEXUS_DEMO_MODE", "false")
    for name in ("NEXUS_ALLOWED_HOSTS", "QDRANT_URL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ProductionConfigError), TestClient(app):
        pass


# ---- readiness ---------------------------------------------------------------------------------------------------

@pytest.fixture
def ready_client(monkeypatch):
    from app.api import health

    monkeypatch.delenv("NEXUS_DEMO_MODE", raising=False)
    monkeypatch.delenv("NEXUS_READINESS_REQUIRE_LLM", raising=False)
    state = {
        "llm": {"status": "online", "chat_model_available": True, "embedding_model_available": True,
                "available_models": ["secret-model:1b"], "chat_model": "internal-name"},
        "rag": {"status": "healthy", "qdrant": "online", "collection": "internal-collection"},
        "memory": {"status": "online", "storage": "sql"},
        "schema": {"status": "current", "current": "0002", "expected": "0002"},
    }
    monkeypatch.setattr("app.core.llm.get_ollama_health", lambda timeout=3.0: state["llm"])
    monkeypatch.setattr("app.services.rag.rag.health_check", lambda: state["rag"])
    monkeypatch.setattr("app.core.memory.memory.health_check", lambda: state["memory"])
    monkeypatch.setattr("app.core.schema_check.schema_status", lambda engine: state["schema"])
    api = FastAPI()
    api.include_router(health.router)
    return TestClient(api), state


def test_ready_when_every_dependency_is_ready_and_nothing_internal_is_exposed(ready_client):
    client, _ = ready_client
    response = client.get("/health/ready")
    assert response.status_code == 200 and response.json()["status"] == "ready"
    text = response.text
    for secret in ("secret-model", "internal-name", "internal-collection", "0002"):
        assert secret not in text


@pytest.mark.parametrize("key,value,name", [
    ("rag", {"status": "degraded", "qdrant": "offline:ConnectError"}, "qdrant"),
    ("memory", {"status": "offline"}, "database"),
    ("schema", {"status": "outdated", "current": None, "expected": "0002"}, "schema"),
    ("llm", {"status": "offline", "error_type": "ConnectError"}, "llm"),
])
def test_each_dependency_failure_makes_readiness_503(ready_client, key, value, name):
    client, state = ready_client
    state[key] = value
    response = client.get("/health/ready")
    assert response.status_code == 503 and response.json()["status"] == "not_ready"
    assert client.get("/health/live").status_code == 200  # liveness is independent of dependencies


def test_the_model_server_can_be_made_optional_for_readiness(ready_client, monkeypatch):
    client, state = ready_client
    state["llm"] = {"status": "offline"}
    monkeypatch.setenv("NEXUS_READINESS_REQUIRE_LLM", "false")
    assert client.get("/health/ready").status_code == 200


def test_a_raising_dependency_is_a_503_not_a_500(ready_client, monkeypatch):
    client, _ = ready_client

    def boom():
        raise RuntimeError("postgres://user:hunter2@db/x unreachable")

    monkeypatch.setattr("app.services.rag.rag.health_check", boom)
    response = client.get("/health/ready")
    assert response.status_code == 503 and "hunter2" not in response.text


# ---- schema revision ---------------------------------------------------------------------------------------------

def test_schema_status_on_a_migrated_unmigrated_and_stale_database(tmp_path):
    from app.core.schema_check import expected_head, schema_status

    engine = create_engine(f"sqlite:///{tmp_path / 's.db'}")
    assert schema_status(engine)["status"] == "not_managed"  # a development database made from the models
    with engine.begin() as connection:
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "migrations"))
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")
    assert schema_status(engine) == {"status": "current", "current": expected_head(), "expected": expected_head()}
    with engine.begin() as connection:
        connection.exec_driver_sql("UPDATE alembic_version SET version_num = '0001'")
    assert schema_status(engine)["status"] == "outdated"
    engine.dispose()


# ---- host allow-list ---------------------------------------------------------------------------------------------

def test_host_allow_list(monkeypatch):
    from app.security.hardening import install_security

    for name in ("NEXUS_DEMO_MODE", "APP_ENV", "NEXUS_REQUIRE_API_KEY", "NEXUS_API_KEY", "NEXUS_API_KEYS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NEXUS_ALLOWED_HOSTS", "localhost, 127.0.0.1")
    api = FastAPI()

    @api.get("/ping")
    def ping():
        return {"ok": True}

    install_security(api)
    client = TestClient(api, base_url="http://localhost")
    assert client.get("/ping").status_code == 200
    assert client.get("/ping", headers={"Host": "127.0.0.1:8010"}).status_code == 200
    assert client.get("/ping", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/ping", headers={"Host": "localhost.evil.example"}).status_code == 400


# ---- deployment files --------------------------------------------------------------------------------------------

def test_dockerfile_is_pinned_non_root_and_ships_the_migrations():
    text = (ROOT / "Dockerfile").read_text()
    from_line = next(line for line in text.splitlines() if line.startswith("FROM "))
    assert ":latest" not in from_line and any(ch.isdigit() for ch in from_line.rsplit(":", 1)[-1]) and from_line.count(".") >= 2
    assert "USER nexus" in text and "COPY --chown=nexus:nexus migrations" in text and "alembic.ini" in text
    assert 'CMD ["python", "-m", "app.serve"]' in text
    serve = (ROOT / "app/serve.py").read_text()
    assert "proxy_headers=False" in serve and "timeout_graceful_shutdown=25" in serve and "TCP_NODELAY" in serve


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not installed")
def test_compose_file_is_secure_by_construction():
    out = subprocess.run(["docker", "compose", "-f", str(ROOT / "deploy/compose.yaml"), "config", "--format", "json"],
                         capture_output=True, text=True)
    if out.returncode != 0 and "compose" in out.stderr.lower() and "not a docker command" in out.stderr.lower():
        pytest.skip("docker compose is not available")
    assert out.returncode == 0, out.stderr
    config = json.loads(out.stdout)
    services = config["services"]
    assert set(services) == {"db", "qdrant", "migrate", "api", "caddy"}
    for name, svc in services.items():
        image = svc["image"]
        assert ":" in image and not image.endswith(":latest"), f"{name} image must be pinned"
        assert svc.get("security_opt") and "no-new-privileges:true" in svc["security_opt"]
        assert name in {"db", "qdrant"} or svc.get("cap_drop") == ["ALL"]
        if name != "migrate":
            assert svc["deploy"]["resources"]["limits"]["memory"]
    published = {name: svc["ports"] for name, svc in services.items() if svc.get("ports")}
    assert list(published) == ["caddy"]
    assert all(p["host_ip"] == "127.0.0.1" for p in published["caddy"])
    assert config["networks"]["backend"]["internal"] is True
    for name in ("db", "qdrant", "migrate"):
        assert list(services[name]["networks"]) == ["backend"]
    # no credential in plain environment variables: only *_FILE pointers
    for name, svc in services.items():
        for key in svc.get("environment") or {}:
            if any(word in key for word in ("PASSWORD", "API_KEY", "SECRET", "DATABASE_URL")):
                assert key.endswith("_FILE"), f"{name}.{key} must be a *_FILE pointer"
    assert services["api"]["environment"]["NEXUS_TRUSTED_PROXY_HOPS"] == "1"
    assert services["api"]["read_only"] is True


def test_caddy_redacts_credentials_and_secrets_stay_out_of_git():
    caddy = (ROOT / "deploy/Caddyfile").read_text()
    for header in ("Authorization", "X-Api-Key", "Cookie"):
        assert f"request>headers>{header} delete" in caddy
    assert caddy.count("import redacted_json") >= 2  # the access log and the global default logger (error logs)
    gitignore = (ROOT / ".gitignore").read_text()
    assert "deploy/secrets/" in gitignore
    if shutil.which("git") is not None:  # minimal containers have no git; CI does
        tracked = subprocess.run(["git", "ls-files", "deploy/secrets"], cwd=ROOT, capture_output=True, text=True).stdout
        assert tracked.strip() == ""
