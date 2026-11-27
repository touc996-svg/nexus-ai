# Load and performance test report (Task #4)

**Everything below was measured on one laptop, with a fake model server, on the local production-like Compose stack.** Numbers are
indicative of this environment, not capacity guarantees. Figures marked **(estimate)** are derived arithmetic, not measurements.
Method, scenarios and how to reproduce: `docs/LOAD_TESTING.md`. Raw data: `docs/loadtest-results/`.

## Summary

| Finding | Evidence | Status |
|---|---|---|
| Multi-worker uvicorn adds **~42 ms to every keep-alive request** after the first (Nagle + delayed ACK; listener socket has `proto` 0 so asyncio skips `TCP_NODELAY`) | loopback keep-alive p50 **42.3 / 42.2 ms** (2 rounds) with plain `uvicorn --workers 2` vs **0.51 / 0.55 ms** with `app.serve`; a plain FastAPI app with no NEXUS code shows the same | **Fixed** (`app/serve.py`) |
| Under thread-pool saturation the synchronous liveness route queued behind slow model calls; Caddy marked the API unhealthy and answered **503 to everything** | before: liveness 91 of 5664 probes succeeded (1.6%), chat 238 of 802 succeeded; after: liveness **18314 of 18314**, chat **280 of 280** | **Fixed** (`async def` liveness) |
| API container is **CPU-bound at its 2-CPU limit** for chat, search, ingest and mixed load | api CPU 172-207% avg in those steps (limit 2.0); chat c=64 reaches 51 rps at p95 2.1 s | Documented; sizing guidance below |
| `/chat` does ~12 PostgreSQL transactions (+~4 rollbacks) per request | 11313 commits and 4296 rollbacks for 975 chats | **Not fixed** (follow-up) |
| With the model server unreachable, `/chat` takes ~21 s per request (4 x timeout) but still answers 200 | 24 requests, p50 21.1 s, all 200 | **Not fixed** (follow-up: lower timeouts/retries, circuit breaker) |
| `/health/ready` is expensive (api 205% CPU at 157 rps) | readiness step | Documented; probe it sparingly |
| Qdrant outage: search fails for ~20 s, **chat keeps answering** (100% 200), readiness back 1.1 s after Qdrant restarts, `qdrant_unavailable` alert fires | qdrant_interruption step | Works as designed |
| Rate limiting, extraction concurrency limits and recovery after a spike behave as designed | 99.1% 429 under shipped limits; 2 of 146 DOCX uploads got 503 + Retry-After; p95 before/after spike 1273/1194 ms, 100% success | OK |
| **Single runs on this host are noisy**: two earlier runs of the same code showed 2-5x lower throughput and bursts of 502/503 that two later runs did not reproduce | Reproducibility section | Cause not established |

## Test environment
- Host: macOS arm64 laptop, Docker Desktop VM with 8 CPUs / 3.8 GB, **shared with 7 unrelated containers** and a host load average of about 4.9 at start. The load generator runs on the same host.
- Stack (`deploy/compose.yaml` + `deploy/compose.loadtest.yaml`): Caddy 2.10.2 (TLS, certificate validation on) -> NEXUS API (`python -m app.serve`, 2 uvicorn workers, limits 2 CPU / 1 GB) -> PostgreSQL 16.16 and Qdrant 1.20.0 (real, internal network); fake model server (`loadtest/fake_llm.py`: 200 +/- 50 ms per chat call, 10 ms per embedding, unless a step says otherwise).
- **Capacity profile**: per-address and per-identity rate limits raised to 1,000,000/min through environment variables on the test stack only (shipped default 120 is unchanged in the repository); the rate-limit step restores 120.
- Client: `loadtest/runner.py` (asyncio + httpx, closed loop), 30 s per step, 8 virtual tenants, seed 1. Sustained phases 60 s / 20 s (spike) / 60 s.

## Workload
`health` (liveness), `ready`, `chat` (intent call + profile-learning call + RAG search + memory writes), `agent` (one agent call), `team` (3 sequential agent calls + decision engine), `ingest` (2 KB TXT -> extraction subprocess -> embed -> Qdrant), `ingest_docx` (400-paragraph DOCX), `search`, `mixed` (20% health, 30% search, 30% chat, 10% ingest, 5% agent, 5% team), plus fault scenarios (slow, failing and hanging model; Qdrant stopped for 20 s; thread starvation; shipped rate limits; spike and recovery; keep-alive A/B).

