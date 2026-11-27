# NEXUS AI: Production Readiness Audit

Inspected 2026-11-26. Updated 2026-11-27 after P0 PRs #19-#22 merged (section 0). Sections 1-13 describe the state **at audit time**; section 0 supersedes them where they conflict. Nothing has been deployed or provisioned. Every result below comes from a command run in this session unless marked ASSUMPTION or NOT RUN.

Statuses: VERIFIED, MISSING, NEEDS FIX, BLOCKED, NOT APPLICABLE.

## 0. P0 status after merges (main CI green on every merge)

| Finding | PR | Status | Evidence | Still unverified |
|---|---|---|---|---|
| H2 chat context length | #19 (6b7bb0d) | FIXED | regression test fails before / passes after; CI 4/4 | live-model behaviour |
| H4 reproducible deps | #20 (12bf7e8) | FIXED | hashed universal lock, `openai==3.42.0`, `--require-hashes` in Docker/Render/CI, `pip-audit` clean and in CI | lock not refreshed on a schedule (no Dependabot/Renovate) |
| H3 managed DB | #22 (8621ce0) | CODE READY | Postgres via `DATABASE_URL`, TLS enforced for remote hosts, pooling, Alembic baseline; 455 tests incl. real local Postgres 16, no drift; CI job added | **not tested on real Neon**; no SQLite->Postgres data copy; Render config still demo-only and has no migration step |
| H1 injection/PII/output | #21 (13bbad8) | PARTIAL | heuristic layer: chunk drop, marker defusal, PII masking, output scrub; 48 security regression tests green; mutation check on chunk drop | bypassable by design; user messages unfiltered; no adversarial corpus or live-model evaluation; no ingest-time scan |
| M4 tracked `.bak` files | #24 (f694cc0) | FIXED | 25 files removed, `*.bak*` ignored | untracked local backups left in place |
| CI scanning | #25 (1ca79db) | DONE | gitleaks (full history) and Trivy (fs + image, HIGH/CRITICAL fixable) run on main; 3 image CVEs patched in the Dockerfile | base image not pinned by digest; scanners find only known CVEs and configured secret patterns; gitleaks allowlist covers `tests/` and `evals/cache|recovery/` |
| M6 document parsing | #26 (96e0c52) | FIXED (hardened, not sandboxed) | signature checks, DOCX zip-bomb limits, PDF page and incremental char limits, child process with timeout/CPU/memory limits and scrubbed env, off the event loop; 24 extraction tests green on Linux CI and in Linux Docker, incl. memory-limit test | see below |
| Extraction concurrency | #28 | DONE (per process) | `BoundedSemaphore` (default 2, range 1-16), 5 s queue timeout, HTTP 503 + `Retry-After`, released in `finally`; release verified after normal, rejected, timeout and SIGKILL on Linux CI | total = workers x N and not shared across instances; needs shared limiting (M1) for multi-instance |

## Remaining production blockers (untrusted multi-user)
1. Real Neon verification (TLS, pooler, cold starts) and a migration step in the deploy flow.
2. H1 hardening: adversarial test corpus, live-model evaluation, ingest-time checks.
3. Qdrant server path never run in CI (M-level RAG items: hybrid/rerank/citations/abstention still absent in production code).
4. Shared rate limiting / proxy handling (M1), secret management (M2). Extraction concurrency is capped per process only (see M6 row): not shared across instances.
5. No observability (errors, latency, tokens) and no deploy gate (Render auto-deploys `main` on commit).
6. Ruff 98 / mypy 13 findings untriaged.
7. Docker image: built and health-checked locally once (non-root, healthy), CI builds it; not run under production settings.
8. Extraction limitations: `RLIMIT_AS` (memory) is **not enforced on macOS** (enforced and tested on Linux); no network isolation or seccomp sandbox, so a parser exploit in the child keeps the server user's read access; legacy `NexusRAG.extract_text`/`ingest_file` in `rag.py` still parse in-process (unused by the API; removal needs an eval replay-cache refresh with live Ollama).

## 1. Executive summary

NEXUS AI is a working, well-tested **demo-grade** platform: 437 tests pass, authentication, tenant isolation and
rate limiting exist and are tested, and a deterministic public-demo mode is deployable today. It is **not ready for untrusted
multi-user production**. The blockers are (a) no prompt-injection / PII / output-validation layer, (b) Docker and Qdrant-server
paths never run here, (c) per-process rate limits and local SQLite, (d) no observability beyond health endpoints, (e) an
unpinned dependency (`openai`) and no lockfile, and (f) CI that lacks lint, audit, scan and deploy gates. The existing
`docs/RELEASE_READINESS.md` is accurate on these points; I found no overstated claim in it, but one stale number (432 vs 437 tests).

