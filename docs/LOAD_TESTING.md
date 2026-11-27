# Load and performance testing

A repeatable, dependency-free load-testing kit for the production-like Compose stack (`deploy/compose.yaml`). It uses
`asyncio` + `httpx` (already installed, development-only: `loadtest/` is not copied into the application image) instead of adding
Locust. Results are in `docs/LOAD_TEST_REPORT.md` and the raw JSON in `docs/loadtest-results/`.

## What runs
Real PostgreSQL, real Qdrant, Caddy (TLS), the NEXUS API with 2 uvicorn workers, and a **fake model server**
(`loadtest/fake_llm.py`: Ollama `/api/tags`, `/api/embed`, OpenAI-style chat completions; latency, jitter, failures and hangs
set at run time through `POST /_control`). No paid model or external service is used.

## Run it
```bash
# whole suite (Docker required; builds the image, starts the stack, writes JSON, tears everything down; ~35 min)
python -m loadtest suite --duration 30 --sustained 60 --out docs/loadtest-results/my-run.json
python -m loadtest suite --only baseline rag --duration 15            # a subset
python -m loadtest.report docs/loadtest-results/my-run.json > /tmp/report.md

# one scenario against a running stack (any URL); keys are read from a file, never from the command line
./scripts/deploy_local_secrets.sh
docker compose -f deploy/compose.yaml -f deploy/compose.loadtest.yaml up -d --wait   # NEXUS_READINESS_REQUIRE_LLM=false
python -m loadtest run --scenario chat --concurrency 16 --duration 30 --api-key-file deploy/secrets/nexus_api_key --ca-cert caddy-root.crt --out run.json
```
Scenarios: `health ready chat agent team ingest ingest_docx search ratelimit mixed`. Options: `--concurrency`, `--duration`,
`--warmup`, `--users` (distinct tenants), `--seed` (deterministic request streams), `--timeout`, `--host-header`.
Suite parts (`--only`): `baseline rag llm concurrency mixed slow_llm llm_failures llm_timeouts thread_starvation qdrant
extraction ratelimit nodelay sustained`.

## Method and measurements
- **Closed loop:** each virtual user sends a request, waits for the answer, repeats. Throughput therefore reflects service time at
  that concurrency, not an offered rate. p50/p95/p99 are nearest-rank percentiles over all recorded requests.
- **Per step:** requests/s, success rate, error classes (429, 503, other 5xx, transport errors), per-5 s timeline; container CPU and
  memory (`docker stats` every 3 s); PostgreSQL counters (`pg_stat_database` deltas); server-side LLM and RAG latency from the
  application's own histograms (`/system/metrics` deltas); Qdrant latency measured directly from inside the API container.
- **Rate limits are not weakened in the repository.** The *capacity* profile sets `NEXUS_RATE_LIMIT_PER_MINUTE` and
  `NEXUS_RATE_LIMIT_PER_IDENTITY_PER_MINUTE` very high through environment variables **on the test stack only**; the `ratelimit`
  step switches back to the shipped values (120/min) and reports the 429s.
- **The load generator shares the machine** with the stack (Docker Desktop VM), so absolute numbers are indicative of this
  laptop, not of a server. Compare runs on the same host; do not extrapolate capacity.

## Limits of the kit
Closed-loop only (no fixed-arrival-rate test); the fake model returns canned text instantly compared with a real model, so LLM
cost is modelled only through the configured latency; a single run per scenario (no repetition statistics).
