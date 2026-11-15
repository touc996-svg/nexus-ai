
# NEXUS AI

### Unified Enterprise, Personal Assistant & Cybersecurity AI Platform

**Application version:** 0.7.0 (coordinated 8-area upgrade)

NEXUS AI is a modular AI platform designed to bring enterprise assistance, personal productivity, and cybersecurity analysis together through a unified multi-agent architecture.

## Project Overview

NEXUS AI uses specialized agents coordinated by a central orchestrator. It combines automated routing, rule-based decision support, security guardrails, and persistent SQLite memory.

The goal is to build a scalable foundation for intelligent automation and enterprise AI workflows.

## Core Capabilities

- **Enterprise Agent:** Business and operational assistance.
- **Personal Agent:** Productivity, career, and organizational assistance.
- **Cybersecurity Agent:** Security-focused analysis and recommendations.
- **Multi-Agent Orchestration:** Coordinates specialized agents through a unified workflow.
- **Intelligent Routing:** Routes requests using keyword-based classification.
- **Decision Engine:** Identifies risks, priorities, potential contradictions, and agent failures.
- **Persistent Memory:** Stores local conversation history using SQLite; it is disabled in the public demo.
- **API Interface:** Exposes platform functionality through FastAPI.
- **Web Dashboard:** Provides an interface for interacting with agents and uses a separate browser session identifier.

## Architecture

```text
                 NEXUS AI
                     |
             FastAPI Application
                     |
             Nexus Orchestrator
                     |
          +----------+----------+
          |          |          |
       Enterprise  Personal  Cybersecurity
         Agent      Agent      Agent
          |          |          |
          +----------+----------+
                     |
              Decision Engine
                     |
              Security Guardrails
                     |
              SQLite Memory
```

## Technology Stack

| Area | Technology |
|---|---|
| Backend | Python |
| API Framework | FastAPI |
| Data Validation | Pydantic |
| Database | SQLite |
| ORM | SQLAlchemy |
| Testing | Pytest |
| API Documentation | OpenAPI / Swagger |
| Interface | HTML, CSS, JavaScript |

## Project Structure

```text
nexus-ai/
├── app/
│   ├── agents/
│   ├── core/
│   │   ├── database.py
│   │   ├── decision_engine.py
│   │   ├── memory.py
│   │   ├── orchestrator.py
│   │   └── router.py
│   ├── dashboard/
│   ├── security/
│   └── main.py
├── tests/
├── docs/
├── README.md
└── .gitignore
```

## Getting Started

### 1. Clone the repository

```bash
git clone YOUR_REPOSITORY_URL
cd nexus-ai
```