## Results (final complete run: `docs/loadtest-results/local-compose-final.json`)
| step | scenario | conc. | req/s | p50 ms | p95 ms | p99 ms | errors | 429 | 503 | other 5xx | transport |
|---|---|---|---|---|---|---|---|---|---|---|---|
| health | health | 64 | 329.4 | 113.31 | 656.71 | 1254.95 | 0% | 0 | 0 | 0 | 0 |
| ready | ready | 8 | 157.6 | 42.68 | 97.64 | 142.91 | 0% | 0 | 0 | 0 | 0 |
| rag_ingest | ingest | 8 | 16.67 | 511.74 | 767.16 | 865.22 | 0% | 0 | 0 | 0 | 0 |
| rag_search | search | 16 | 174.47 | 85.47 | 171.44 | 480.12 | 0% | 0 | 0 | 0 | 0 |
| chat | chat | 16 | 32.5 | 471.94 | 682.11 | 967.22 | 0% | 0 | 0 | 0 | 0 |
| agent | agent | 16 | 73.93 | 213.43 | 259.04 | 374.21 | 0% | 0 | 0 | 0 | 0 |
| team | team | 8 | 12.83 | 629.45 | 712.2 | 741.74 | 0% | 0 | 0 | 0 | 0 |
| chat_c1 | chat | 1 | 2.1 | 471.64 | 551.87 | 617.25 | 0% | 0 | 0 | 0 | 0 |
| chat_c4 | chat | 4 | 8.8 | 456.55 | 525.74 | 603.72 | 0% | 0 | 0 | 0 | 0 |
| chat_c16 | chat | 16 | 31.6 | 468.96 | 712.23 | 1733.98 | 0% | 0 | 0 | 0 | 0 |
| chat_c64 | chat | 64 | 51.2 | 1163.34 | 2063.13 | 3062.19 | 0% | 0 | 0 | 0 | 0 |
| mixed | mixed | 24 | 56.2 | 252.27 | 1308.47 | 1603.88 | 0% | 0 | 0 | 0 | 0 |
| slow_llm | chat | 16 | 2.67 | 6163.64 | 6532.44 | 6859.69 | 0% | 0 | 0 | 0 | 0 |
| llm_failures_30pct | chat | 16 | 19.53 | 552.82 | 1755.57 | 1868.01 | 0% | 0 | 0 | 0 | 0 |
| llm_timeouts | chat | 8 | 0.53 | 21132.09 | 21278.0 | 21340.0 | 0% | 0 | 0 | 0 | 0 |
| extraction_docx | ingest_docx | 16 | 4.87 | 2245.21 | 5984.25 | 6270.13 | 1.4% | 0 | 2 | 0 | 0 |
| rate_limit_default | ratelimit | 10 | 873.03 | 5.92 | 37.74 | 66.75 | 99.1% | 25952 | 0 | 0 | 0 |
| ab1_legacy_uvicorn_cli_health | health | 64 | 122.5 | 350.26 | 1541.33 | 2370.13 | 0% | 0 | 0 | 0 | 0 |
| ab1_legacy_uvicorn_cli_search | search | 16 | 176.23 | 84.74 | 156.35 | 396.5 | 0% | 0 | 0 | 0 | 0 |
| ab1_legacy_uvicorn_cli_chat | chat | 16 | 32.1 | 489.17 | 600.97 | 774.01 | 0% | 0 | 0 | 0 | 0 |
| ab1_legacy_uvicorn_cli_mixed | mixed | 24 | 53.7 | 277.77 | 1291.52 | 1744.51 | 0% | 0 | 0 | 0 | 0 |
| ab1_app_serve_health | health | 64 | 386.47 | 95.19 | 537.15 | 1129.64 | 0% | 0 | 0 | 0 | 0 |
| ab1_app_serve_search | search | 16 | 172.07 | 86.33 | 174.44 | 421.93 | 0% | 0 | 0 | 0 | 0 |
| ab1_app_serve_chat | chat | 16 | 34.77 | 455.54 | 537.28 | 724.19 | 0% | 0 | 0 | 0 | 0 |
| ab1_app_serve_mixed | mixed | 24 | 57.43 | 239.25 | 1341.98 | 1705.34 | 0% | 0 | 0 | 0 | 0 |
| ab2_legacy_uvicorn_cli_health | health | 64 | 122.2 | 348.35 | 1536.04 | 2348.17 | 0% | 0 | 0 | 0 | 0 |
| ab2_legacy_uvicorn_cli_search | search | 16 | 161.37 | 88.02 | 178.57 | 457.9 | 0% | 0 | 0 | 0 | 0 |
| ab2_legacy_uvicorn_cli_chat | chat | 16 | 32.8 | 484.37 | 571.11 | 702.01 | 0% | 0 | 0 | 0 | 0 |
| ab2_legacy_uvicorn_cli_mixed | mixed | 24 | 61.5 | 234.4 | 1106.02 | 1629.3 | 0% | 0 | 0 | 0 | 0 |
| ab2_app_serve_health | health | 64 | 307.77 | 109.68 | 721.31 | 1341.73 | 0% | 0 | 0 | 0 | 0 |
| ab2_app_serve_search | search | 16 | 172.97 | 88.46 | 178.31 | 328.77 | 0% | 0 | 0 | 0 | 0 |
| ab2_app_serve_chat | chat | 16 | 33.6 | 465.51 | 607.64 | 802.23 | 0% | 0 | 0 | 0 | 0 |
| ab2_app_serve_mixed | mixed | 24 | 58.5 | 231.42 | 1175.76 | 1671.46 | 0% | 0 | 0 | 0 | 0 |
| sustained_steady_before | mixed | 24 | 57.18 | 245.35 | 1273.27 | 1587.83 | 0% | 0 | 0 | 0 | 0 |
| sustained_spike | mixed | 96 | 53.0 | 1370.64 | 4507.44 | 5123.97 | 0% | 0 | 0 | 0 | 0 |
| sustained_steady_after | mixed | 24 | 63.42 | 211.77 | 1193.5 | 1529.5 | 0% | 0 | 0 | 0 | 0 |

### Resource use per step (avg/max CPU %, max memory)

