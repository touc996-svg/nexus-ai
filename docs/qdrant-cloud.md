# Qdrant: local and Qdrant Cloud

## Settings (environment only)
| Variable | Default | Meaning |
|---|---|---|
| `QDRANT_URL` | `http://localhost:6333` | Qdrant endpoint. |
| `QDRANT_API_KEY` | unset | API key. **Secret**: use the host's secret store; never commit, never log. |
| `QDRANT_TIMEOUT_SECONDS` | 30 (1-120) | Per-request timeout. |
| `QDRANT_MAX_RETRIES` | 1 (0-3) | Retries for transport failures and 502/503/504 only, with a short backoff. |

- **Local development** (host `localhost`, `127.0.0.1`, `::1`): unchanged. Plain HTTP, no key needed; a key is accepted.
  Ollama configuration is not affected.
- **Remote host** (for example `https://<cluster>.cloud.qdrant.io:6333`): `https://` and `QDRANT_API_KEY` are both required;
  NEXUS refuses to start the RAG client otherwise, so a key is never sent in clear text and an unauthenticated remote store is
  never used by accident. Certificates are verified (httpx defaults; verification is not disabled anywhere).
- Failures: persistent transport failures raise `QdrantUnavailable` with a message that contains no URL, key or request data;
  401/403 and other client errors are **not** retried. `/chat` already continues without retrieval when Qdrant fails;
  `/health/ready` reports `offline:<ErrorType>`.

## Tenant isolation
Every search, list and delete carries a `user_id` filter built by the application. On start-up NEXUS also creates a keyword
payload index on `user_id` flagged `is_tenant=true` (idempotent; if the key may not create indexes, a warning is logged and
isolation is unaffected because the filter is what enforces it). **The API key is not tenant-scoped**: anyone holding it can
read every tenant's chunks. Use a dedicated cluster/key for NEXUS, restrict the key to that cluster, rotate it by updating the
secret and redeploying, and prefer a Qdrant key with only the permissions NEXUS needs (check Qdrant's current access-control
options; not verified here). Per-tenant JWT access control is not implemented.

## Evaluation cache
The replay cache used by the RAG quality gate is keyed on the source of the members that determine vectors and retrieval
(`embed`, `chunk_text`, `ingest_text`, `search`, `_source_id`, `_user_filter`, `_ensure_collection`; see
`evals/nexus_adapter.py:HASHED_MEMBERS`), no longer on the whole `rag.py`. Changing how Qdrant is reached therefore does not
invalidate recorded vectors, and any change to a hashed member still does (the gate then fails closed because inference is
blocked in CI). Vectors recorded under the old whole-file key are adopted once, only while the hashed members hash to the
pinned value proving they are unchanged. `evals/frozen_nexus_config.json` was regenerated: only `rag_py_sha` changed.

## Testing
```bash
docker run -d --name q -p 6333:6333 qdrant/qdrant:v1.20.0
docker run -d --name qa -p 6334:6333 -e QDRANT__SERVICE__API_KEY=local-test-key qdrant/qdrant:v1.20.0
NEXUS_TEST_QDRANT_URL=http://localhost:6333 NEXUS_TEST_QDRANT_AUTH_URL=http://localhost:6334 \
NEXUS_TEST_QDRANT_API_KEY=local-test-key python -m pytest -q tests/test_qdrant_server.py tests/test_qdrant_config.py
```
Not tested: a real Qdrant Cloud cluster (TLS to Qdrant's endpoint, cloud key semantics).
