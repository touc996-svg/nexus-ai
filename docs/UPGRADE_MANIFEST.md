# Upgrade manifest

## New files

- `app/agents/contracts.py` — shared agent input/result contracts and bounded profile context.
- `docs/UPGRADE_NOTES.md` — technical details, verification notes, and security limitations.
- `tests/test_nexus_upgrade.py` — regression coverage for the coordinated upgrade.

## Updated files

- `.env.example` — documents the RAG extracted-document character limit.
- `.github/workflows/nexus-ci.yml` — Python 3.11/3.12 test matrix, compile/dependency/secret checks, and Docker build.
- `README.md` — version and coordinated-upgrade overview.
- `app/agents/enterprise_agent.py`, `personal_agent.py`, `cybersecurity_agent.py` — common response shape, input/output validation, and stronger role boundaries.
- `app/core/orchestrator.py` — bounded untrusted handoffs, failure-aware metrics, resilient activity persistence.
- `app/core/router.py` — word-boundary matching, confidence labels, ambiguity detection.
- `app/core/decision_engine.py` — source-aware negative-aware indicators and explicit heuristic limitations.
- `app/core/memory.py`, `database.py` — profile limits and safer SQLite/managed-SQL engine configuration.
- `app/core/llm.py` — degraded health status when either configured Ollama model is missing.
- `app/services/rag.py` — bounded document, metadata, query, identity, and score-threshold inputs.
- `app/services/evaluation.py` — benchmark validation, token-aware signal matching, per-domain metrics, median/P95, atomic writes.
- `app/services/profile_learning.py` — bounded profile context and stricter extracted-value validation.
- `app/security/hardening.py` — bounded in-process limiter state and complete demo activity-path blocking.
- `app/main.py` — unique default chat session IDs, privacy-safe logs, clarification for ambiguous auto-routing, application version 0.7.0.
- `tests/test_security_hardening.py` — retained suite with test definitions kept compatible.

No new third-party dependencies were introduced. Existing SQLite schema, Qdrant collection name, API route names, agent names, and dashboard entry point are retained.