## 2. Current architecture (verified from code)

- FastAPI app `app/main.py` (v0.7.0), routers: health, observability (`/system/*`, admin only), knowledge (RAG), AEGIS-X (admin only).
- `NexusOrchestrator` dispatches to Enterprise, Personal and Cybersecurity agents; `decision_engine` is **rule-based keyword logic**, not ML.
- LLM through the `openai` client against Ollama (`OLLAMA_BASE_URL`, default `localhost:11434`).
- RAG `app/services/rag.py` (453 lines): Qdrant (`QDRANT_URL`, default `http://localhost:6333`; `:memory:` in demo), cosine, dense-only, 1000-word chunks.
- SQLAlchemy via `DATABASE_URL`, default SQLite `./nexus.db` (`app/core/database.py`).
- Security in `app/security/*`: operator key, per-user keys, hashed expiring keystore, HS256 JWT, sliding-window rate limiter, CORS allowlist, security headers.
- Dashboard: single static HTML.

## 3. Test and tooling results (all run now)

| Check | Status | Result |
|---|---|---|
| `pytest -q` | VERIFIED | **437 passed, 1 skipped, 0 failed** (5.4 s). Skipped: real Qdrant-server test (`EVALS_QDRANT_URL` unset). |
| `compileall app` | VERIFIED | OK |
| `pip check` | VERIFIED | No broken requirements |
| `pip-audit -r requirements.txt` | VERIFIED | No known vulnerabilities (RELEASE_READINESS says this was BLOCKED; it now runs). Caveat: `openai` unpinned, so this audits whatever resolves today (3.42.0 installed). |
| `bandit -r app` (excluding `.bak`) | VERIFIED | No findings |
| `ruff check` (app, tests, evals, scripts) | NEEDS FIX | 98 findings, 48 auto-fixable; not triaged, none verified as bugs |
| `mypy app --ignore-missing-imports` | NEEDS FIX | 13 errors in 4 files (e.g. `str | None` passed where `str` expected: `app/api/knowledge.py:118`, `app/main.py:208,220,238`) |
| `docker build` / run | BLOCKED | Docker daemon not running; image never built locally. NOT RUN |
| Real Qdrant server test | BLOCKED | no server available; only in-process client tested |
| Live-model behaviour | NOT RUN | tests stub inference |
| Frozen-config / eval gate (`evals.run --check`) | NOT RUN here | CI runs it; I did not re-run it |

## 4. Verified existing capabilities

- VERIFIED (by code read and passing tests): operator vs per-user principals; 403 on system routes for user keys; per-user `user_id` filter on every RAG query, list and delete (`rag.py` `_user_filter`); upload extension allowlist and 20 MB cap with chunked read (`knowledge.py`); temp file removed in `finally`; JWT accepts only `alg=HS256`, requires `exp`, issuer, audience, caps lifetime, constant-time compare, redacted `repr`; CORS is allowlist-only; `nosniff`, `X-Frame-Options`, HSTS (conditional) headers; app refuses to start without an API key when `APP_ENV=production` and not demo; embedding dimension is checked against the existing collection at startup (`rag.py` ~l.101); similarity floor `NEXUS_RAG_MIN_SCORE`; no raw SQL string building found (grep for `text(`/`execute(`/f-string SQL returned nothing; ORM only).
- Not re-verified by me beyond reading: mutation-check claims in RELEASE_READINESS (that script is not in the repo).

## 5. Security findings

### Critical
None found that is exploitable in the documented demo mode. In production mode the items below are blockers for *untrusted* users.

### High
| ID | File | Problem | Impact | Fix | Validation |
|---|---|---|---|---|---|
| H1 | `app/security/guardrails.py` (21 lines) | Only checks type, emptiness, length. No prompt-injection or indirect-injection detection, no PII handling, no output validation. Retrieved text is labelled "untrusted" in prompts only (`main.py` `format_knowledge_context`), which is advisory. | An uploaded document can steer an agent; model output may leak other context. | Layered: strip/neutralize delimiters in retrieved chunks, instruction-hierarchy prompt, output schema validation, PII redaction on ingest and logs, adversarial test set. | New tests with injection corpora; red-team script; mutation check. |
| H2 | `app/security/guardrails.py` + orchestrator | 5,000-char limit applies to user message *plus* appended retrieved text, so valid `/chat` can fail with "Message exceeds maximum length" (documented in RELEASE_READINESS, still unfixed). `MAX_INTERNAL_CONTEXT_LENGTH = 15000` exists but `main.py` caps knowledge at 6000. | Functional failure for real users with RAG. | Validate user input and internal context separately. | Test: 4,900-char message + 6,000-char context returns 200. |
| H3 | `app/core/database.py`, `Dockerfile`, `render.yaml` | Default SQLite file inside container `/app`, no volume; Render free tier has an ephemeral disk. | Memory, profiles, keystore lost on restart; not multi-instance safe. | Managed Postgres via `DATABASE_URL` (+ migrations, see M3). | Restart test; two-instance test. |
| H4 | `requirements.txt` | `openai` unpinned, no lockfile, others use `==` or ranges. | Non-reproducible builds, supply-chain drift. | Pin all, generate hashed lockfile (`pip-compile --generate-hashes` or uv). | Clean build twice yields identical `pip freeze`. |

