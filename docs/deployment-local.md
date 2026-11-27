# Production-like deployment on one machine (Docker Compose)

A rehearsal of the production shape, **not a deployment**: no cloud, no real domain, no paid service. Everything runs on
your machine; only Caddy listens, on `127.0.0.1:8443`.

```
browser/curl --HTTPS (Caddy local CA)--> caddy --edge net--> api (python -m app.serve: uvicorn, 2 workers, non-root, read-only fs)
                                                              |  backend net (internal: no outside route)
                                                              +--> db      PostgreSQL 16 (volume pgdata)
                                                              +--> qdrant  Qdrant v1.20.0 (volume qdrant_storage, API key)
migrate (one-shot): alembic upgrade head, must succeed before api starts
```

| Service | Image (pinned) | Notes |
|---|---|---|
| `caddy` | `caddy:2.10.2-alpine` | TLS with Caddy's own local CA (`tls internal`); credential-free JSON logs; only published port |
| `api` | built from `Dockerfile` (`python:3.11.18-slim-trixie`) | `UVICORN_WORKERS=2`, 35 s stop grace, caps dropped, `no-new-privileges`, read-only root, 2 CPU / 1 GB |
| `migrate` | same image | `alembic upgrade head`, exits 0; `api` waits for it |
| `db` | `postgres:16.16-alpine` | no host port, internal network only, 1 CPU / 512 MB |
| `qdrant` | `qdrant/qdrant:v1.20.0` | no host port, internal network only, API key required, telemetry off, 1 CPU / 1 GB |

## Start, check, stop
```bash
./scripts/deploy_local_secrets.sh                      # random throwaway secrets in deploy/secrets/ (git-ignored, never printed)
export NEXUS_READINESS_REQUIRE_LLM=false               # only if no model server is running on this machine (see below)
docker compose -f deploy/compose.yaml up -d --build    # migrate runs first, then api, then caddy
docker compose -f deploy/compose.yaml ps
```
Trust the local CA for curl (certificate validation stays **on**):
```bash
docker compose -f deploy/compose.yaml exec -T caddy cat /data/caddy/pki/authorities/local/root.crt > /tmp/caddy-root.crt
curl --cacert /tmp/caddy-root.crt https://localhost:8443/health/ready
curl --cacert /tmp/caddy-root.crt -H "X-API-Key: $(cat deploy/secrets/nexus_api_key)" https://localhost:8443/system/alerts
```
Stop: `docker compose -f deploy/compose.yaml stop` (graceful; each worker flushes its metrics). Remove everything including
data: `docker compose -f deploy/compose.yaml down -v`. Automated rehearsal of all of the above: `./scripts/compose_smoke.sh`.

## Configuration
Non-secret settings are environment variables (defaults in `deploy/compose.yaml`; override from your shell or `deploy/.env`,
which is git-ignored): `NEXUS_ALLOWED_HOSTS` (default `localhost,127.0.0.1`), `NEXUS_CORS_ORIGINS`, `UVICORN_WORKERS`,
`NEXUS_RATE_LIMIT_PER_MINUTE`, `NEXUS_HTTPS_PORT`, `OLLAMA_BASE_URL` (default `http://host.docker.internal:11434/v1`, a model
server on your machine), `NEXUS_MODEL`, `EMBEDDING_MODEL`, `NEXUS_READINESS_REQUIRE_LLM`.

**Secrets** are files mounted at `/run/secrets/*`; the application reads `NAME_FILE=/run/secrets/...` for `DATABASE_URL`,
`NEXUS_API_KEY`, `NEXUS_API_KEYS`, `NEXUS_JWT_SECRETS` and `QDRANT_API_KEY` (an explicitly set `NAME` wins). They never
appear in `docker inspect`, the image, the repository or logs (checked by the smoke script). The generator keeps
`deploy/secrets` mode 700 and files 644 so the container users can read them while other host users cannot enter the directory.
Real deployments supply the same variables from their platform's secret store (environment variables or mounted files).

## Production configuration is validated at start-up
With `APP_ENV=production` and not demo mode, the API refuses to start (naming variables, never values) unless:
`NEXUS_API_KEY` ≥ 32 characters; `DATABASE_URL` is PostgreSQL (SQLite needs `NEXUS_ALLOW_SQLITE_IN_PRODUCTION=true`);
`QDRANT_URL` is set; `NEXUS_ALLOWED_HOSTS` is set; CORS has no `*`; `NEXUS_REQUIRE_API_KEY` is not disabled.
A remote Qdrant still needs `https://` and its key; a remote PostgreSQL still needs `sslmode` require/verify-*.
`NEXUS_PRIVATE_NETWORK_HOSTS` (here `db,qdrant`) exempts only the listed names from those two TLS rules, because they are
services on an `internal: true` network with no route outside. **Never list a public or managed host** (Neon, Qdrant Cloud).

