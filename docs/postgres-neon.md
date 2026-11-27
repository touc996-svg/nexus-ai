# PostgreSQL (Neon) support

NEXUS AI uses SQLite by default for local development and PostgreSQL when `DATABASE_URL` points at one. Neon is the
intended managed provider. **Nothing here creates an account or resources; you do that in Neon's console.** The
Neon-specific behaviour below follows Neon's documented connection options but was **not tested against a real Neon
project**; it was tested against a local `postgres:16` container.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./nexus.db` | `postgresql://USER:PASSWORD@HOST/DB?sslmode=require` (also `postgres://` and `postgresql+psycopg://`). Keep it in a secret manager or the host's env settings. Never commit it. |
| `NEXUS_DB_AUTO_CREATE` | `true` for SQLite, `false` for PostgreSQL | When false, the schema is managed only by Alembic. |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | 5 / 5 | Per-process pool (PostgreSQL). Total connections = workers x (size + overflow); keep it under your Neon plan limit. |
| `DB_POOL_TIMEOUT` / `DB_POOL_RECYCLE` / `DB_CONNECT_TIMEOUT` | 30 / 300 / 10 s | Recycle is short because Neon suspends idle compute and drops connections. `pool_pre_ping` is on. |

Remote hosts must use TLS: `sslmode` defaults to `require`; `disable`, `allow` and `prefer` are refused at startup.
Use `verify-full` where you can supply the CA. Neon's `-pooler` endpoint (PgBouncer, transaction mode) is supported:
server-side prepared statements are disabled. Use the **direct** (non-pooler) endpoint for running migrations if Neon
advises it for DDL.

## First-time setup / migrating

```bash
export DATABASE_URL='postgresql://USER:PASSWORD@HOST/DB?sslmode=require'   # from the Neon console
alembic current          # shows nothing on an empty database
alembic upgrade head     # creates the tables; safe to run again
alembic check            # "No new upgrade operations detected" when models and schema agree
```

Run migrations as a deploy step before starting the new app version, using the same `DATABASE_URL` as the server.
`python -m app.security.keystore` prints which database (password hidden) it is changing.

**Moving existing SQLite data:** there is no automated copy. Memory, profiles, activity and stored keys in `nexus.db` are
not transferred. For a demo or fresh deployment start empty and re-issue keys. If you need the data, export the tables
from SQLite and load them into Postgres yourself, then reset each `id` sequence (`SELECT setval(...)`). Not implemented
or tested here.

## Rollback considerations

- **Application rollback:** unset `DATABASE_URL` to return to local SQLite (development only; data is separate).
- **Schema rollback:** `alembic downgrade base` on the baseline **drops all four tables and their data**. Take a Neon
  branch or `pg_dump` first. Prefer restoring from a Neon branch/point-in-time restore over downgrading in production.
- Future migrations must be additive first (add columns nullable, backfill, then tighten) so the previous app version
  keeps working during a rollback.
- The baseline matches the existing models; a database created earlier by `create_all` can be adopted with
  `alembic stamp 0001` (after confirming `alembic check` reports no drift).

## Tests

```bash
python -m pytest -q tests/test_database_config.py tests/test_migrations.py        # SQLite, no server needed
docker run -d --name pgtest -e POSTGRES_PASSWORD=localtestonly -e POSTGRES_DB=nexus_t -p 55432:5432 postgres:16-alpine
NEXUS_TEST_POSTGRES_URL=postgresql://postgres:localtestonly@localhost:55432/nexus_t python -m pytest -q tests/test_migrations.py
```

The PostgreSQL tests drop and recreate tables: use a throwaway database only. CI runs them against a `postgres:16`
service container.