- **health**: caddy 11.9/19.1% cpu, 23.1 MB, api 43.1/69.7% cpu, 307.1 MB, db 0.0/0.1% cpu, 37.8 MB, qdrant 0.0/0.0% cpu, 56.5 MB, fake-llm 9.9/28.6% cpu, 41.6 MB
- **ready**: caddy 9.6/10.8% cpu, 19.1 MB, api 204.8/212.6% cpu, 317.9 MB, db 22.9/27.9% cpu, 53.2 MB, qdrant 5.6/6.2% cpu, 59.4 MB, fake-llm 25.4/43.1% cpu, 36.0 MB
- **rag_ingest**: caddy 2.6/3.9% cpu, 19.6 MB, api 201.7/210.5% cpu, 361.0 MB, db 0.3/2.1% cpu, 50.5 MB, qdrant 4.4/5.1% cpu, 64.8 MB, fake-llm 4.3/4.9% cpu, 38.2 MB
- **rag_search**: caddy 11.0/11.9% cpu, 29.7 MB, api 207.3/209.2% cpu, 400.5 MB, db 7.2/9.0% cpu, 53.3 MB, qdrant 17.0/19.9% cpu, 79.4 MB, fake-llm 28.1/29.2% cpu, 41.9 MB
- **chat**: caddy 3.1/4.5% cpu, 30.0 MB, api 84.0/101.7% cpu, 415.6 MB, db 22.3/27.9% cpu, 50.2 MB, qdrant 4.4/7.3% cpu, 49.6 MB, fake-llm 21.1/34.3% cpu, 42.8 MB
- **agent**: caddy 4.9/6.1% cpu, 36.1 MB, api 43.0/56.4% cpu, 411.3 MB, db 6.3/8.8% cpu, 54.7 MB, qdrant 0.1/0.2% cpu, 45.2 MB, fake-llm 5.0/6.9% cpu, 45.5 MB
- **team**: caddy 1.1/2.2% cpu, 42.0 MB, api 20.0/30.3% cpu, 397.4 MB, db 2.6/3.6% cpu, 59.6 MB, qdrant 0.0/0.1% cpu, 40.4 MB, fake-llm 5.4/21.1% cpu, 37.9 MB
- **chat_c1**: caddy 0.6/0.9% cpu, 20.0 MB, api 10.9/19.2% cpu, 403.3 MB, db 5.9/8.7% cpu, 46.4 MB, qdrant 2.0/4.6% cpu, 42.0 MB, fake-llm 12.0/30.2% cpu, 44.1 MB
- **chat_c4**: caddy 1.2/1.7% cpu, 20.4 MB, api 20.7/24.4% cpu, 381.5 MB, db 3.4/4.1% cpu, 46.4 MB, qdrant 0.9/1.2% cpu, 47.2 MB, fake-llm 3.0/3.3% cpu, 36.5 MB
- **chat_c16**: caddy 2.8/5.8% cpu, 22.2 MB, api 74.7/148.3% cpu, 370.6 MB, db 15.7/37.7% cpu, 47.1 MB, qdrant 3.1/6.9% cpu, 58.1 MB, fake-llm 10.5/18.4% cpu, 37.2 MB
- **chat_c64**: caddy 6.4/8.1% cpu, 44.7 MB, api 203.5/208.3% cpu, 380.7 MB, db 48.3/55.6% cpu, 69.2 MB, qdrant 10.7/14.9% cpu, 48.0 MB, fake-llm 21.5/23.8% cpu, 49.2 MB
- **mixed**: caddy 6.3/8.9% cpu, 54.3 MB, api 172.8/208.9% cpu, 406.9 MB, db 21.1/28.6% cpu, 63.9 MB, qdrant 6.4/11.0% cpu, 73.4 MB, fake-llm 15.4/33.5% cpu, 48.9 MB
- **slow_llm**: caddy 0.5/1.7% cpu, 58.2 MB, api 12.0/21.7% cpu, 382.8 MB, db 4.0/7.0% cpu, 59.9 MB, qdrant 2.6/4.4% cpu, 69.6 MB, fake-llm 12.6/23.6% cpu, 45.8 MB
- **llm_failures_30pct**: caddy 2.3/5.4% cpu, 57.5 MB, api 45.1/72.6% cpu, 384.0 MB, db 6.6/8.7% cpu, 60.9 MB, qdrant 1.9/4.5% cpu, 70.2 MB, fake-llm 12.6/27.9% cpu, 45.2 MB
- **llm_timeouts**: caddy 0.5/2.8% cpu, 20.1 MB, api 9.8/32.3% cpu, 315.5 MB, db 1.8/9.4% cpu, 40.2 MB, qdrant 0.4/1.2% cpu, 47.1 MB, fake-llm 2.3/11.3% cpu, 34.7 MB
- **extraction_docx**: caddy 1.7/3.1% cpu, 36.2 MB, api 174.5/209.1% cpu, 490.3 MB, db 0.0/0.0% cpu, 55.9 MB, qdrant 5.0/9.2% cpu, 64.3 MB, fake-llm 18.0/30.3% cpu, 41.3 MB
- **rate_limit_default**: caddy 23.1/25.4% cpu, 33.1 MB, api 56.4/62.2% cpu, 308.4 MB, db 0.0/0.1% cpu, 51.5 MB, qdrant 0.1/0.2% cpu, 60.7 MB, fake-llm 0.2/0.3% cpu, 41.0 MB
- **ab1_legacy_uvicorn_cli_health**: caddy 6.7/10.5% cpu, 38.7 MB, api 18.3/21.7% cpu, 312.7 MB, db 0.8/2.7% cpu, 29.0 MB, qdrant 1.2/3.6% cpu, 46.5 MB, fake-llm 0.2/0.3% cpu, 30.3 MB
- **ab1_legacy_uvicorn_cli_search**: caddy 14.5/16.2% cpu, 40.9 MB, api 205.6/211.2% cpu, 356.1 MB, db 0.0/0.1% cpu, 35.0 MB, qdrant 19.9/22.6% cpu, 90.1 MB, fake-llm 32.4/65.6% cpu, 33.4 MB
- **ab1_legacy_uvicorn_cli_chat**: caddy 3.3/5.3% cpu, 22.5 MB, api 62.9/88.6% cpu, 359.6 MB, db 9.2/13.8% cpu, 56.1 MB, qdrant 3.8/5.6% cpu, 91.5 MB, fake-llm 10.3/22.3% cpu, 44.2 MB
- **ab1_legacy_uvicorn_cli_mixed**: caddy 6.6/9.3% cpu, 25.4 MB, api 177.6/213.0% cpu, 379.9 MB, db 17.6/22.6% cpu, 53.4 MB, qdrant 7.9/10.6% cpu, 102.0 MB, fake-llm 13.6/22.6% cpu, 41.9 MB
- **ab1_app_serve_health**: caddy 11.9/23.1% cpu, 34.6 MB, api 31.1/65.2% cpu, 312.4 MB, db 0.8/3.4% cpu, 38.4 MB, qdrant 0.0/0.1% cpu, 102.4 MB, fake-llm 10.4/24.4% cpu, 43.3 MB
- **ab1_app_serve_search**: caddy 13.7/15.2% cpu, 41.1 MB, api 202.5/208.5% cpu, 343.2 MB, db 1.1/5.4% cpu, 32.2 MB, qdrant 22.8/26.4% cpu, 82.4 MB, fake-llm 34.8/59.1% cpu, 43.6 MB
- **ab1_app_serve_chat**: caddy 2.1/3.0% cpu, 42.1 MB, api 74.3/102.3% cpu, 339.7 MB, db 13.2/45.9% cpu, 54.5 MB, qdrant 3.4/4.4% cpu, 83.1 MB, fake-llm 7.9/12.6% cpu, 36.0 MB
- **ab1_app_serve_mixed**: caddy 6.0/7.8% cpu, 25.1 MB, api 200.0/207.1% cpu, 365.1 MB, db 25.0/34.0% cpu, 60.8 MB, qdrant 11.0/14.9% cpu, 116.4 MB, fake-llm 14.3/16.9% cpu, 33.9 MB
- **ab2_legacy_uvicorn_cli_health**: caddy 6.6/14.9% cpu, 28.4 MB, api 16.1/27.1% cpu, 310.3 MB, db 0.5/3.4% cpu, 26.1 MB, qdrant 0.6/3.3% cpu, 51.8 MB, fake-llm 4.4/19.3% cpu, 32.7 MB
- **ab2_legacy_uvicorn_cli_search**: caddy 14.1/16.3% cpu, 30.5 MB, api 205.6/212.1% cpu, 345.7 MB, db 1.0/3.7% cpu, 29.9 MB, qdrant 22.1/25.9% cpu, 61.5 MB, fake-llm 27.8/32.7% cpu, 35.8 MB
- **ab2_legacy_uvicorn_cli_chat**: caddy 2.8/4.2% cpu, 29.4 MB, api 77.6/118.1% cpu, 344.8 MB, db 17.3/59.7% cpu, 51.4 MB, qdrant 4.2/6.6% cpu, 62.6 MB, fake-llm 18.7/32.8% cpu, 50.0 MB
- **ab2_legacy_uvicorn_cli_mixed**: caddy 6.2/9.0% cpu, 32.5 MB, api 199.8/206.5% cpu, 379.8 MB, db 20.9/24.6% cpu, 61.8 MB, qdrant 11.0/13.5% cpu, 74.5 MB, fake-llm 18.7/34.9% cpu, 38.0 MB
- **ab2_app_serve_health**: caddy 9.5/20.6% cpu, 53.8 MB, api 36.3/72.0% cpu, 377.8 MB, db 0.5/2.5% cpu, 39.7 MB, qdrant 0.0/0.1% cpu, 68.7 MB, fake-llm 0.2/0.2% cpu, 32.2 MB
- **ab2_app_serve_search**: caddy 13.5/15.7% cpu, 53.1 MB, api 202.1/205.3% cpu, 352.4 MB, db 0.8/3.2% cpu, 40.7 MB, qdrant 21.4/25.6% cpu, 80.0 MB, fake-llm 39.0/51.9% cpu, 35.7 MB
- **ab2_app_serve_chat**: caddy 2.9/4.0% cpu, 33.3 MB, api 82.7/122.7% cpu, 354.5 MB, db 13.9/19.5% cpu, 40.6 MB, qdrant 4.2/5.2% cpu, 50.9 MB, fake-llm 8.5/9.5% cpu, 34.9 MB
- **ab2_app_serve_mixed**: caddy 5.7/7.6% cpu, 34.1 MB, api 178.3/209.2% cpu, 384.1 MB, db 20.4/28.9% cpu, 56.7 MB, qdrant 8.7/11.8% cpu, 73.3 MB, fake-llm 19.4/44.9% cpu, 54.6 MB
- **sustained_steady_before**: caddy 5.9/7.6% cpu, 52.3 MB, api 184.9/209.5% cpu, 389.0 MB, db 22.6/30.8% cpu, 63.8 MB, qdrant 10.5/16.4% cpu, 47.2 MB, fake-llm 22.6/44.5% cpu, 45.9 MB
- **sustained_spike**: caddy 6.8/10.7% cpu, 35.1 MB, api 162.4/211.3% cpu, 443.3 MB, db 17.2/19.0% cpu, 71.3 MB, qdrant 10.8/14.7% cpu, 44.8 MB, fake-llm 13.8/20.7% cpu, 46.0 MB
- **sustained_steady_after**: caddy 6.6/8.9% cpu, 36.4 MB, api 203.1/207.2% cpu, 462.7 MB, db 18.4/27.8% cpu, 54.8 MB, qdrant 12.1/17.3% cpu, 46.6 MB, fake-llm 16.3/34.7% cpu, 46.0 MB

