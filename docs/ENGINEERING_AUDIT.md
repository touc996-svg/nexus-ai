# NEXUS AI engineering audit (Phase 1)

Audited commit: `main` @ `0c4c6bf`, plus PR #3 (`feature/rag-eval-harness`) read from GitHub.
Method: read the code, ran the test suite and static tools locally, read PR #3 CI results.
Every claim below is labelled with how it was established. Status labels:
VERIFIED COMPLETE, IMPLEMENTED BUT NOT VERIFIED, PARTIALLY IMPLEMENTED, NOT STARTED, BLOCKED.

## 1. Current architecture

FastAPI application, about 5,400 lines of Python (`app/`, `tests/`, `scripts/`).

```
client -> security middleware (API key, rate limit, size cap, CORS, request id)
       -> app/main.py  (17 routes; memory, profile, agents, team, auto, chat, activity)
       -> routers: /knowledge, /system/*, /health/*, /integrations/aegis-x/*
orchestrator -> keyword router -> domain agent (enterprise | personal | cybersecurity)
             -> decision engine (keyword/heuristic risk and priority report)
agent -> OpenAI-compatible client -> Ollama (chat model)
RAG   -> NexusRAG -> Ollama /api/embed (nomic-embed-text) -> Qdrant (dense vectors)
state -> SQLAlchemy (SQLite `nexus.db`): memory, profiles, activity
AEGIS-X integration -> read-only findings/incidents loader and 5 tools
```

Notable layout facts: `app/main.py` is 614 lines and holds most routes and some business logic; routing
and risk decisions are keyword matching; there is no provider abstraction layer.

## 2. Verified completed functionality

| Capability | Status | Evidence |
|---|---|---|
| Test suite on `main` | VERIFIED COMPLETE | 84 passed locally (`NEXUS_DEMO_MODE=true`) |
| CI on PR #3 | VERIFIED COMPLETE | Python 3.11 and 3.12 tests and Docker build all `SUCCESS` |
| Offline demo mode | VERIFIED COMPLETE | tests cover demo RAG and demo LLM without Ollama or Qdrant |
| API-key auth (shared secret) | VERIFIED COMPLETE (as designed) | constant-time `hmac.compare_digest`; required in production unless demo mode |
| Request size cap, CORS allowlist, sliding-window rate limit | VERIFIED COMPLETE (as designed) | `app/security/hardening.py`, `tests/test_security_hardening.py`. Limiter is process-local (documented in the file). |
| Embedding validation | VERIFIED COMPLETE | dimension check, non-finite check, retries (`rag.py:106`) |
| Idempotent re-ingestion | VERIFIED COMPLETE | content hash, publish new chunks then remove stale ones, tested in `test_rag_indexing.py` |
| Upload limits | VERIFIED COMPLETE | allowed extensions, 20 MB cap, temp file cleaned in `finally` |
| Non-root container with healthcheck | VERIFIED COMPLETE | `Dockerfile`: dedicated user, `HEALTHCHECK` |
| Read-only AEGIS-X integration | VERIFIED COMPLETE | tests in `test_aegis_x_integration.py`; no execution path |
| Secret scan of tracked files | VERIFIED COMPLETE | regex scan for AWS, OpenAI, GitHub keys and private keys found nothing |
| Bandit | VERIFIED COMPLETE | 0 issues on `app/` |

## 3. Incomplete or missing functionality

