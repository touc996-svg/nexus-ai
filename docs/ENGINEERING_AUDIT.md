# NEXUS AI engineering audit

| Revision | Audited state | Where it lives | Scope |
|---|---|---|---|
| v1 (Phase 1) | `main` @ `0c4c6bf`, before PRs #3, #4, #5 | branch `docs/engineering-audit` @ `f063c2a` (never merged to `main`) | first audit |
| **v2 (this update)** | `main` @ `86ed852` (merge of PR #5) | this file | status update after PRs #3, #4, #5 |

v1 described a repository that no longer exists in that form. This revision re-checks every v1 claim against `main` @ `86ed852`.
This is a documentation update only: no application code, dataset, cache, fingerprint or historical report was changed by it.

Status labels (a capability is only called complete if the repository supports that conclusion):

| Label | Meaning |
|---|---|
| VERIFIED COMPLETE | implemented, and checked by tests and/or CI that were actually run, within the scope stated on the row |
| IMPLEMENTED BUT NOT VERIFIED | code exists, but the check that would verify it has not been run |
| PARTIALLY IMPLEMENTED | some of the described capability exists; the gap is stated |
| NOT STARTED | not present in the repository |
| BLOCKED | cannot proceed until an external dependency or decision exists |

## 0. What changed since v1

| PR | Merge commit | Size | What it changed |
|---|---|---|---|
| #3 offline RAG evaluation harness | `0e9be03` | 18 files, +1,811 / -0 | `evals/` package: datasets, metrics, retrievers, answerers, regression gate in CI |
| #4 tenant isolation | `8f579a7` | 10 files, +444 / -43 | `app/security/identity.py`, per-user API keys, scoped routes, 20 tests |
| #5 real-NexusRAG evaluation | `86ed852` | 36 files, +10,054 / -35 | adapter, frozen configuration, held-out results, cache fixes, recovery tool and report, documentation |

Production code (`app/`) changed in only four files since v1: `app/security/identity.py` (new), `app/security/hardening.py`, `app/main.py`,
`app/api/knowledge.py`; all in PR #4. `app/services/rag.py` is unchanged. Every other v1 finding about `app/` is therefore re-confirmed by that diff, not
re-derived. `requirements.txt`, `Dockerfile` and `render.yaml` are also unchanged since v1.

## 1. Current architecture

```
client -> security middleware (API key -> Principal, per-process rate limit, size cap, CORS, request id)
       -> app/main.py (17 route handlers) + routers: /knowledge, /system/*, /health/*, /integrations/aegis-x/* (33 handlers in total)
identity layer (app/security/identity.py): Principal = scoped user | operator | unauthenticated (dev/demo)
orchestrator -> keyword router -> domain agent (enterprise | personal | cybersecurity) -> decision engine (heuristics)
agent -> OpenAI-compatible client -> Ollama (chat model)
RAG   -> NexusRAG (dense only) -> Ollama /api/embed (nomic-embed-text) -> Qdrant (cosine, tenant filter on user_id)
state -> SQLAlchemy / SQLite: memory, profiles, activity
AEGIS-X integration -> read-only findings/incidents loader and 5 tools
evals/ (not imported by app/) -> harness, metrics, BM25/TF-IDF/dense/RRF/LLM-rerank experiments, NexusRAG adapter,
                                 fingerprinted replay caches, frozen configuration, reports, recovery and cleanup tools
```

Size on `main`: `app/` 4,207 lines, `tests/` 2,358, `evals/` 1,390 (Python). `app/main.py` is 624 lines.

## 2. Capability status

### 2.1 Tenant isolation, authentication and authorization (v1 weakness S1)

**Previous S1 status (v1): NOT STARTED** (capability row "Per-user authorization"), with weakness S1 rated High and verified by code.
**New S1 status: PARTIALLY IMPLEMENTED.** The specific defect is fixed in code and tested for scoped keys; the secure configuration is opt-in and the
wider authentication and authorization story is not finished. It is deliberately not labelled complete.

| Part of S1 | Status | Evidence |
|---|---|---|
| Identity-bound access on the 11 user-data handlers (`/memory/{session_id}` x3, `/profile/{user_id}` x3, `/chat`, `/knowledge/ingest`, `/knowledge/sources`, `DELETE /knowledge/sources/{source_id}`, `/knowledge/search`) | VERIFIED COMPLETE for scoped-key requests | `Depends(get_principal)` plus `resolve_user_id` / `scope_session_id` in `app/main.py` and `app/api/knowledge.py`; `tests/test_tenant_isolation.py` (20 tests) pass locally and in CI |
| Cross-user access returns 403; same session name does not collide across users; scoped users cannot read the shared `system` pool | VERIFIED COMPLETE | same tests |
| Missing identity fails closed when authentication is required | VERIFIED COMPLETE | `get_principal`; test `test_missing_principal_fails_closed_when_auth_is_required` |
| Per-user key configuration is validated (length, duplicates, reuse of the operator key, reserved `system` id) | VERIFIED COMPLETE | `parse_user_keys`; parametrized tests |
| Malformed credential returns 401, not 500 | VERIFIED COMPLETE | regression test for the non-ASCII header bug found during PR #4 review |
| Isolation is opt-in | PARTIALLY IMPLEMENTED | per-user keys come from `NEXUS_API_KEYS`; a deployment that sets only the legacy `NEXUS_API_KEY` gets an **operator** principal with access to every user's data |
| Authentication switched off (local development, public demo mode) | PARTIALLY IMPLEMENTED | no identity and no enforcement by design; must never hold real data |

Residual exposures that this change does **not** close (read from the code, not exploited):
- The `/integrations/aegis-x/*` routes (6) have no identity dependency: any authenticated key, scoped or operator, reads the same organisation-wide findings.
- `/activity`, `/activity/summary` and `/system/*` are global (the activity log holds agent names, durations and error text, not user content).
- Sessions and profiles created before PR #4 are reachable only with the operator key.

**Tenant isolation is not complete authentication and authorization.** What is still outstanding:

| Capability | Status |
|---|---|
| OIDC / JWT identity | NOT STARTED |
| API-key rotation, expiry and revocation (keys are environment variables) | NOT STARTED |
| Roles or permissions beyond "scoped user" and "operator" | NOT STARTED |
| Rate limiting | PARTIALLY IMPLEMENTED: a sliding-window limiter exists, but it is per process and keyed by the client address (`request.client.host`), so it is neither shared across workers nor per identity, and clients behind a proxy share one key |

### 2.2 Evaluation, reproducibility and caches

| Capability | Status | Evidence and scope |
|---|---|---|
| Offline RAG evaluation harness (PR #3) | VERIFIED COMPLETE as a regression guard | deterministic and offline; `tests/test_eval_harness.py` (36 tests); dev regression gate `python -m evals.run --check` passes locally and runs in CI (job logs show "quality gate passed" on Python 3.11 and 3.12) |
| Real `NexusRAG` evaluation adapter, in-process Qdrant client | VERIFIED COMPLETE for that scope | `evals/nexus_adapter.py`; `tests/test_nexus_adapter.py` (33 test items). Uses real ingestion, chunking, tenant filter, score threshold and `qdrant-client` local mode; the embedding HTTP response is replayed from a cache |
| Real Qdrant **server** integration | IMPLEMENTED BUT NOT VERIFIED | the server code path and `test_against_a_real_qdrant_server` exist; the test is **skipped** (no `EVALS_QDRANT_URL`; Docker was not running). No Qdrant server has been exercised |
| Frozen configuration and a single held-out run | VERIFIED COMPLETE | `evals/frozen_nexus_config.json` (sha `e5bad09980fb8f5f`) committed before the held-out run; a test fails if production RAG code, defaults, prompts or datasets change |
| Held-out reproducibility | VERIFIED COMPLETE | 12 of 12 recorded reports (6 dev, 6 held-out) replay exactly with inference blocked: `test_recorded_reports_replay_exactly` (12 parametrized cases) pass in CI |
| Cache consistency: merge-on-flush with a file lock, usage tracking that counts writes | VERIFIED COMPLETE (POSIX) | `tests/test_eval_cache.py` (16 tests); each original defect was reintroduced by hand and the tests failed (manual mutation checks, not automated in CI). Locking uses `fcntl`; Windows is untested |
| Held-out recovery (44 lost generations rebuilt) | VERIFIED COMPLETE | 44 of 44 matched the original reports exactly, 0 differed (`evals/recovery/heldout_llm_rebuild.json`); no nondeterminism was observed, but the determinism probe only runs on entries that differ, so this shows those 44 calls reproduced, not that the model is deterministic |
| Cache cleanup tool (`evals.prune_cache`) | PARTIALLY IMPLEMENTED | safe by construction (refuses on an unreplayable workload, flushes first, counts writes as use) but **currently unusable**: it refuses and changes nothing because eight workload combinations are not replayable (section 4, L9) |
| Model inference blocked during tests | VERIFIED COMPLETE | `tests/conftest.py` closes the model endpoints and sets the inference switch before any test imports |
| Independent external benchmark | NOT STARTED | see L1 and L2 |

### 2.3 Production RAG, agents and platform capabilities (v1 rows, re-checked)

| Area | Status | What exists / what does not |
|---|---|---|
| Dense retrieval over Qdrant with tenant filter, embedding validation, idempotent re-ingestion | VERIFIED COMPLETE (unchanged) | `rag.py` untouched; `test_rag_indexing.py`, `test_rag_demo_mode.py` |
| Upload checks in `POST /knowledge/ingest` (allowed extensions, 20 MB cap) | IMPLEMENTED BUT NOT VERIFIED | the code is in `app/api/knowledge.py`, but no test exercises the 415 or 413 rejection paths. The 413 test in `test_security_hardening.py` covers the separate request-size middleware, not this handler. (v1 grouped this under a verified row; that was too strong.) |
| Hybrid retrieval, reranking in **production** | NOT STARTED | `NexusRAG.search` is dense-only. Lexical hybrid (RRF) and LLM reranking exist **only** in `evals/` as experiments |
| Citations, citation validation, abstention in **production** | NOT STARTED | the cited, structured, refusing answerer lives only in `evals/` |
| Guardrails (prompt-injection, PII, output validation) | PARTIALLY IMPLEMENTED | `guardrails.validate` checks type, emptiness and length only; team context is labelled untrusted without adversarial tests |
| Agent planning, tool selection, MCP, human approval | NOT STARTED | single LLM calls behind a keyword router; only the 5 read-only AEGIS-X tools |
| Provider abstraction (LLM, embeddings) | NOT STARTED | OpenAI SDK pointed at Ollama; embeddings call Ollama over `httpx` |
| OpenTelemetry, token and cost tracking | NOT STARTED | access logs with request ids and an activity log only |
| Caching, model routing, streaming (in the app) | NOT STARTED | the replay caches in `evals/` are evaluation tooling, not an application cache |
| Multimodal ingestion (images, OCR, tables) | NOT STARTED | PDF, DOCX, TXT and MD text extraction only |
| Docker Compose, Kubernetes, Helm | NOT STARTED | none present |
| Non-root container with healthcheck | IMPLEMENTED BUT NOT VERIFIED | read from the `Dockerfile` (dedicated user, `HEALTHCHECK`), unchanged since v1; CI only builds the image, and nothing runs the container to confirm the user or the healthcheck. (v1 labelled this verified; that was too strong.) |
| API versioning, consistent error schema, async DB access | NOT STARTED | unversioned routes, mostly synchronous handlers |

## 3. Verified test and CI state

| Check | Result | How verified |
|---|---|---|
| Full offline suite on `main` | 188 passed, 1 skipped, 0 failed (189 collected) | run locally in this update with model inference blocked |
| The skipped test | `test_against_a_real_qdrant_server` | needs `EVALS_QDRANT_URL` |
| Evaluation gate and frozen configuration | pass | `python -m evals.run --check`, `python -m evals.freeze_nexus_config --check` |
| CI on the PR #5 head (`f9234e1`) | Python 3.11 and 3.12: 188 passed, 1 skipped, "quality gate passed"; Docker build success | read from the CI run logs |
| CI on `main` after the merge (`86ed852`) | Tests (3.11) success, Tests (3.12) success, Docker image build success | read with `gh run view` in this update |
| CI contents | compile, pytest, RAG quality gate, `pip check`, tracked-`.env` check, Docker build | `.github/workflows/nexus-ci.yml` |
| CI does **not** run | linting, formatting, type checking, dependency audit, secret scanning, container scanning, a Qdrant server | not present in the workflow |

Static analysis on `main` (run in this update; there is no lint or type configuration in the repository):

| Tool | v1 | Now | Note |
|---|---|---|---|
| ruff (default rules) on `app/` | 58 findings | 70 | the 12 additional ones are `B008` on FastAPI `Depends()` defaults, the standard idiom |
| ruff on `evals/` and `tests/` | not measured | 28 | not triaged |
| mypy on `app/` | 4 errors in 2 files | 13 errors in 4 files (`knowledge.py` 6, `main.py` 3, `memory.py` 2, `evaluation.py` 2) | **increased** since v1; not triaged |
| bandit on `app/` | 0 | 0 | |
| pip-audit on `requirements.txt` | BLOCKED | BLOCKED | `openai` is still unpinned and `qdrant-client` is a range, so the audit cannot run on the file as written |

## 4. Limitations and risks (preserved and extended)

Evaluation limitations (every one applies to the numbers in `docs/RAG_ADAPTER_EVALUATION.md`):
- **L1. The evaluation dataset is small and single-author (in-house, not independent).** 13 documents; 25 answerable and 4 unanswerable dev questions; 21 and 6 held-out. The documents and both question sets were written by the same party for this project, not by independent annotators. One question moves a metric by about 0.04.
- **L2. Held-out results are not an independent external benchmark.** The held-out split was frozen and run once, which protects against tuning on it, but it comes from the same author and corpus. It is a regression guard, not evidence of general quality.
- **L3. Faithfulness is a lexical proxy.** Token overlap between an answer and its cited passages; it does not detect a fluent misreading of correct words and is noisy when a model paraphrases.
- **L4. The Qdrant integration was tested only with the in-process local client.** A real Qdrant server integration was not verified; the server test is skipped. Reported latency uses in-process Qdrant and an embedding time recorded at first computation, so it is not representative of a networked deployment.
- **L5. Production `NexusRAG` remains dense-only.**
- **L6. Hybrid retrieval and LLM reranking are experimental evaluation-harness features, not production capabilities.** Their rows in the reports fuse or rerank production dense results inside the harness.
- **L7. Tenant isolation does not mean complete authentication and authorization** (section 2.1): isolation is opt-in, the operator key is unrestricted, and AEGIS-X and global endpoints are not scoped.
- **L8. OIDC/JWT, key rotation and revocation, and shared or per-identity rate limiting remain outstanding.**
- **L9. Eight exploratory cache workload combinations remain unreplayable, and cleanup refuses to prune.** They are: dev `dense` and `broken` with the LLM answerer; held-out `bm25`, `tfidf`, `hybrid`, `dense`, `hybrid_dense` and `broken` with the LLM answerer. No test and no recorded report uses them and most were never computed. `python -m evals.prune_cache` exits 1 and changes nothing until the workload is narrowed to what tests and reports use. This is a known cleanup limitation, not a data-integrity issue; it is not addressed here.
- **L10. Whether embedding without `nomic-embed-text` task prefixes affects quality is unresolved:** dev and held-out comparisons point in opposite directions by one or two questions.

Other weaknesses carried from v1 and re-checked:
- **S2 (High, unchanged):** only length validation on prompts; no injection detection, PII handling or output validation.
- **S3 (Medium, unchanged):** demo mode disables authentication by design; `render.yaml` (unchanged) auto-deploys `main` to a free public service in demo mode on every commit.
- **S5 (Medium, unchanged):** dependencies are not reproducible (`openai` unpinned, `qdrant-client` ranged, `pytest` in the production requirements, no lockfile or Dependabot configuration).
- **S6 (Low, unchanged):** `app/core/config.py` defines only `OPENAI_API_KEY`; configuration is read from scattered `os.getenv` calls.
- **Q2 (unchanged):** `app/main.py` mixes routing and logic; routing and risk decisions are keyword matching.
- **New, documentation:** `docs/UPGRADE_NOTES.md` (line 63) still carries the pre-PR-#4 "Identity limitation" paragraph, which now overstates the gap for scoped keys. It was not edited by this update (scope limit) and should be reconciled with the README section "Identity and tenant isolation".
- **New, process:** two incidents are on record: a non-ASCII API-key header returned 500 instead of 401 (found in review of PR #4 and fixed before merge; the original comparison had the same bug), and a cache data-loss incident during PR #5 (section 5). Both are fixed and covered by regression tests.

## 5. Review history

- **PR #3** (`0e9be03`): additive (0 deletions); checks green on its head; merged after approval.
- **PR #4** (`8f579a7`): review found two defects before merge (non-ASCII credentials returned 500; a key mapped to `system` would have exposed the shared pool); both were fixed with regression tests; checks green on its head `8b879ab`; merged after approval.
- **PR #5** (`86ed852`): a stray `git checkout` reverted uncommitted cache entries, and two defects in the cache tooling (overwrite on flush; cleanup dropped fresh entries) left 44 held-out generations unreplayable. The fixes and tests were reviewed, the 44 entries were rebuilt and every one matched the original report. The PR description initially said "one commit, 22 files"; it has two commits and 36 files, and the description was corrected before merge.

## 6. Recommended implementation order

1. **Narrow the cleanup workload** to the combinations tests and reports use, then run `evals.prune_cache` (closes L9). Small and independent.
2. **Pin dependencies; add lint, type, dependency-audit and secret scanning to CI** (S5, Q1; unblocks `pip-audit`). Triage the 13 mypy errors first.
3. **Reconcile `docs/UPGRADE_NOTES.md`** with the PR #4 behaviour.
4. **Run the Qdrant server test** (needs Docker running and a Qdrant container) to turn the "IMPLEMENTED BUT NOT VERIFIED" row into a verified one or a documented failure.
5. **Complete authentication and authorization:** scope or restrict the AEGIS-X and global routes; OIDC/JWT; key rotation and revocation; identity-aware and shared rate limiting.
6. **Phase 3, AI security hardening** (S2): prompt-injection and indirect-injection tests, PII handling, output validation, red-team suite, tool permission boundaries. Not started by this update.
7. Production RAG improvements (hybrid, reranking, cited answers with abstention), then orchestration and approval gates, observability, multimodal, deployment hardening. Use the evaluation harness to decide which retrieval changes actually help, noting L1 to L3.

## 7. Risks and dependencies

- **Memory:** this machine has 8 GB of RAM; a 4B chat model, an embedding model and Qdrant fit one at a time; the 8B model did not run usefully. Live evaluation must use small models and run staged.
- **Docker:** not running in the environment used so far; required for item 4.
- **Breaking changes:** scoping more routes or adding OIDC changes the API contract and must be documented as such.
- **Evidence strength:** conclusions drawn from L1 to L3 are weak; use the harness to catch regressions, not to claim quality.
- **Cost:** nothing here requires a paid service; no cloud deployment is planned.

## 8. How this revision was verified

Commands run in this update (all read-only except creating this file on a local branch):
`git fetch`, `git rev-parse origin/main`, `git diff --stat 0c4c6bf origin/main -- app`, `python -m pytest -q` (inference blocked by `tests/conftest.py`), `grep` over `tests/` for coverage of the upload and container claims, `python -m evals.run --check`,
`python -m evals.freeze_nexus_config --check`, `ruff check`, `mypy`, `bandit`, `pip-audit`, `gh run list`/`gh run view`/`gh pr view`, and `grep` over `app/` for route and identity usage.
The recovery results (44 of 44, 12 of 12) were established during PR #5; this update re-confirms them only through the passing replay tests, not by re-running the rebuild.
The route inventory for section 2.1 was read from the code; a regex helper used while preparing it mis-classified `/knowledge/ingest` (nested parentheses) and was not used as evidence for that route.
