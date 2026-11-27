import pytest
from sqlalchemy.engine import make_url

from app.core import database as db


def opts(raw):
    return db.build_engine_options(db.normalize_database_url(raw))


def test_sqlite_options_are_unchanged_for_local_development():
    url, kwargs = opts("sqlite:///./nexus.db")
    assert url.get_backend_name() == "sqlite"
    assert kwargs["connect_args"] == {"timeout": 30, "check_same_thread": False}
    assert "pool_size" not in kwargs


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+psycopg2", "postgresql+psycopg"])
def test_postgres_spellings_map_to_psycopg3(scheme):
    url, _ = opts(f"{scheme}://u:p@ep-x.neon.tech/db?sslmode=require")
    assert url.drivername == "postgresql+psycopg"


def test_remote_host_defaults_to_tls_and_pool_is_configured():
    url, kwargs = opts("postgresql://u:p@ep-x-pooler.neon.tech/db")
    assert kwargs["connect_args"]["sslmode"] == "require"
    assert kwargs["connect_args"]["prepare_threshold"] is None  # PgBouncer transaction mode
    assert kwargs["pool_pre_ping"] is True
    assert (kwargs["pool_size"], kwargs["max_overflow"], kwargs["pool_recycle"]) == (5, 5, 300)
    assert "sslmode" not in url.query


@pytest.mark.parametrize("mode", ["disable", "allow", "prefer"])
def test_remote_host_refuses_weak_tls_modes(mode):
    with pytest.raises(ValueError, match="sslmode"):
        opts(f"postgresql://u:p@db.example.com/db?sslmode={mode}")


def test_verify_full_and_channel_binding_are_kept():
    _, kwargs = opts("postgresql://u:p@db.example.com/db?sslmode=verify-full&channel_binding=require")
    assert kwargs["connect_args"]["sslmode"] == "verify-full"
    assert kwargs["connect_args"]["channel_binding"] == "require"


def test_local_postgres_does_not_force_tls():
    _, kwargs = opts("postgresql://u:p@localhost:5432/db")
    assert "sslmode" not in kwargs["connect_args"]


def test_pool_settings_come_from_the_environment_and_are_bounded(monkeypatch):
    monkeypatch.setenv("DB_POOL_SIZE", "12")
    monkeypatch.setenv("DB_POOL_RECYCLE", "120")
    _, kwargs = opts("postgresql://u:p@db.example.com/db?sslmode=require")
    assert kwargs["pool_size"] == 12 and kwargs["pool_recycle"] == 120
    monkeypatch.setenv("DB_POOL_SIZE", "0")
    with pytest.raises(ValueError, match="DB_POOL_SIZE"):
        opts("postgresql://u:p@db.example.com/db?sslmode=require")


def test_other_backends_are_rejected():
    with pytest.raises(ValueError):
        opts("mysql://u:p@h/db")


def test_password_is_hidden_when_the_url_is_described():
    text = make_url("postgresql://user:s3cretvalue@h.example.com/db").render_as_string(hide_password=True)
    assert "s3cretvalue" not in text


def test_auto_create_defaults(monkeypatch):
    monkeypatch.delenv("NEXUS_DB_AUTO_CREATE", raising=False)
    monkeypatch.setattr(db, "_URL", make_url("sqlite:///x.db"))
    assert db.auto_create_enabled() is True
    monkeypatch.setattr(db, "_URL", make_url("postgresql+psycopg://u:p@h/db"))
    assert db.auto_create_enabled() is False
    monkeypatch.setenv("NEXUS_DB_AUTO_CREATE", "true")
    assert db.auto_create_enabled() is True


def test_init_db_survives_a_concurrent_create_table_race(monkeypatch):
    """Two workers starting together can both try to create a table; the loser must retry, not crash."""
    from sqlalchemy.exc import OperationalError

    from app.core import database

    calls = {"n": 0}

    def racy(bind=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OperationalError("CREATE TABLE x", {}, Exception("table x already exists"))

    monkeypatch.setattr(database.Base.metadata, "create_all", racy)
    monkeypatch.setattr(database, "auto_create_enabled", lambda: True)
    database.init_db()
    assert calls["n"] == 3


def test_init_db_still_raises_real_errors(monkeypatch):
    from sqlalchemy.exc import OperationalError

    from app.core import database

    def broken(bind=None):
        raise OperationalError("CREATE TABLE x", {}, Exception("disk I/O error"))

    monkeypatch.setattr(database.Base.metadata, "create_all", broken)
    monkeypatch.setattr(database, "auto_create_enabled", lambda: True)
    import pytest

    with pytest.raises(OperationalError):
        database.init_db()


def test_private_network_hosts_are_an_explicit_narrow_allow_list(monkeypatch):
    from app.core.database import build_engine_options, normalize_database_url

    url = normalize_database_url("postgresql://u:p@db:5432/nexus")
    monkeypatch.delenv("NEXUS_PRIVATE_NETWORK_HOSTS", raising=False)
    assert build_engine_options(url)[1]["connect_args"]["sslmode"] == "require"  # a non-local host still gets TLS
    monkeypatch.setenv("NEXUS_PRIVATE_NETWORK_HOSTS", "db")
    assert "sslmode" not in build_engine_options(url)[1]["connect_args"]  # listed: private network, no TLS forced
    other = normalize_database_url("postgresql://u:p@ep-x.neon.tech/nexus")
    assert build_engine_options(other)[1]["connect_args"]["sslmode"] == "require"  # public hosts are unaffected
    bad = normalize_database_url("postgresql://u:p@ep-x.neon.tech/nexus?sslmode=disable")
    import pytest

    with pytest.raises(ValueError):
        build_engine_options(bad)
