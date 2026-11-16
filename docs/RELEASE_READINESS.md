# NEXUS AI release readiness

**NEXUS AI is not production-ready for untrusted multi-user exposure.** This page says what has been checked, what has
not, and what is blocked, so a release decision rests on evidence. It does not certify anything. Section 4 of
`docs/ENGINEERING_AUDIT.md` has the detail behind the security rows.

| Status | Meaning |
|---|---|
| VERIFIED | a check was run; the command and its real result are given |
| NOT VERIFIED | code or a claim exists, but the check that would confirm it was not run |
| BLOCKED | the check cannot run until something external exists |
| NOT IMPLEMENTED | the capability is not in the repository |

## 1. Evidence

Run on the application code and tests of the Phase 2 follow-up (commit `664ad99` and the documentation after it), with
`NEXUS_DEMO_MODE=true APP_ENV=test`. Exit codes were captured directly, not read from a truncated log.

| Check | Status | Result and command |
|---|---|---|
| Test suite | VERIFIED | 459 passed, 1 skipped, 0 failed: `python -m pytest -q` |
| Evaluation regression gate | VERIFIED | "quality gate passed": `python -m evals.run --check` |
| Frozen retrieval configuration | VERIFIED | matches: `python -m evals.freeze_nexus_config --check` |
| Compilation | VERIFIED | `python -m compileall -q app evals tests` |
| Lint (ruff, default rules) | VERIFIED | no new findings: `app` 70 and `tests` plus `evals` 28, identical to `main` (the existing findings are not triaged) |
| Security static analysis (bandit) | VERIFIED | 0 issues in `app`, as on `main`: `bandit -r app` |
| Type check (mypy) | VERIFIED | 13 errors in the same 4 files as `main`, none in the new modules; not triaged: `mypy app --ignore-missing-imports` |
| Installed dependencies consistent | VERIFIED | `pip check`: no broken requirements |
| Dependency vulnerability audit | BLOCKED | `pip-audit` cannot run on `requirements.txt` while `openai` is unpinned; not re-run here |
| Docker image build | BLOCKED | the Docker daemon is not running (`docker info` exits 1). CI builds the image; it was not built locally |
| Qdrant server integration | NOT VERIFIED | `test_against_a_real_qdrant_server` is skipped (`EVALS_QDRANT_URL` unset); only the in-process Qdrant client was tested |
| Continuous integration on this change | NOT VERIFIED | nothing was pushed, so the workflow has not run on it |
| Behaviour with a live model | NOT VERIFIED | every test blocks inference; nothing here claims live-model quality or safety |

**Local demonstration (VERIFIED, 2026-11-16).** Started with `uvicorn app.main:app` on a scratch SQLite database, against the
local Ollama models (`qwen3:1.7b`, `nomic-embed-text`), with no Qdrant server running. Live, with real model output:
`/agents/enterprise`, `/agents/personal` and `/agents/cybersecurity` each answered (5 to 12 s); `/auto` routed a ransomware,
a certification-study and a regional-expansion prompt to cybersecurity, personal and enterprise, each with high confidence, and
asked for clarification on "Tell me something."; `/team` ran all three agents and produced the final report (52 s); two `/chat`
turns in one session were stored (4 messages) and the second turn used the first (the agent answered about the exam the user
had named), and the profile learned the stated goal; empty, blank and oversized input returned 422, 400 and 422. With retrieval
working (in-memory Qdrant, real embeddings) `/chat` answered from a 1,527-word document after the composed-message fix.
Not demonstrated: `/knowledge/search` against the default backend returns 500 and `/health/ready` reports not ready, because
no Qdrant server is reachable (Docker is not running here); that is the documented dependency, not a code defect, though a
503 would describe it better than a 500. The 1.7B model sometimes returns malformed intent JSON, which `/chat` turns into
"Could you rephrase your request"; a larger model (`NEXUS_MODEL=qwen3:4b` is installed) reduces that.

Each security feature in section 2 was also mutation-checked on a scratch copy (a protection is removed and the tests
must fail). That check is a throwaway script, not part of CI.

## 2. Security configuration for a deployment

None of this is on by default, and a green test suite does not mean a deployment is configured this way.

- [ ] `APP_ENV=production` and `NEXUS_DEMO_MODE=false`. The application then refuses to start without an API key.
- [ ] `NEXUS_API_KEY`: a random secret (`python -c "import secrets; print(secrets.token_urlsafe(48))"`) held in a secret
      manager. It is the administrator credential and the break-glass credential. Never give it to end users.
- [ ] Per-user access: stored keys (`NEXUS_KEYSTORE_ENABLED=true`, keys issued with `python -m app.security.keystore`),
      `NEXUS_API_KEYS`, or bearer tokens (`NEXUS_JWT_*`). Each confines a caller to their own data.
- [ ] Administrator-only routes (AEGIS-X, `/system/*`, `/activity*`, `/memory/status`, `/decision-engine/status`) are reached
      only with an administrator credential. Check by calling one with a per-user key and expecting 403.
- [ ] Rotation: stored keys with `rotate --overlap-days`; environment keys by redeploying; token secrets by listing the new
      secret beside the old in `NEXUS_JWT_SECRETS`, switching the issuer, then removing the old one once its tokens have
      expired (at most `NEXUS_JWT_MAX_LIFETIME_SECONDS`).
- [ ] Revocation: stored keys with `revoke` (effective on the next request). Environment keys and tokens cannot be revoked
      without a redeploy or a secret change; keep tokens short-lived.
- [ ] Explicit `NEXUS_CORS_ORIGINS`, and `NEXUS_RATE_LIMIT_PER_MINUTE` / `NEXUS_RATE_LIMIT_PER_IDENTITY_PER_MINUTE` set
      deliberately. Both limits are per process: put a gateway or reverse proxy in front for limits shared across workers,
      and for TLS.
- [ ] A managed SQL database through `DATABASE_URL` if more than one instance runs (the default is a local SQLite file).
      Not verified here.
- [ ] Run the key-management command with the same `DATABASE_URL` as the server; it prints which database it changed.

What enforces these, and the tests behind them, is in `README.md` ("Identity and tenant isolation") and
`docs/ENGINEERING_AUDIT.md` section 2.1.

## 3. Known gaps that block an untrusted multi-user release

Ranked by security and business priority. The first two are the ones a reviewer should ask about.

1. **AI input and output security (Phase 3) is not started.** No prompt-injection or indirect-injection detection, no
   delimiter neutralization, no PII handling, no validation of free-text model output, no adversarial tests
   (`docs/ENGINEERING_AUDIT.md` section 4.1). Instructions that tell a model to treat retrieved text as untrusted are advisory.
2. **No OIDC.** Only HS256 bearer tokens with a shared secret exist. RS256, JWKS and OIDC discovery need `cryptography` and a
   reachable identity provider.
3. Rate limits are not shared across workers or instances (a shared store such as Redis is not available here), and users
   behind one proxy share one per-address budget.
4. The environment keys are plaintext and non-expiring; stored keys fix that only for keys issued through the key store, which
   is one SQL table managed from the command line, not a managed secret service.
5. Dependencies are not reproducible (`openai` unpinned, no lockfile), CI has no lint, type or dependency-audit step, and the
   container and the Qdrant server path were never run in this environment.
6. Production retrieval is dense-only; hybrid retrieval, reranking, citations and abstention exist only in the evaluation harness.
7. No distributed tracing or cost tracking, no multimodal ingestion, no Compose, Kubernetes or Helm.

The public demo (`NEXUS_DEMO_MODE=true`) is deterministic, stateless and has authentication off by design; it must never hold
real data.