### Medium
| ID | File | Problem | Fix |
|---|---|---|---|
| M1 | `security/hardening.py` | Rate limiter is per process; users behind one proxy share one per-address budget. ASSUMPTION: no trusted-proxy (`X-Forwarded-For`) handling was found in the lines I grepped; confirm. | Gateway/Redis shared limiter; configure trusted proxies. |
| M2 | `.env.example`, keys | Env keys plaintext, non-expiring; JWT is shared-secret HS256, not OIDC. | Secret manager; keystore or OIDC/JWKS. |
| M3 | `core/database.py` | `create_all` on startup, no Alembic migrations. | Add Alembic before any schema change in production. |
| M4 | repo | **25 `*.bak*` files tracked in git** under `app/` (old code versions, e.g. `main.py.bak.*`), plus 3 untracked items (`.nexus_finish_backup/`, `index.html.bak.soc`, `docs/FINALIZATION_REPORT.md`). `.dockerignore` excludes `*.bak*`, so images are unaffected. | Delete tracked backups (history preserves them) in a cleanup PR; I did not touch them. |
| M5 | `Dockerfile` | `python:3.11-slim` unpinned tag; no digest; `pip install` as root then switches user (fine); no `--require-hashes`. | Pin digest, hashed install, multi-stage if size matters. |
| M6 | `knowledge.py` | Ingest trusts file extension, not content type; `pypdf`/`python-docx` parse attacker-supplied files in-process (zip bomb / parser CVE exposure). 20 MB upload but `NEXUS_RAG_MAX_DOCUMENT_CHARS` is the only extraction bound. | Magic-byte check, parse in a resource-limited worker, page/size caps. |
| M7 | `knowledge.py` | Error detail returns only exception type (good), but `ValueError` messages pass through to the client (`detail=str(exc)`). Low leak risk; review messages. | Map to fixed messages. |

### Low
- L1: `evals/cache/*.json` and reports are committed (size, not secrets). I did not scan them for secrets: NOT RUN.
- L2: Ruff `S110` try/except-pass and `PLW1510` subprocess without `check` in a script; triage.
- L3: `/system/evaluation/latest` reads a path from env; admin-only, fine.
- L4: Dashboard chat panel does not send an API key, so it works only with auth off (documented in the showcase).
- L5: Secret scanning: **NOT RUN** (no gitleaks/trufflehog executed). Only `.env` tracking is checked in CI; `.env` exists locally and is gitignored (VERIFIED via `git check-ignore`).

## 6. RAG assessment

| Stage | Status | Evidence |
|---|---|---|
| Ingestion (pdf, docx, txt, md) | VERIFIED | `extract_text`, `ingest_file`; per-user idempotent replace by source id, publish-then-delete-stale |
| Chunking | NEEDS FIX | fixed 1000-word window, no overlap default tuning, no structure/heading awareness, no metadata beyond filename and chunk index |
| Embeddings | VERIFIED | Ollama `nomic-embed-text`, 768-dim checked at startup and per call |
| Qdrant integration | NEEDS FIX | works with in-process client; real server path untested (skipped test); default URL `localhost:6333`, no API key/TLS option seen in the constructor lines I read (ASSUMPTION) |
| Retrieval | NEEDS FIX | **dense-only** in production |
| Hybrid / rerank / citations / abstention | MISSING in production | exist only in `evals/` harness (`retrievers.py`, `answerer.py`); `docs/RAG_ADAPTER_EVALUATION.md` describes them |
| Context construction | NEEDS FIX | truncation by characters, labelled as untrusted; see H2 |
| Grounded answers / hallucination control | MISSING in production | no citation enforcement or "I don't know" gate in the agent path |
| Evaluation | VERIFIED (experimental) | dev and held-out sets, baseline gate in CI; measures the eval adapter, not the production agent path. I did not re-run the numbers. |

To make RAG reliable for users: port hybrid + rerank + citations + abstention from `evals/` into `app/services`, add overlap and heading-aware chunking, run the Qdrant-server test in CI (service container), and evaluate end to end through the real `/chat` path.

## 7. Docker and runtime