### PostgreSQL per step

- **health**: 111 commits, 24 inserts, 38 updates, cache hit 0.978, connections at end 3
- **ready**: 9566 commits, 22 inserts, 42 updates, cache hit 1.0, connections at end 9
- **rag_ingest**: 91 commits, 24 inserts, 83 updates, cache hit 1.0, connections at end 9
- **rag_search**: 98 commits, 45 inserts, 77 updates, cache hit 1.0, connections at end 9
- **chat**: 11313 commits, 2364 inserts, 127 updates, cache hit 1.0, connections at end 9
- **agent**: 6752 commits, 2228 inserts, 112 updates, cache hit 1.0, connections at end 11
- **team**: 3602 commits, 1178 inserts, 133 updates, cache hit 1.0, connections at end 11
- **chat_c1**: 942 commits, 161 inserts, 91 updates, cache hit 1.0, connections at end 11
- **chat_c4**: 3554 commits, 626 inserts, 106 updates, cache hit 1.0, connections at end 11
- **chat_c16**: 12559 commits, 2272 inserts, 112 updates, cache hit 1.0, connections at end 11
- **chat_c64**: 15562 commits, 3630 inserts, 79 updates, cache hit 1.0, connections at end 11
- **mixed**: 7575 commits, 1562 inserts, 173 updates, cache hit 1.0, connections at end 11
- **slow_llm**: 1152 commits, 217 inserts, 79 updates, cache hit 1.0, connections at end 11
- **llm_failures_30pct**: 7818 commits, 1421 inserts, 151 updates, cache hit 1.0, connections at end 11
- **llm_timeouts**: 519 commits, 98 inserts, 113 updates, cache hit 1.0, connections at end 8
- **extraction_docx**: 128 commits, 30 inserts, 91 updates, cache hit 1.0, connections at end 11
- **rate_limit_default**: 334 commits, 19 inserts, 77 updates, cache hit 1.0, connections at end 11
- **sustained_steady_before**: 15713 commits, 3157 inserts, 482 updates, cache hit 1.0, connections at end 9
- **sustained_spike**: 4605 commits, 1032 inserts, 103 updates, cache hit 1.0, connections at end 11
- **sustained_steady_after**: 17373 commits, 3528 inserts, 450 updates, cache hit 1.0, connections at end 11

