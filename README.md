
# NEXUS AI

### Unified Enterprise, Personal Assistant & Cybersecurity AI Platform

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
