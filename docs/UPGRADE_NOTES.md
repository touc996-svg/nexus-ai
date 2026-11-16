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
Authenticated requests also spend a per-credential-owner budget
(`NEXUS_RATE_LIMIT_PER_IDENTITY_PER_MINUTE`, default: the per-address limit), so
one key used from many addresses shares one budget; like the per-address limit it
is per process, not shared across workers.
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

**Breaking change (administrator-only system routes):** `/integrations/aegis-x/*`,
`/system/*`, `/activity`, `/activity/summary`, `/dashboard/activity`,
`/memory/status` and `/decision-engine/status` now require the operator key
(`NEXUS_API_KEY`). A per-user key (`NEXUS_API_KEYS`) receives 403 on them where it
previously succeeded; a request with no valid key still receives 401. A deployment
that sets only `NEXUS_API_KEYS` (no operator key) can no longer reach these routes
at all: set `NEXUS_API_KEY` to keep an administrator credential. With
authentication off (local development, public demo) nothing changes.

**Stored keys (opt-in, no effect unless enabled):** set `NEXUS_KEYSTORE_ENABLED=true`
to also accept keys issued with `python -m app.security.keystore`. They are generated
by the server, shown once, stored only as a SHA-256 digest, always expire (default
90 days, at most 366), can be revoked (effective on the next request) and rotated
(a replacement plus a short overlap for the old key, never longer than it already
had). Enabling it creates an `api_keys` table in the application database. The
environment keys keep working and stay the bootstrap and break-glass credentials;
at least one is still required, because enabling the store turns authentication on
(with no environment key the application refuses to start). If the store cannot be
read, a stored key gets 503, never access. Keys are managed only from the command
line, which prints the database it changed (password hidden): run it with the same
`DATABASE_URL` as the server.

**Bearer tokens (opt-in, HS256 only, not OIDC):** set `NEXUS_JWT_SECRETS`,
`NEXUS_JWT_ISSUER` and `NEXUS_JWT_AUDIENCE` to also accept
`Authorization: Bearer <token>` signed with a shared secret by a trusted issuer.
Only HS256 is accepted; `iss`, `aud`, `exp` (required, at most
`NEXUS_JWT_MAX_LIFETIME_SECONDS` away) and a valid `sub` are checked, and `nbf` and
`iat` may not be in the future. The user is the verified `sub`; no claim can make a
token an administrator. A partial configuration stops the application at startup,
and enabling tokens turns authentication on (an environment key is still
required). Tokens cannot be revoked before they expire, and there is no replay
tracking. RS256/ES256, JWKS and OIDC discovery are not implemented: they need
`cryptography` and a network path to an identity provider.

This is tenant isolation, not complete authentication and authorization. The
environment keys have no rotation, expiry or revocation (stored keys do, when
enabled), OIDC identity is not implemented (HS256 bearer tokens are, when
configured), there are only two roles (administrator, which is the operator
key, and scoped user), the AEGIS-X, activity and system endpoints are
administrator-only but return deployment-wide data rather than per-user views, and rate
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
