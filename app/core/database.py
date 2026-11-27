"""SQLAlchemy engine configuration: SQLite for local development, PostgreSQL (for example Neon) for deployment.

``DATABASE_URL`` selects the database. It is read from the environment only; no credential lives in the repository.

* Unset or ``sqlite:///...``: local SQLite file, tables created automatically (development default).
* ``postgres://``, ``postgresql://`` or ``postgresql+psycopg://``: PostgreSQL through psycopg 3. The scheme is normalised
  to ``postgresql+psycopg://``. Remote hosts must use TLS (``sslmode`` of require, verify-ca or verify-full); the default
  for a remote host is ``require``. Schema changes are applied with Alembic (``alembic upgrade head``), not at startup,
  unless ``NEXUS_DB_AUTO_CREATE=true``.

Pool settings (PostgreSQL only): ``DB_POOL_SIZE`` (5), ``DB_MAX_OVERFLOW`` (5), ``DB_POOL_TIMEOUT`` seconds (30),
``DB_POOL_RECYCLE`` seconds (300, shorter than the idle timeout of serverless Postgres), ``DB_CONNECT_TIMEOUT`` seconds (10).
"""

import os
import time

from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import declarative_base, sessionmaker

DEFAULT_DATABASE_URL = "sqlite:///./nexus.db"
_LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1"}
_SAFE_SSLMODES = {"require", "verify-ca", "verify-full"}
_TRUTHY = {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def normalize_database_url(raw: str) -> URL:
    """Parse ``raw`` and map PostgreSQL URL spellings onto the psycopg 3 driver."""
    url = make_url(raw)
    if url.drivername in {"postgres", "postgresql", "postgresql+psycopg2"}:
        url = url.set(drivername="postgresql+psycopg")
    return url


def is_postgres(url: URL) -> bool:
    return url.get_backend_name() == "postgresql"


def is_sqlite(url: URL) -> bool:
    return url.get_backend_name() == "sqlite"


def build_engine_options(url: URL) -> tuple[URL, dict]:
    """Return ``(url, create_engine kwargs)`` for the backend. Raises ValueError for an unsafe PostgreSQL setup."""
    if is_sqlite(url):
        return url, {"connect_args": {"timeout": 30, "check_same_thread": False}, "pool_pre_ping": True}

    if not is_postgres(url):
        raise ValueError("DATABASE_URL must be a sqlite or postgresql URL")

    query = dict(url.query)
    # Neon-style parameters that libpq understands but psycopg's URL parser does not need to receive twice.
    channel_binding = query.pop("channel_binding", None)
    sslmode = query.pop("sslmode", None)
    remote = (url.host or "") not in _LOCAL_HOSTS
    if sslmode is None and remote:
        sslmode = "require"
    if remote and sslmode not in _SAFE_SSLMODES:
        raise ValueError("A remote PostgreSQL host requires sslmode=require, verify-ca or verify-full")

    connect_args: dict = {"connect_timeout": _env_int("DB_CONNECT_TIMEOUT", 10, 1, 120)}
    if sslmode:
        connect_args["sslmode"] = sslmode
    if channel_binding:
        connect_args["channel_binding"] = channel_binding
    # Neon's pooled endpoint runs PgBouncer in transaction mode, which cannot hold server-side prepared statements.
    connect_args["prepare_threshold"] = None

    url = url.set(query=query)
    return url, {
        "connect_args": connect_args,
        "pool_pre_ping": True,
        "pool_size": _env_int("DB_POOL_SIZE", 5, 1, 100),
        "max_overflow": _env_int("DB_MAX_OVERFLOW", 5, 0, 100),
        "pool_timeout": _env_int("DB_POOL_TIMEOUT", 30, 1, 300),
        "pool_recycle": _env_int("DB_POOL_RECYCLE", 300, 30, 3600),
    }


DATABASE_URL = os.getenv("DATABASE_URL") or DEFAULT_DATABASE_URL
_URL, _ENGINE_OPTIONS = build_engine_options(normalize_database_url(DATABASE_URL))

engine = create_engine(_URL, **_ENGINE_OPTIONS)

if is_sqlite(_URL):
    @event.listens_for(engine, "connect")
    def _configure_sqlite(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def auto_create_enabled() -> bool:
    """SQLite creates missing tables at startup. PostgreSQL leaves the schema to Alembic unless explicitly enabled."""
    raw = os.getenv("NEXUS_DB_AUTO_CREATE", "").strip().lower()
    if raw:
        return raw in _TRUTHY
    return is_sqlite(_URL)


def init_db():
    if not auto_create_enabled():
        return
    # Several workers starting together can each see a table as missing and then collide on CREATE TABLE. The loser's
    # error is harmless: the table exists now, so a second pass (which re-checks) succeeds.
    for attempt in range(4):
        try:
            Base.metadata.create_all(bind=engine)
            return
        except (OperationalError, ProgrammingError) as exc:
            if "already exists" not in str(exc).lower() or attempt == 3:
                raise
            time.sleep(0.05 * (attempt + 1))
