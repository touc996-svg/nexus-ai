"""Alembic baseline: SQLite always; a real PostgreSQL server when NEXUS_TEST_POSTGRES_URL is set (skipped otherwise)."""

import os

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from app.core.database import Base, build_engine_options, normalize_database_url

EXPECTED = {"memory_messages", "user_profiles", "agent_activity", "api_keys"}
POSTGRES_URL = os.getenv("NEXUS_TEST_POSTGRES_URL", "")


def _config(connection):
    cfg = Config("alembic.ini")
    cfg.attributes["connection"] = connection
    return cfg


def _drift(connection):
    return compare_metadata(MigrationContext.configure(connection, opts={"compare_type": True}), Base.metadata)


def _roundtrip(engine):
    with engine.begin() as connection:
        cfg = _config(connection)
        command.upgrade(cfg, "head")
        command.upgrade(cfg, "head")  # repeatable: a second run changes nothing
        assert EXPECTED <= set(inspect(connection).get_table_names())
        assert _drift(connection) == []  # migrations match the models
    with engine.begin() as connection:
        command.downgrade(_config(connection), "base")
        assert not EXPECTED & set(inspect(connection).get_table_names())


def test_baseline_migration_on_sqlite(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    _roundtrip(engine)


@pytest.mark.skipif(not POSTGRES_URL, reason="set NEXUS_TEST_POSTGRES_URL to a throwaway PostgreSQL database")
def test_baseline_migration_on_postgres():
    url, kwargs = build_engine_options(normalize_database_url(POSTGRES_URL))
    engine = create_engine(url, **kwargs)
    try:
        _roundtrip(engine)
    finally:
        engine.dispose()


@pytest.mark.skipif(not POSTGRES_URL, reason="set NEXUS_TEST_POSTGRES_URL to a throwaway PostgreSQL database")
def test_application_stores_work_on_postgres():
    from app.core.memory import NexusMemory
    from app.security.keystore import KeyStore

    url, kwargs = build_engine_options(normalize_database_url(POSTGRES_URL))
    engine = create_engine(url, **kwargs)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    from sqlalchemy.orm import sessionmaker
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    try:
        mem = NexusMemory(session_factory=factory, initialize=False)
        mem.add_message("s1", "user", "hello")
        assert mem.get_history("s1")[0]["content"] == "hello"
        mem.update_profile("u1", {"goals": ["learn"]})
        assert mem.get_profile("u1")["profile"]["goals"] == ["learn"]
        assert mem.health_check()["status"] == "online"
        store = KeyStore(session_factory=factory, initialize=False)
        issued = store.issue("alice", role="user", ttl_days=1)
        assert store.authenticate(issued.key).user_id == "alice"
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