### Server-side latency per step (application histograms; p95 is a bucket upper bound)

- **rag_ingest**: rag:ingest: n=500, mean 45.5 ms, p95<=100.0 ms
- **rag_search**: rag:search: n=5234, mean 61.5 ms, p95<=250.0 ms
- **chat**: llm: n=1950, mean 209.9 ms, p95<=500.0 ms; rag:search: n=975, mean 28.2 ms, p95<=100.0 ms
- **agent**: llm: n=2218, mean 206.4 ms, p95<=250.0 ms
- **team**: llm: n=1155, mean 203.2 ms, p95<=250.0 ms
- **chat_c1**: llm: n=126, mean 207.0 ms, p95<=500.0 ms; rag:search: n=63, mean 30.7 ms, p95<=50.0 ms
- **chat_c4**: llm: n=528, mean 205.2 ms, p95<=250.0 ms; rag:search: n=264, mean 23.7 ms, p95<=50.0 ms
- **chat_c16**: llm: n=1896, mean 215.8 ms, p95<=500.0 ms; rag:search: n=948, mean 26.6 ms, p95<=100.0 ms
- **chat_c64**: llm: n=3072, mean 408.9 ms, p95<=1000.0 ms; rag:search: n=1536, mean 99.3 ms, p95<=250.0 ms
- **mixed**: llm: n=1342, mean 264.4 ms, p95<=500.0 ms; rag:ingest: n=155, mean 118.6 ms, p95<=500.0 ms; rag:search: n=981, mean 85.9 ms, p95<=250.0 ms
- **slow_llm**: llm: n=160, mean 3036.9 ms, p95<=5000.0 ms; rag:search: n=80, mean 29.7 ms, p95<=100.0 ms
- **llm_failures_30pct**: llm: n=1172, mean 392.7 ms, p95<=1000.0 ms; rag:search: n=586, mean 22.9 ms, p95<=50.0 ms
- **llm_timeouts**: llm: n=48, mean 10459.3 ms, p95<=30000.0 ms; rag:search: n=24, mean 71.6 ms, p95<=250.0 ms
- **qdrant_interruption**: llm: n=1952, mean 215.5 ms, p95<=500.0 ms; rag:search: n=9530, mean 58.0 ms, p95<=250.0 ms
- **extraction_docx**: rag:ingest: n=144, mean 537.0 ms, p95<=1000.0 ms
- **sustained_steady_before**: llm: n=2737, mean 261.4 ms, p95<=500.0 ms; rag:ingest: n=324, mean 107.2 ms, p95<=500.0 ms; rag:search: n=2037, mean 80.3 ms, p95<=250.0 ms
- **sustained_spike**: llm: n=873, mean 703.9 ms, p95<=2500.0 ms; rag:ingest: n=86, mean 717.8 ms, p95<=2500.0 ms; rag:search: n=648, mean 422.4 ms, p95<=2500.0 ms
- **sustained_steady_after**: llm: n=3035, mean 243.4 ms, p95<=500.0 ms; rag:ingest: n=361, mean 108.3 ms, p95<=500.0 ms; rag:search: n=2268, mean 73.1 ms, p95<=250.0 ms