| Item | Status | Note |
|---|---|---|
| Non-root user | VERIFIED (by reading) | `USER nexus`, system user |
| HEALTHCHECK | VERIFIED (by reading) | hits `/health/live`; liveness only, does not check dependencies (that is `/health/ready`) |
| Build / start / shutdown | BLOCKED | Docker not running; NOT RUN |
| Persistent storage | NEEDS FIX | none declared (H3) |
| Config | NEEDS FIX | `APP_ENV=production` baked in the image, so a container started without a key refuses to boot (good), but no `NEXUS_DEMO_MODE` default; port 8010 vs `$PORT` on Render (Render uses its own start command, fine) |
| Resource limits, logging | MISSING | no limits, no `--workers`/`--proxy-headers`, plain uvicorn logs; only 2 logging calls found in `app/` |
| Ollama / Qdrant exposure | NOT APPLICABLE | no Compose file; neither is exposed by this repo |

**Render config:** `render.yaml` is appropriate **only** for the stateless public demo (`NEXUS_DEMO_MODE=true`, free plan, no DB, no Ollama, no Qdrant). It is not appropriate for the real platform, which needs Ollama (or a hosted LLM), Qdrant and a database. Note `autoDeployTrigger: commit` on `main` deploys with no CI gate.

## 8. CI/CD and observability

| Capability | Status |
|---|---|
| Tests on 3.11/3.12, compile, eval gate, `pip check`, `.env` guard, docker build | VERIFIED (workflow read; not run on GitHub by me) |
| Lint, type check | MISSING in CI |
| Dependency audit (`pip-audit`) | MISSING in CI |
| Container scan (Trivy/Grype) | MISSING |
| Secret scanning | MISSING (GitHub secret scanning may be enabled in repo settings: NOT CHECKED) |
| Deployment gate (CI must pass before Render deploy) | MISSING |
| Qdrant server integration job | MISSING |
| Structured logs, error tracking | MISSING |
| Request latency, availability metrics | MISSING |
| LLM latency, token usage, RAG retrieval metrics | MISSING (`/activity` counters exist in-app: partial, not exportable) |
| DB health | PARTIAL: `memory.health_check()` and `/health/ready` |

## 9. Recommended deployment architecture (cost-conscious, portfolio)

Do not deploy anything until you approve.

- **Public demo (now):** current Render free service in demo mode. Cost: $0.
- **Real platform, small:** one container host (Render/Fly/Railway) + managed Postgres (Neon or Supabase free/low tier) + Qdrant Cloud free 1 GB cluster (API key + TLS) + a hosted LLM/embedding API (e.g. Anthropic or OpenAI; Ollama on a public VM is costly and must stay private) + secrets in the host's secret store + Sentry free tier + UptimeRobot + Cloudflare for DNS/TLS. Rough cost categories: compute, Postgres, vector DB, LLM tokens (dominant and variable), monitoring, domain. Figures not estimated; they depend on your provider choices.
- Keep Ollama and Qdrant private (no public ports); the app calls them over TLS with keys.

## 10. Deployment blockers (for untrusted users)

H1, H2, H3, H4, plus Docker build never verified, Qdrant server path unverified, no shared rate limiting.

## 11. Prioritized remediation roadmap

**P0:** H1 injection/PII/output validation; H2 context-length bug; H3 managed DB; H4 pin + lock deps; remove secrets risk (run gitleaks over history).
**P1:** build and run the Docker image locally and in CI; Qdrant server in CI; production hybrid/rerank/citations/abstention; Alembic; proxy-aware shared rate limiting; deploy gate (CI before Render).
**P2:** ruff and mypy in CI (fix the 13 mypy errors); pip-audit, Trivy, gitleaks in CI; structured logs, Sentry, metrics (latency, tokens, retrieval); remove tracked `.bak` files.
**P3:** chunking quality, caching, workers/resource limits, OIDC.

## 12. Commands to validate (safe, read-only)

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q app
.venv/bin/python -m pip check
pip-audit -r requirements.txt
bandit -q -r app -x '*.bak*'
ruff check app tests evals scripts --statistics
mypy app --ignore-missing-imports --exclude '\.bak'
.venv/bin/python -m evals.run --check
docker build -t nexus-ai:audit . && docker run --rm -e APP_ENV=production nexus-ai:audit   # expect refusal without a key
git ls-files | grep -c '\.bak'
```

## 13. Decisions needed from you

1. Hosted LLM provider vs self-hosted Ollama for production.
2. Postgres provider (Neon / Supabase / Render).
3. Qdrant Cloud vs self-hosted.
4. Delete the 25 tracked `.bak` files in a cleanup PR?
5. Approve start order: P0 first, one PR per item.
6. Keep `render.yaml` as demo-only and add a gate so `main` deploys only after CI passes?
7. Install gitleaks/Trivy in CI (free)?

No fixes have been started.
