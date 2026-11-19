# AEGIS-X Integration (read-only)

NEXUS AI acts as an **intelligence layer** over normalized AEGIS-X findings. This first
integration is strictly **read-only**.

## What exists now

- **Data access** (`app/integrations/aegis_x/`): loads `findings*.json|jsonl` and
  `incidents*.json|jsonl` from the local directory in `NEXUS_AEGIS_X_DATA_DIR`.
  No network, no credentials, no cloud SDKs. Files are opened for reading only.
- **Validation**: pydantic models mirror the AEGIS-X finding schema (vendored as
  `finding.schema.json`, copied from the AEGIS-X repo; re-sync manually when it changes).
  Invalid, non-JSON or duplicate-id records are **quarantined** (reported by
  `GET /integrations/aegis-x/status`, never silently dropped, never written anywhere).
- **Incidents**: the `Incident` model mirrors AEGIS-X `pipeline/schemas/incident.schema.json`
  (`member_finding_ids`, `highest_severity`, `score`, `grouping_reasons`, ...). A document
  `{"incidents": [...], "uncorrelated_finding_ids": [...]}` is accepted from `incidents*.json`.
  Naive timestamps are quarantined. Verified only against hand-built fixtures: no real pipeline
  output was available. Incident summaries cite AEGIS-X grouping reasons and score but state that
  they indicate possible relation, not confirmed compromise.
- **Tools** (Cybersecurity agent, allowlisted): `query_findings` (cloud, severity, resource,
  time range), `summarize_incident`, `correlate_evidence`, `explain_risk`,
  `recommend_actions` (text only).
- **Endpoints** (routed through `NexusOrchestrator.run_aegis_x_tool` → `CybersecurityAgent`):
  `GET /integrations/aegis-x/status`, `POST .../findings/query`, `.../incidents/summary`,
  `.../evidence/correlate`, `.../risk/explain`, `.../recommendations/text`. POST is used only
  to carry query bodies.
- **Evidence discipline**: every response carries an `evidence` list of `finding_id` /
  `native_id`, and an `uncertainty` list (missing native_id/resource/evidence, unknown ids,
  quarantined records, empty results, correlation ≠ causation). Tools are deterministic and
  do not call an LLM, so they cannot invent facts.
- **Guards** (`tests/test_aegis_x_integration.py`): AST tests assert the module and API import no
  boto3/botocore/azure/google/requests/httpx/urllib/socket/subprocess, make no write/exec calls,
  open files read-only, expose only the allowlisted tools, and register no PUT/PATCH/DELETE routes.

Existing NEXUS API-key auth and rate limiting apply to these routes, and they require
administrator credentials: the operator key (`NEXUS_API_KEY`). A per-user key (`NEXUS_API_KEYS`)
gets 403, because findings are deployment-wide data rather than per-user data. Findings may contain
sensitive resource names: run NEXUS with the API key enabled and keep the data directory private.

## Not implemented: requirements before any response execution

Execution (isolating hosts, disabling keys, changing security groups, etc.) is **out of scope**
and must not be added without all of the following:

1. **Narrow permissions** – a separate executor identity per cloud with least-privilege,
   action-specific roles; short-lived credentials held only by the executor, never by NEXUS or
   its LLM context. Read-only analysis and execution must stay separate components.
2. **Policy controls** – an allowlist of action types, per-account/resource scope, blast-radius
   limits, rate limits, protected-resource denylist, and a global kill switch (default off).
3. **Human approval** – each action proposed as a structured plan, approved by a named human
   with authority; no approval inferred from model output or from text in findings
   (findings are untrusted data and can carry injected instructions).
4. **Idempotency** – deterministic action keys so retries and duplicates cannot double-apply.
5. **Audit trail** – append-only record of proposal, approver, parameters, evidence
   finding_ids, result and timestamps, stored outside the executor's write reach.
6. **Rollback** – every action must define a tested inverse (or be refused), with pre-state
   captured before change and a dry-run mode.

Until these exist, `recommend_actions` returns advisory text and `executes_actions: false`.

## Dashboard (read-only SOC view)

`app/dashboard/index.html` has an "AEGIS-X Security Operations Center" section. It calls only the
read-only routes above plus `GET /integrations/aegis-x/incidents` (metadata list). The operator key
(`NEXUS_API_KEY`) is typed into a password field, kept in JavaScript memory only (never in
localStorage/sessionStorage), sent as `X-API-Key`, and discarded on Disconnect or page hide. 401/403
show a clean message. Data provenance is declared by the operator with `NEXUS_AEGIS_X_DATA_LABEL=demo|live`
and shown as a banner; unset means "undeclared" and the dashboard warns not to assume live data.