### Qdrant direct (from inside the API container, 40 calls each)

```json
{
 "collection": "nexus_knowledge",
 "points": 500,
 "get_collection": {
  "p50_ms": 0.42,
  "p95_ms": 0.7
 },
 "scroll_5": {
  "p50_ms": 0.59,
  "p95_ms": 1.02
 },
 "query_vector": {
  "p50_ms": 0.98,
  "p95_ms": 1.58
 }
}
```

### thread_starvation

FastAPI runs sync handlers in a 40-thread pool per worker (2 workers = 80). 120 chats each holding a thread for ~16 s (two 8 s model calls) saturate it.

```json
{
 "chat": {
  "requests": 280,
  "rps": 7.0,
  "success_rate": 1.0,
  "latency_ms": {
   "min": 16425.11,
   "p50": 17213.91,
   "p95": 33168.05,
   "p99": 33362.59,
   "max": 33451.29,
   "mean": 23601.52
  },
  "status_counts": {
   "200": 280
  }
 },
 "liveness_probe_during_saturation": {
  "requests": 18314,
  "success_rate": 1.0,
  "latency_ms": {
   "min": 1.21,
   "p50": 1.67,
   "p95": 4.35,
   "p99": 9.38,
   "max": 129.85,
   "mean": 2.18
  },
  "status_counts": {
   "200": 18314
  }
 }
}
```

### qdrant_interruption

Qdrant stopped for 20 s under search+chat load; chat must keep answering without retrieval

```json
{
 "marks": {
  "ready_after_start_s": 1.1
 },
 "server_side": {
  "http_request_duration_ms": {
   "count": 9543,
   "mean_ms": 118.4,
   "p95_ms_bucket_upper_bound": 1000.0
  },
  "llm_duration_ms": {
   "count": 1952,
   "mean_ms": 215.5,
   "p95_ms_bucket_upper_bound": 500.0
  },
  "rag_duration_ms": {
   "count": 9530,
   "mean_ms": 58.0,
   "p95_ms_bucket_upper_bound": 250.0
  },
  "rag_duration_ms:search": {
   "count": 9530,
   "mean_ms": 58.0,
   "p95_ms_bucket_upper_bound": 250.0
  },
  "counters": {
   "alerts_fired_total:rule=qdrant_unavailable": 1,
   "qdrant_unavailable_total": 927
  }
 },
 "search_status": {
  "200": 7874,
  "500": 680
 },
 "chat_status": {
  "200": 976
 },
 "search_timeline": [
  {
   "t": 0.0,
   "requests": 640,
   "success_rate": 1.0,
   "p95_ms": 134.0
  },
  {
   "t": 5.0,
   "requests": 826,
   "success_rate": 1.0,
   "p95_ms": 94.0
  },
  {
   "t": 10.0,
   "requests": 753,
   "success_rate": 1.0,
   "p95_ms": 92.7
  },
  {
   "t": 15.0,
   "requests": 177,
   "success_rate": 0.215,
   "p95_ms": 492.8
  },
  {
   "t": 20.0,
   "requests": 155,
   "success_rate": 0.0,
   "p95_ms": 306.9
  },
  {
   "t": 25.0,
   "requests": 166,
   "success_rate": 0.0,
   "p95_ms": 263.1
  },
  {
   "t": 30.0,
   "requests": 156,
   "success_rate": 0.0,
   "p95_ms": 314.1
  },
  {
   "t": 35.0,
   "requests": 412,
   "success_rate": 0.845,
   "p95_ms": 281.8
  },
  {
   "t": 40.0,
   "requests": 651,
   "success_rate": 1.0,
   "p95_ms": 112.4
  },
  {
   "t": 45.0,
   "requests": 757,
   "success_rate": 1.0,
   "p95_ms": 100.6
  },
  {
   "t": 50.0,
   "requests": 707,
   "success_rate": 1.0,
   "p95_ms": 96.6
  },
  {
   "t": 55.0,
   "requests": 797,
   "success_rate": 1.0,
   "p95_ms": 88.5
  },
  {
   "t": 60.0,
   "requests": 739,
   "success_rate": 1.0,
   "p95_ms": 110.0
  },
  {
   "t": 65.0,
   "requests": 778,
   "success_rate": 1.0,
   "p95_ms": 97.1
  },
  {
   "t": 70.0,
   "requests": 840,
   "success_rate": 1.0,
   "p95_ms": 86.4
  }
 ],
 "chat_timeline": [
  {
   "t": 0.0,
   "requests": 64,
   "success_rate": 1.0,
   "p95_ms": 996.2
  },
  {
   "t": 5.0,
   "requests": 73,
   "success_rate": 1.0,
   "p95_ms": 691.4
  },
  {
   "t": 10.0,
   "requests": 72,
   "success_rate": 1.0,
   "p95_ms": 745.2
  },
  {
   "t": 15.0,
   "requests": 55,
   "success_rate": 1.0,
   "p95_ms": 969.9
  },
  {
   "t": 20.0,
   "requests": 59,
   "success_rate": 1.0,
   "p95_ms": 819.8
  },
  {
   "t": 25.0,
   "requests": 60,
   "success_rate": 1.0,
   "p95_ms": 734.9
  },
  {
   "t": 30.0,
   "requests": 59,
   "success_rate": 1.0,
   "p95_ms": 913.6
  },
  {
   "t": 35.0,
   "requests": 56,
   "success_rate": 1.0,
   "p95_ms": 1080.6
  },
  {
   "t": 40.0,
   "requests": 64,
   "success_rate": 1.0,
   "p95_ms": 929.8
  },
  {
   "t": 45.0,
   "requests": 68,
   "success_rate": 1.0,
   "p95_ms": 791.5
  },
  {
   "t": 50.0,
   "requests": 68,
   "success_rate": 1.0,
   "p95_ms": 819.7
  },
  {
   "t": 55.0,
   "requests": 74,
   "success_rate": 1.0,
   "p95_ms": 654.4
  },
  {
   "t": 60.0,
   "requests": 65,
   "success_rate": 1.0,
   "p95_ms": 787.4
  },
  {
   "t": 65.0,
   "requests": 67,
   "success_rate": 1.0,
   "p95_ms": 975.9
  },
  {
   "t": 70.0,
   "requests": 72,
   "success_rate": 1.0,
   "p95_ms": 633.0
  }
 ]
}
```