### 2. Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Start the application

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8010 --reload
```

### 5. Access the application

- Dashboard: http://127.0.0.1:8010/
- API Documentation: http://127.0.0.1:8010/docs
- Decision Engine: http://127.0.0.1:8010/decision-engine/status
- Memory Status: http://127.0.0.1:8010/memory/status

## Testing

Run the automated test suite:

```bash
python -m pytest -q
```

Run the current test suite with `python -m pytest -q`; it covers agent APIs, orchestration, conversation validation, personalization, decision rules, and demo-mode RAG.

## Engineering Focus

NEXUS AI is being developed with an emphasis on:

- Modular software architecture
- Multi-agent system design
- AI-assisted decision workflows
- Persistent data management
- Cybersecurity-aware application design
- API development and testing
- Future cloud deployment and scalability

## Current Development Status

The current implementation includes the FastAPI application, three specialized agents, orchestration, rule-based decision support, SQLite persistent memory, and automated tests.

The project is under active development. Authentication and user-level authorization remain required before exposing live/local-AI mode to an untrusted network. The public demo is stateless and disables memory/profile CRUD; knowledge ingestion and retrieval remain disabled there.

## Disclaimer

NEXUS AI is a development and portfolio project. It is not currently intended for production use or as a substitute for professional cybersecurity analysis.


## RAG and Ollama Operations

NEXUS uses Ollama for chat completions and embeddings, with Qdrant for per-user vector retrieval.

Example local services:
```bash
ollama serve
ollama pull qwen3:1.7b
ollama pull nomic-embed-text
docker run -d --name nexus-qdrant -p 6333:6333 -p 6334:6334 qdrant/qdrant
```

Configure the models and vector dimensions in `.env` using `.env.example`. The configured embedding dimension must match the embedding model and the existing Qdrant collection. If changing vector dimensions, create a new collection and re-index; NEXUS refuses to silently mix vector sizes.

Knowledge endpoints:
- `POST /knowledge/ingest`
- `POST /knowledge/search`
- `GET /knowledge/sources`
- `DELETE /knowledge/sources/{source_id}`

## Agent Collaboration and Memory

The team workflow runs Enterprise → Personal → Cybersecurity. Each agent receives a bounded text summary from prior agents rather than an unbounded serialized Python dictionary. The response includes per-agent duration, output length, status and a basic usability flag.

SQLite memory retains up to the configured per-session maximum but supplies only the latest `NEXUS_MEMORY_HISTORY_LIMIT` messages to conversation intelligence. Nested profile dictionaries are merged so adding one skill does not erase other saved skills.

## Observability and Evaluation

- `GET /system/llm-health` checks Ollama connectivity and configured model availability.
- `GET /system/rag-health` checks the Qdrant collection and embedding model status.
- `GET /system/memory-health` checks SQLite memory availability.
- `GET /system/evaluation/latest` returns the last saved agent benchmark.

Run the repeatable six-case benchmark against the configured agents:
```bash
python scripts/evaluate_agents.py
```

The benchmark reports response availability, expected-signal coverage, pass rate, average latency and p95 latency. Signal coverage and response length are deterministic heuristics, not a human-quality judgement or ground-truth accuracy metric. When `NEXUS_DEMO_MODE=true`, it measures deterministic demo responses, not live model quality.


## Production Security and Deployment

Batch 3 adds API-key authentication, request-size limits, process-local sliding-window rate limiting, explicit CORS origins, security response headers, request IDs, dependency-aware readiness checks, and a non-root Docker runtime.

### API authentication

- In `APP_ENV=production` with `NEXUS_DEMO_MODE=false`, `NEXUS_API_KEY` is required at startup.
- Send the key as `Authorization: Bearer <key>` or `X-API-Key: <key>`.
- `/health/live` and `/health/ready` remain public for platform health checks. The root dashboard remains publicly accessible.
- In public demo mode, API-key auth is disabled by design, but request throttling remains active and shared activity-history endpoints are blocked.
- Generate a secret locally with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Store it in your hosting provider's secret manager; never commit `.env` or a real key.

### Runtime controls

Configure `NEXUS_RATE_LIMIT_PER_MINUTE`, `NEXUS_MAX_REQUEST_BYTES` (for requests with a declared `Content-Length`), and `NEXUS_CORS_ORIGINS` through environment variables. CORS is disabled for cross-origin browser traffic unless exact trusted origins are configured. The rate limiter is in-memory and per process; for multiple workers or replicas, enforce shared limits at an API gateway or reverse proxy. NEXUS does not trust client-supplied `X-Forwarded-For` for rate-limit identity.

### Health checks and containers

- `GET /health/live` reports process liveness.
- `GET /health/ready` checks Ollama chat/embedding models, Qdrant and SQL memory in non-demo mode; it returns HTTP 503 when a required dependency is unavailable.
- The Docker image runs as an unprivileged `nexus` user and includes a liveness health check.
- Render is configured to use `/health/live`. For a real production deployment, use managed persistent SQL storage and configure reachable Ollama/Qdrant services; the public Render demo remains deterministic and stateless.

## Coordinated 8-area engineering upgrade

The current upgrade strengthens all eight engineering areas while preserving
the existing project architecture:

1. **Agents:** consistent response contracts, bounded profile context, and
   explicit failure for empty model output.
2. **Orchestration:** bounded sequential context transfer, explicit untrusted
   agent-output boundaries, resilient activity logging, and per-agent metrics.
3. **Decision engine:** source-attributed indicators, negative-signal handling,
   review escalation, and documented heuristic limits.
4. **Memory:** identifier and profile validation, JSON safety, bounded profile
   size, and unique default chat sessions.
5. **RAG:** bounded documents/metadata/queries, score-threshold validation,
   vector-size validation, and user-filtered retrieval.
6. **Evaluation:** validated benchmark cases, stronger signal matching,
   per-domain reporting, median/P95 latency, and atomic report writes.
7. **Security:** bounded in-process rate-limiter state, complete demo activity
   blocking, and privacy-safe application logs.
8. **CI/CD:** Python 3.11/3.12 test matrix, compile/dependency checks, tracked
   `.env` guard, and a Docker build check.

See [`docs/UPGRADE_NOTES.md`](docs/UPGRADE_NOTES.md) for exact behavior and
security boundaries.

### Important identity boundary

`NEXUS_API_KEY` is a shared service credential, not a per-user identity token.
Request-supplied `user_id` and `session_id` values are not cryptographically
bound to a verified principal. Persistent profile, memory, and per-user RAG
features must remain in a trusted local or single-operator environment until
OIDC/JWT identity and per-user authorization are implemented. Do not describe
the current system as a multi-tenant production service.

---

## Engineering Portfolio

This repository is part of a four-project engineering portfolio spanning data engineering, machine learning operations, cloud infrastructure automation, and applied AI engineering.

| Project | Engineering Focus | Technology |
|---|---|---|
| [ATLAS](https://github.com/touc996-svg/atlas) | Event-driven data engineering and lakehouse analytics | Python, Kafka, PostgreSQL, Spark, Parquet, dbt, FastAPI |
| [APEX MLOps](https://github.com/touc996-svg/apex-mlops) | Reproducible ML lifecycle, model governance and monitoring | Python, scikit-learn, MLflow, FastAPI, Docker |
| [ORBIT](https://github.com/touc996-svg/orbit-cloud-automation) | AWS infrastructure automation and security validation | Terraform, AWS, Python, GitHub Actions |
| [NEXUS AI](https://github.com/touc996-svg/nexus-ai) | Multi-agent AI, RAG architecture and intelligent orchestration | FastAPI, Qwen3, Ollama, Qdrant, Nomic Embeddings, SQL |

### Portfolio Verification Highlights

- **ATLAS:** Verified local end-to-end pipeline execution, 3/3 dbt models and 9/9 dbt data-quality tests.
- **APEX MLOps 0.3.0:** Model registry, artifact integrity, controlled retraining, protected inference API and drift monitoring.
- **ORBIT:** Terraform validation, security scanning, infrastructure preflight and CI. This is an infrastructure baseline; no live AWS deployment is claimed.
- **NEXUS AI 0.7.0:** 70 passing local tests, 12/12 reported live integration checks, successful Qwen3 inference, 768-dimensional Nomic embeddings, and successful GitHub CI for commit `5c6a949`. RAG endpoints passed HTTP smoke tests; retrieval quality remains a separate validation item.

Each project has its own architecture, setup instructions, tests and documented limitations. The repositories can be reviewed and run independently.

## Evaluation harness (`evals/`)

An offline, deterministic harness that measures retrieval and grounded answering against a golden dataset
(13 documents, 25 answerable questions including paraphrased, hard and multi-document ones, plus 4
unanswerable questions). No model calls and no network, so it runs in CI in under a second.

```bash
python -m evals.run --compare        # bm25 vs tf-idf vs hybrid (RRF), side by side
python -m evals.run --check          # exit 1 if any metric regresses vs evals/baseline.json
python -m evals.run --update-baseline
```

Metrics: hit@1, recall@3, MRR, nDCG@3 (retrieval); answer correctness, faithfulness (lexical support of the
answer by its cited sources), citation precision (answers); correct and false abstention (unanswerable vs
answerable questions). `tests/test_eval_harness.py` includes mutation checks: a deliberately broken retriever
and a deliberately hallucinating answerer must each trip the gate.

Systems compared (`python -m evals.run --compare`): BM25, TF-IDF, lexical hybrid (RRF), dense
(`nomic-embed-text` via local Ollama), dense+lexical hybrid, and hybrid followed by a local-LLM reranker
(`qwen3:1.7b`, temperature 0, fixed seed). Embeddings and reranker judgements are cached in `evals/cache/`
so CI and tests run offline; a cache miss with no Ollama is an error, never a guess. To refresh after changing
the dataset: run with Ollama up, then `python -m evals.run --system rerank --update-baseline`.

| metric | bm25 | lexical hybrid | dense | hybrid + dense | + LLM rerank (baseline) |
|---|---|---|---|---|---|
| hit@1 | 0.88 | 0.88 | 0.84 | 0.88 | **0.92** |
| recall@3 | 0.92 | 0.92 | 0.98 | 0.94 | **1.00** |
| MRR | 0.91 | 0.91 | 0.92 | 0.92 | **0.95** |
| answer correct | 0.84 | 0.84 | 0.76 | 0.84 | 0.84 |
| false abstention | 0.08 | 0.08 | 0.20 | 0.08 | 0.08 |

Findings, stated plainly: the reranker improves retrieval (MRR 0.91 to 0.95, recall@3 to 1.00). Dense search
alone raises recall but hurts answers (false abstention 0.20), so it is only useful fused or reranked. Answer
correctness did not improve (0.84): the bottleneck is now the extractive answerer and its lexical abstention
rule, not retrieval. The next step is a grounded LLM generator evaluated by this same harness. Faithfulness
is a lexical proxy, not a model-judged measure. The dataset is small (25 answerable questions), so differences
of one question move a metric by 0.04; treat results as a regression guard, not a benchmark.

### LLM generator (`--answerer llm`)

A grounded generator (`qwen3:1.7b` via Ollama) answers only from the top-3 reranked passages, cites passage ids,
and must reply `INSUFFICIENT_INFORMATION` when the evidence is missing. Generations are cached like the rest.
Same retrieval (rerank), different answerer:

| metric | extractive (gated baseline) | LLM 1.7B |
|---|---|---|
| answer correct | **0.84** | 0.68 |
| faithfulness (lexical proxy) | **1.00** | 0.76 |
| citation precision | 0.72 | **0.93** |
| false abstention | **0.08** | 0.20 |
| correct abstention | 1.00 | 1.00 |

The small LLM is worse than the extractive baseline, so the extractive answerer stays the gated baseline. What
the harness exposed (not tuned away, since tuning the prompt on the same 25 questions would invalidate them):
five answerable questions were refused even though retrieval supplied the right passage; three answers were
wrong (a score stated as 1 where the source says 3; a duplicate-prevention answer naming the wrong
components); one correct answer was cited to the wrong document, which citation precision caught. Lexical
faithfulness is noisy when a model paraphrases, so the next step is a model-judged faithfulness metric and a
larger local model, both measured on a held-out question set.
