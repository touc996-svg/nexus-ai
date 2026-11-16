# NEXUS AI — Coordinated 8-Area Upgrade

This release builds on the existing NEXUS AI implementation. It does not replace
its FastAPI routes, SQLite tables, Qdrant collection name, Ollama integration,
or dashboard contract.

## 1. Specialized agents

Enterprise, Personal, and Cybersecurity agents now return a consistent result
shape with a lowercase `domain`, `status`, `response` or a bounded generic error.
Empty model output is a failure, not a successful run. Personal profile context
is allowlisted and size-bounded before it is serialized into a prompt.

## 2. Multi-agent orchestration

The team workflow remains intentionally sequential because later agents depend
on earlier outputs. Every downstream agent receives bounded prior output inside
explicit untrusted-output delimiters. Failed or empty agent results are not
presented as usable context. Activity-log persistence errors no longer convert
otherwise successful agent executions into failures. Per-agent timing, response
length, execution order, and a unique execution ID are returned.

## 3. Decision engine

The rule-based engine now reports matched indicators and the agent sources that
contained them. Explicitly negated indicators such as “no data breach” are not
counted as positive findings. Potential critical indicators require review;
they are not presented as verified incidents. The report states its heuristic
limitations explicitly.

## 4. Memory and personalization

Existing SQLite tables and methods are preserved. Session/user identifiers,
profile fields, text lengths, list sizes, JSON serialization, and total profile
size are validated. New API chat sessions receive a unique default session ID
instead of sharing the literal `default` session.

## 5. RAG and knowledge retrieval

The existing Qdrant collection and per-user payload filter are preserved.
Document text, source IDs, user IDs, metadata size, search query size, vector
score thresholds, and embedding vector dimensions are validated. Re-indexing
still creates the replacement embeddings before stale chunks are removed.

## 6. Evaluation and observability

Benchmark case schemas and IDs are validated. Expected-signal matching respects
word boundaries and CamelCase token boundaries. Reports now include per-domain
pass rates and latency, median latency, and p95 latency. Evaluation report writes
use an atomic temporary file replacement.

These are deterministic heuristics. They are not factuality checks, model
judgments, or ground-truth accuracy measurements.

## 7. Security and guardrails

The public demo now blocks `/activity/summary` as well as the other activity
history paths. The in-process sliding-window limiter bounds its client-key map.
Chat logging no longer prints user IDs or RAG scores. The existing API-key
middleware, request-size check for declared `Content-Length`, response headers,
and explicit-origin CORS policy remain in place.

**Identity limitation (updated for tenant isolation):** two credential types are
now supported. A per-user key (`NEXUS_API_KEYS`) binds a request to one user: a
request that names another `user_id` is refused with 403, and chat/memory
`session_id`s are stored in a per-user namespace, so one user cannot read or
overwrite another user's memory, profile or knowledge by naming them.
`NEXUS_API_KEY` remains one shared operator credential with access to every
user's data, and a deployment that sets only that key gets no isolation.
Sessions and profiles created before this change are reachable only with the
operator key. When authentication is switched off (local development, public
demo) there is no identity and no enforcement.

This is tenant isolation, not complete authentication and authorization. Keys are
environment variables (no rotation, expiry or revocation), OIDC/JWT identity is
not implemented, there are no roles beyond scoped user and operator, the
AEGIS-X, activity and system endpoints are not scoped to a user, and rate
limiting is per-process and per-client-address. Do not expose persistent-memory,
profile or RAG features as a multi-tenant production service on this basis alone.
See the "Identity and tenant isolation" section of `README.md` and section 2.1
of `docs/ENGINEERING_AUDIT.md`.

## 8. Testing and CI/CD

Regression tests cover the new agent contract, empty model output, router
ambiguity, orchestrator failure handling, decision negation, profile limits,
RAG limits, evaluator validation, unique chat sessions, bounded rate-limiter
state, and public-demo activity restrictions. CI runs Python 3.11 and 3.12 test
jobs, compile checks, dependency checks, tracked-secret checks, and a Docker
build without pushing an image.

## Local verification

```bash
python -m compileall -q app tests scripts
python -m pytest -q
python -m pip check
```

## Deployment boundary

The included Docker/Render settings support portfolio demonstration and
controlled deployment. A green test suite and successful image build do not
make the service production-ready. Before untrusted multi-user deployment,
implement verified user identity and authorization, shared rate limiting,
managed durable storage, secret management, and operational monitoring.