### Recovery after a traffic spike

```json
{
 "p95_before_ms": 1273.27,
 "p95_after_ms": 1193.5,
 "success_before": 1.0,
 "success_after": 1.0
}
```



## Keep-alive A/B (alternating legacy command and `app.serve`, 2 rounds, fresh API container + 15 s warm-up each)

| variant | loopback keep-alive p50 / p95 (ms, 120 requests) | health c=64 req/s (p50 ms) | search c=16 req/s | chat c=16 req/s (p50 ms) | mixed c=24 req/s |
|---|---|---|---|---|---|
| legacy `uvicorn --workers 2`, round 1 | 42.31 / ~45 | 122.5 (350) | 176.2 | 32.1 (489) | 53.7 |
| `app.serve`, round 1 | 0.51 / ~1.3 | 386.5 (95) | 172.1 | 34.8 (456) | 57.4 |
| legacy, round 2 | 42.22 / ~44 | 122.2 (348) | 161.4 | 32.8 (484) | 61.5 |
| `app.serve`, round 2 | 0.55 / ~1.2 | 307.8 (110) | 173.0 | 33.6 (466) | 58.5 |

Reading: the fix removes the 42 ms floor (about 80x on a bare keep-alive request) and raises throughput of cheap endpoints 2.5-3x.
Search, chat and mixed are dominated by service time and CPU, so **no change beyond run-to-run noise** is claimed for them. An earlier
single run (health c=64) went from 36 to 408 req/s; the alternating runs above are the reliable comparison. The Linux regression test fails (median 42.2 ms)
when the `setsockopt` line is removed and passes with it (verified in a Python 3.11 Linux container).

## Root cause of the keep-alive delay (measured, then explained)
1. Bisected by elimination: not the application (plain FastAPI shows it), not Caddy (direct loopback inside the container), not the container limits, capabilities or read-only
   filesystem (each removed in turn), not the environment variables. `--workers 1`: ~1.3 ms; `--workers 2` or `3`: ~42 ms.
2. With several workers uvicorn gives each worker a duplicate of the listening socket whose `proto` is 0; asyncio sets `TCP_NODELAY` on accepted connections only when
   `sock.proto == IPPROTO_TCP` (confirmed: the listener reports `proto=0`). Without NODELAY, Nagle's algorithm holds the response body until the client's delayed ACK (~40 ms) on every
   keep-alive request after the first; the first request is fast because Linux starts a connection in quick-ACK mode.
3. Accepted sockets inherit `TCP_NODELAY` from the listening socket on Linux, so setting it once on the listener in the parent process fixes every worker. On macOS the loopback behaves
   differently, so the effect appears only in the Linux containers.

## Thread starvation and liveness
Synchronous handlers (`/chat`, `/team`, ...) share a 40-thread pool per worker (80 for two workers). 120 concurrent chats with 8 s model calls saturate it.
Before the fix the synchronous `/health/live` queued behind them, exceeded Caddy's 3 s probe timeout, and Caddy answered 503 to **all** traffic (chat 564 of 802 requests, liveness 5573 of 5664).
After making liveness `async def` it runs on the event loop: **18314 of 18314 liveness probes succeeded (p95 4.4 ms)** while chat requests queued (p95 33 s, all 280 eventually succeeded, none failed).
The saturation itself is unchanged: when every pool thread waits on the model, chat latency grows with the queue.

