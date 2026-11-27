"""Alembic environment. The database is taken from DATABASE_URL, exactly as the application reads it."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

import app.core.activity  # noqa: F401  (registers AgentActivity)
import app.core.alerts  # noqa: F401  (registers AlertStateRow, AlertEventRow)
import app.core.metrics_store  # noqa: F401  (registers the metric_* tables)
import app.core.memory  # noqa: F401  (registers MemoryMessage, UserProfile)
import app.security.keystore  # noqa: F401  (registers ApiKey)
from app.core.database import _ENGINE_OPTIONS, _URL, Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=_URL.render_as_string(hide_password=False), target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _migrate(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    supplied = config.attributes.get("connection")  # tests and embedding callers pass an open connection
    if supplied is not None:
        _migrate(supplied)
        return
    engine = create_engine(_URL, **_ENGINE_OPTIONS)
    try:
        with engine.connect() as connection:
            _migrate(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