## Health, startup and shutdown
- `GET /health/live`: the process serves requests. Independent of dependencies. Used by the container health check and Caddy.
- `GET /health/ready`: 200 only if PostgreSQL answers, the schema is at the Alembic head, Qdrant is healthy, and (unless
  `NEXUS_READINESS_REQUIRE_LLM=false`) the model server has the models. Otherwise 503. The response carries statuses only: no
  URLs, model names, hostnames or error messages. Use this for orchestrator/load-balancer routing.
- Start order is enforced by Compose: `db` healthy -> `migrate` succeeds -> `qdrant` healthy -> `api` healthy -> `caddy`.
  If a migration fails, `api` never starts. Metric flush failures (for example tables missing) are logged and never stop serving.
- Shutdown: SIGTERM -> uvicorn drains in-flight requests (up to 25 s) -> each worker's lifespan shutdown flushes pending
  metrics. The smoke test measures a clean exit code 0 in about a second.

## Migrations
Every start runs `alembic upgrade head` through the `migrate` service (idempotent). Manually:
`docker compose -f deploy/compose.yaml run --rm migrate alembic current` (or `upgrade head`, `check`). Take a database backup
before a migration in any shared environment; `downgrade` is destructive (see `docs/postgres-neon.md`).

## Reverse proxy and client addresses
Caddy sets `X-Forwarded-For` for each request and, with no trusted proxies configured, replaces anything the client sent, so the
API sees exactly one hop: `NEXUS_TRUSTED_PROXY_HOPS=1` (see `docs/trusted-proxy.md`). **Measured on this stack:** 12 requests
with a different forged `X-Forwarded-For` each were limited after the 6th (limit set to 6/minute): spoofing does not create new
rate-limit buckets. This measures *this* Caddy topology only; **it says nothing about Render or any other platform**, whose hop
count must still be measured with the procedure in `docs/trusted-proxy.md`. The launcher `app/serve.py` runs uvicorn with proxy-header rewriting off (`proxy_headers=False`) deliberately, and sets `TCP_NODELAY` on the shared listening socket (without it, multi-worker uvicorn adds ~42 ms to every keep-alive request; see `docs/LOAD_TEST_REPORT.md`):
only the application interprets the header.

## Security properties (verified by `scripts/compose_smoke.sh` and `tests/test_deployment_readiness.py`)
Non-root API (uid 999), read-only root filesystem, all capabilities dropped (Caddy keeps only `NET_BIND_SERVICE`, Postgres the
five its entrypoint needs), `no-new-privileges`; `db`, `qdrant`, `migrate` on an internal-only network; Qdrant needs its API
key; no host port except Caddy on loopback; Caddy logs strip `Authorization`, `X-Api-Key`, `Cookie` and token query parameters
(this includes Caddy's error log, which first leaked `X-Api-Key` in testing and is fixed); authentication, rate limits, tenant
isolation and the security suite are unchanged and still run in CI.

## Troubleshooting
| Symptom | Likely cause / action |
|---|---|
| `api` never starts, logs say `Invalid production configuration: ...` | a variable named in the message is missing or unsafe; fix it, `up -d` again |
| `migrate` exits non-zero | `docker compose -f deploy/compose.yaml logs migrate`; the database is unreachable or a migration failed; `api` stays down by design |
| `/health/ready` is 503, others fine | the body names the failing check (`llm`, `rag`, `memory`, `schema`). No model server: set `NEXUS_READINESS_REQUIRE_LLM=false` or start Ollama and pull the models |
| Caddy answers 502/503 | `api` is down or restarting: `docker compose ps`, `logs api` |
| curl: certificate verify failed | pass `--cacert` with Caddy's root certificate (above); do not use `-k` |
| 400 "Invalid host header" | the `Host` is not in `NEXUS_ALLOWED_HOSTS` |
| `host.docker.internal` unreachable (Linux) | the compose file adds the host-gateway mapping; the model server must listen on an address the container can reach |
| Port 8443 busy | set `NEXUS_HTTPS_PORT` |

## What this does NOT give you (still required before a real production deployment)
1. A real platform, domain and publicly trusted certificate (Caddy `tls internal` is local only); DNS and firewall rules.
2. A managed or hardened PostgreSQL with TLS (`sslmode=verify-full`), backups, point-in-time recovery and a tested restore; a
   decision on migrations as a gated deploy step.
3. Qdrant with TLS, API key and backups (or Qdrant Cloud); the local stack talks to it over a private network without TLS.
4. A secret store, rotation policy and a key-management plan for `NEXUS_API_KEY`, JWT secrets and database credentials.
5. A measured `NEXUS_TRUSTED_PROXY_HOPS` for the real platform; shared rate limiting if more than one instance (limits are per process).
6. Log shipping and someone watching `/system/alerts` and the alert logs; alert thresholds tuned on real traffic.
7. Image scanning and base-image update cadence (Trivy runs in CI; the base image is pinned and must be bumped deliberately).
8. Capacity and load testing (Task #4), a real model server sized for the traffic, and an incident/rollback runbook.
