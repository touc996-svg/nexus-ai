"""Is the database schema at the Alembic head this code expects?

Used by readiness so a new version is never routed traffic while its migrations have not run. SQLite development
databases are created from the models and carry no ``alembic_version`` table; for those the check reports ``not_managed``
and does not block.
"""

import functools
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

ROOT = Path(__file__).resolve().parents[2]


@functools.lru_cache(maxsize=1)
def expected_head() -> str | None:
    ini = ROOT / "alembic.ini"
    if not ini.is_file() or not (ROOT / "migrations").is_dir():
        return None
    config = Config(str(ini))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return ScriptDirectory.from_config(config).get_current_head()


def schema_status(engine) -> dict:
    """``{"status": "current" | "outdated" | "not_managed" | "unknown", ...}``; raises only on connection failure."""
    head = expected_head()
    with engine.connect() as connection:
        if "alembic_version" not in inspect(connection).get_table_names():
            return {"status": "not_managed" if engine.dialect.name == "sqlite" else "outdated", "expected": head}
        current = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    if head is None:
        return {"status": "unknown", "current": current}
    return {"status": "current" if current == head else "outdated", "current": current, "expected": head}