## Database and Qdrant
- PostgreSQL stayed far from saturation (db container at most 48% average / 60% peak of one CPU and 71 MB, cache hit ratio 1.0, at most 11 connections; pool of 5 + 5 overflow per worker). It is **chatty**: a chat issues about 11.6 commits and 4.4 rollbacks (read-only transactions that end in a rollback); 975 chats produced 11313 commits, 4296 rollbacks and 2364 inserts.
- Qdrant direct (inside the API container, 40 calls, 500 points): `get_collection` p50 0.42 ms / p95 0.70, `scroll(5)` p50 0.59 / p95 1.02, `query_points` p50 0.98 / p95 1.58 ms. Search seen by the application (embedding + query): mean 61.5 ms at c=16 (server-side histogram), 28 ms inside chat. Qdrant is not a bottleneck here; the API CPU is.
- Qdrant interruption (stopped at t=15 s, started at t=35 s under search c=8 + chat c=8): search 7874 ok / 680 failed (HTTP 500, 8.0%), all failures between t=15 s and ~t=40 s; first bucket back at 100% by t=40 s; readiness 200 again **1.1 s** after `docker start`; chat 976 of 976 ok throughout (retrieval silently skipped). The application counted 927 `qdrant_unavailable` events and fired the `qdrant_unavailable` alert exactly once.

## Fake-model failure scenarios (chat, c=16 unless noted)
| scenario | req/s | p50 / p95 ms | outcome |
|---|---|---|---|
| fake LLM 200 ms (baseline) | 32.5 | 472 / 682 | all 200 |
| fake LLM 3 s per call | 2.67 | 6164 / 6532 | all 200; throughput = 16 users / ~6 s (two calls per chat) |
| 30% of model calls fail (HTTP 500, one client retry) | 19.5 | 553 / 1756 | all 200 (the application degrades to fallbacks) |
| every model call hangs 30 s, client timeout 5 s (c=8) | 0.53 | 21132 / 21278 | all 200 after ~21 s: 2 calls x (1 try + 1 retry) x 5 s; requests do not hang forever but are slow |

## Recovery after a traffic spike
mixed c=24 (60 s) -> c=96 (20 s) -> c=24 (60 s): p95 1273 ms before, 1194 ms after; success 100% in all three phases; during the spike p50 1371 / p95 4507 ms at 53 req/s (the CPU limit, not errors).

## Reproducibility and noise
- The suite is deterministic per seed, but the host is not. Chat c=16 across four runs of equivalent code: 13.8 req/s (run 1, p95 2989 ms), 31.6 (run 2, **95% HTTP 503**), 34.6 (run 3, p95 551 ms), 32.5 (final, p95 682 ms). In runs 1 and 2 some early steps also showed bursts of 502/503 (up to 95% in one step) and 2-5x lower throughput with higher API CPU per request; neither reproduced in the two later runs.
  **The cause was not established.** Hypotheses, unverified: contention with the other containers sharing the Docker VM, and CPU throttling at the 2-CPU limit delaying Caddy's 3 s health probe (a failed probe keeps the upstream "unhealthy" for the 10 s probe interval and Caddy then answers 503).
- Run 1 (`local-compose-run1.json`) is the pre-fix baseline (its sustained phase crashed on an unrelated `/system/metrics` 5xx and was redone); run 2 contains the after-fix A/B; the **final run is the complete reference**. All are kept.
- Use repeated runs on a quiet machine before drawing conclusions from small differences.

## Limitations and risks
- One host, shared VM, load generator on the same machine; closed-loop load only; 30 s steps; no repetition statistics.
- The fake model returns canned text instantly (plus configured latency): real model throughput, GPU queueing, token streaming and model errors are **NOT VERIFIED**.
- Capacity profile raised rate limits; shipped per-process limits behave per process (N replicas multiply them) and were measured only in the rate-limit step.
- No test of PostgreSQL or Qdrant under their own saturation, failover, or network partitions; no multi-replica test; no soak test beyond ~4 minutes.
- The extraction DOCX is small (about 400 paragraphs); large PDFs were not load tested.

## Capacity guidance for production (derived from the above; estimates are labelled)
- **Measured:** one API container limited to 2 CPUs saturates at about 50 chat/s (c=64: 51 req/s, CPU 203%), 175 search/s, 17 ingests/s, 12.8 team/s, **with an instant model**. Derived CPU cost: chat about 39 ms of CPU per request **(estimate: 2 cores / 51 req/s)**, search about 12 ms, ingest about 120 ms.
- **Estimate, not measured:** with a real model the limit becomes the thread pool: 80 threads / (calls per chat x model latency). At 3 s per call and 2 calls per chat that is roughly 13 chats/s per container; at 10 s per call about 4/s. Size replicas with that formula using your real latency, and re-measure.
- Scale **horizontally** (replicas of 2 workers), not by adding workers to one container, until the thread-pool model is revisited; remember rate limits and extraction slots are per process (shared rate limiting is still required beyond one instance).
- Keep Caddy/load-balancer probes on `/health/live` (cheap, now thread-pool independent) and probe `/health/ready` no more than every 5-10 s.
- Set `OLLAMA_TIMEOUT_SECONDS` / retries deliberately; the default budget makes a dead model cost ~4 x timeout per chat.
- Follow-ups (not done here): reduce per-chat database transactions; add a model circuit breaker; cache readiness results briefly; consider converting I/O-bound handlers to async or enlarging the pool after measuring.
- Re-run `python -m loadtest suite` against the target topology before launch; this report does not replace that.
