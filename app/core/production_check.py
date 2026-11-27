"""Fail-fast validation of a production configuration (``APP_ENV=production`` and not demo mode).

Called from the application's start-up. Every problem is collected and reported together, by variable name only; no value
is ever printed. Development and demo mode are not affected.
"""

import os
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url

MIN_SECRET_CHARS = 32
_TRUTHY = {"1", "true", "yes", "on"}


class ProductionConfigError(RuntimeError):
    pass


def is_production(environ=None) -> bool:
    env = os.environ if environ is None else environ
    demo = (env.get("NEXUS_DEMO_MODE") or "").strip().lower() in _TRUTHY
    return (env.get("APP_ENV") or "").strip().lower() == "production" and not demo


def production_problems(environ=None) -> list[str]:
    env = os.environ if environ is None else environ
    if not is_production(env):
        return []
    problems = []

    def get(name):
        return (env.get(name) or "").strip()

    key = get("NEXUS_API_KEY")
    if len(key) < MIN_SECRET_CHARS:
        problems.append(f"NEXUS_API_KEY must be set and at least {MIN_SECRET_CHARS} characters")

    url = get("DATABASE_URL")
    if not url:
        problems.append("DATABASE_URL must be set (PostgreSQL)")
    else:
        try:
            backend = make_url(url).get_backend_name()
        except Exception:
            backend = ""
            problems.append("DATABASE_URL is not a valid database URL")
        if backend == "sqlite" and get("NEXUS_ALLOW_SQLITE_IN_PRODUCTION").lower() not in _TRUTHY:
            problems.append("DATABASE_URL must be PostgreSQL in production (SQLite is not safe across workers or restarts)")
        elif backend and backend not in {"sqlite", "postgresql"}:
            problems.append("DATABASE_URL must be a PostgreSQL URL")

    qdrant = get("QDRANT_URL")
    if not qdrant:
        problems.append("QDRANT_URL must be set explicitly")
    elif urlsplit(qdrant).scheme not in {"http", "https"}:
        problems.append("QDRANT_URL must be an http(s) URL")

    origins = [o.strip() for o in get("NEXUS_CORS_ORIGINS").split(",") if o.strip()]
    if "*" in origins:
        problems.append("NEXUS_CORS_ORIGINS must not contain '*'")
    if not get("NEXUS_ALLOWED_HOSTS"):
        problems.append("NEXUS_ALLOWED_HOSTS must list the host names this service answers to")
    if get("NEXUS_REQUIRE_API_KEY").lower() in {"0", "false", "no", "off"}:
        problems.append("NEXUS_REQUIRE_API_KEY must not be disabled in production")
    return problems


def validate_production_settings(environ=None) -> None:
    problems = production_problems(environ)
    if problems:
        raise ProductionConfigError("Invalid production configuration: " + "; ".join(problems))