| Area | Status | What exists / what does not |
|---|---|---|
| Hybrid retrieval, reranking in production RAG | NOT STARTED | `NexusRAG.search` is dense-only. BM25, hybrid and reranker exist only inside the eval harness (PR #3). |
| Citations, citation validation, abstention in production RAG | NOT STARTED | `search` returns chunks with a score threshold. Cited, structured, refusing answers exist only in the eval harness. |
| Eval harness connected to real `NexusRAG` and Qdrant | NOT STARTED | PR #3 evaluates a 13-document offline corpus with its own retrievers. |
| Per-user authorization | NOT STARTED (see weakness S1) | one shared key, no identities |
| Prompt-injection defence, PII handling, output validation | PARTIALLY IMPLEMENTED | `guardrails.validate` checks type, emptiness and length only. The orchestrator labels team context as untrusted (`_untrusted_context`); its effectiveness is not tested adversarially. |
| Agent planning, tool selection, MCP, human approval | NOT STARTED | agents are single LLM calls behind a keyword router. The only tools are the 5 read-only AEGIS-X ones. |
| Provider abstraction (LLM, embeddings) | NOT STARTED | chat uses the `openai` SDK pointed at Ollama; embeddings call Ollama over `httpx` directly. |
| OpenTelemetry, token and cost tracking | NOT STARTED | an activity log, health endpoints and `X-Request-ID` in access logs exist. No traces, no token or cost accounting. |
| Caching, model routing, streaming | NOT STARTED | none present |
| Multimodal ingestion (images, OCR, tables) | NOT STARTED | PDF, DOCX, TXT and MD text extraction only (`pypdf`, `python-docx`) |
| Docker Compose, Kubernetes, Helm | NOT STARTED | none in the repo |
| Lint, format, type, dependency, container and secret scanning in CI | NOT STARTED | CI runs compile, pytest, `pip check`, a tracked-`.env` check and a Docker build |
| API versioning, consistent error schema, async DB access | NOT STARTED | unversioned routes; mostly synchronous handlers |

## 4. Technical and security weaknesses

Severity is my judgement. "Verified" means confirmed by reading the code; none were exploited.

- **S1 (High, verified by code): no tenant isolation.** `user_id` and `session_id` come from the caller
  (`/memory/{session_id}`, `/profile/{user_id}`, `/knowledge/search` and `/knowledge/ingest`). Anyone who holds
  the one shared API key can read, overwrite or delete any user's memory, profile and indexed documents by naming
  them. The vector-store filter is correct, but it filters on a value the client chooses.
- **S2 (High, verified by code): only length validation on prompts.** No injection detection, no PII detection,
  no validation of model output before it is returned or stored.
- **S3 (Medium): demo mode disables authentication by design.** `render.yaml` auto-deploys `main` to a free public
  service in demo mode on every commit. That is acceptable for a demo, but demo mode must never be combined with real data.
- **S4 (Medium): process-local rate limiting.** It does not hold across workers or instances (the file says so).
- **S5 (Medium): dependencies are not reproducible.** `openai` is unpinned and `qdrant-client` is a range, so
  `pip-audit` could not run against `requirements.txt` as written (BLOCKED until pinned). `pytest` is in the
  production requirements. No lockfile, no hashes, no Dependabot configuration.
- **S6 (Low): stale config.** `app/core/config.py` defines only `OPENAI_API_KEY`; real configuration is read
  from scattered `os.getenv` calls, so there is no single validated settings object.
- **Q1: static analysis.** `ruff` (default rules) reports 58 findings, 30 auto-fixable. `mypy` reports 4 errors
  in 2 files, and most function bodies are untyped so type checking covers little. There is no lint or type
  configuration in the repo.
- **Q2: structure.** `app/main.py` mixes routing and logic. The router and decision engine rely on keyword
  matching, which is brittle on paraphrased input.

## 5. PR #3 review

PR #3 adds the offline RAG evaluation harness: 18 files, +1,811 and 0 deletions, so it cannot remove working code.

- CI: all checks green (Python 3.11 and 3.12, Docker build). Locally: harness tests pass and the quality gate passes.
- Strengths: deterministic, offline, regression gate, mutation tests, held-out split that cannot set a baseline.
- Limits that matter for this roadmap: it does not test the production `NexusRAG` (see section 3); the dataset is
  small and written by one author; faithfulness is a lexical proxy; the committed model caches (about 0.4 MB)
  only replay results for the current dataset and models.
- Recommendation: merge it, then build the real-RAG adapter on top. **Not merged; waiting for your approval.**

## 6. Recommended implementation order

1. Merge PR #3 (your approval).
2. **Adapter: evaluate real `NexusRAG` over Qdrant.** The existing client supports in-memory Qdrant
   (`QdrantClient(location=":memory:")`), so the real ingestion, chunking and search code can be evaluated in tests
   and CI without Docker or a server; a live Qdrant container is an optional second run. Add latency, token and
   cost columns. Compare dense (production) with the harness's BM25, hybrid and rerank.
3. Fix S1 (identity-bound access) before adding features that read more data; add tests that prove isolation.
4. Production RAG: hybrid retrieval, reranking, cited structured answers and abstention, using whatever step 2
   shows is actually better.
5. Guardrails and a red-team suite (S2), then orchestration and approval gates, then observability and caching,
   then multimodal, then Compose, Helm and the CI scanners.
6. Pin dependencies and add lint, type and audit steps early (cheap, and unblocks `pip-audit`).

## 7. Risks and dependencies

- **Memory:** this machine has 8 GB of RAM. The 4B chat model plus Qdrant plus the app is tight; the 8B model was
  already shown not to run usefully. Live end-to-end runs must use small models.
- **Local models:** live evaluation needs Ollama with `nomic-embed-text` and `qwen3`; CI replays from cache.
- **Behaviour change:** S1 changes the API contract (requests will need an identity). That must be an explicit,
  documented breaking change, not a silent one.
- **Embedding provider:** a hosted embedding provider needs an account and a key; none is configured and none
  will be added without your approval.
- **Cost:** nothing in this plan requires a paid service. Kubernetes and Helm work can be validated locally;
  no cloud deployment is planned.
