"""Read-only analysis tools over AEGIS-X findings. TEXT/DATA OUTPUT ONLY.

No tool executes an action, calls a cloud API or mutates AEGIS-X data. Every
result cites finding_ids/native_ids and lists uncertainties; nothing is
inferred beyond the loaded records.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Callable

from app.integrations.aegis_x.models import SEVERITY_RANK, Finding
from app.integrations.aegis_x.store import AegisXSnapshot, load_snapshot

MAX_RESULTS = 200
READ_ONLY_TOOLS = (
    "query_findings", "summarize_incident", "correlate_evidence",
    "explain_risk", "recommend_actions",
)


def _cite(f: Finding) -> dict:
    return {"finding_id": f.finding_id, "native_id": f.native_id, "cloud": f.cloud, "provider": f.provider}


def _brief(f: Finding) -> dict:
    return {
        **_cite(f), "severity": f.severity, "title": f.title, "resource_id": f.resource_id,
        "account_id": f.account_id, "region": f.region, "status": f.status,
        "observed_at": f.observed_at.isoformat(), "finding_type": f.finding_type,
        "mitre_techniques": f.mitre_techniques,
    }


def _gaps(findings: list[Finding]) -> list[str]:
    out = []
    for label, test in (
        ("native_id", lambda f: not f.native_id),
        ("resource_id", lambda f: not f.resource_id),
        ("evidence references", lambda f: not f.evidence),
        ("status", lambda f: f.status in (None, "unknown")),
    ):
        ids = [f.finding_id for f in findings if test(f)]
        if ids:
            out.append(f"Missing {label} on: {', '.join(ids[:10])}" + (" ..." if len(ids) > 10 else ""))
    return out


class AegisXReadOnlyTools:
    def __init__(self, loader: Callable[[], AegisXSnapshot] | None = None):
        self._loader = loader or load_snapshot

    # ---- helpers
    def _snap(self):
        return self._loader()

    @staticmethod
    def _unavailable(snap: AegisXSnapshot) -> dict:
        return {"status": "unavailable", "reason": snap.error, "evidence": [],
                "uncertainty": ["No AEGIS-X data loaded; no conclusions can be drawn."]}

    @staticmethod
    def _envelope(status, **kw):
        return {"status": status, "read_only": True, **kw}

    def _resolve(self, snap, finding_ids):
        found, missing = [], []
        for fid in finding_ids:
            (found if fid in snap.findings else missing).append(snap.findings.get(fid) or fid)
        return found, missing

    # ---- tools
    def query_findings(self, cloud=None, severity=None, resource=None, since=None, until=None, limit=50):
        snap = self._snap()
        if snap.error:
            return self._unavailable(snap)
        limit = max(1, min(int(limit), MAX_RESULTS))
        rows = list(snap.findings.values())
        if cloud:
            rows = [f for f in rows if f.cloud == cloud]
        if severity:
            rows = [f for f in rows if f.severity == severity]
        if resource:
            r = resource.lower()
            rows = [f for f in rows if f.resource_id and r in f.resource_id.lower()]
        if since:
            rows = [f for f in rows if f.observed_at >= _utc(since)]
        if until:
            rows = [f for f in rows if f.observed_at <= _utc(until)]
        rows.sort(key=lambda f: (-SEVERITY_RANK[f.severity], -f.observed_at.timestamp()))
        shown = rows[:limit]
        unc = _gaps(shown)
        if snap.quarantine:
            unc.append(f"{len(snap.quarantine)} record(s) were quarantined as invalid and are excluded.")
        if not rows:
            unc.append("No findings matched; absence of findings is not proof of absence of activity.")
        return self._envelope(
            "ok", total_matches=len(rows), returned=len(shown),
            findings=[_brief(f) for f in shown],
            evidence=[_cite(f) for f in shown], uncertainty=unc,
        )

    def summarize_incident(self, incident_id: str):
        snap = self._snap()
        if snap.error:
            return self._unavailable(snap)
        inc = snap.incidents.get(incident_id)
        if inc is None:
            return self._envelope("not_found", incident_id=incident_id, evidence=[],
                                  uncertainty=[f"Incident {incident_id!r} is not in the loaded data."])
        found, missing = self._resolve(snap, inc.finding_ids)
        found.sort(key=lambda f: f.observed_at)
        unc = _gaps(found)
        if missing:
            unc.append(f"Incident references finding_ids not present in loaded data: {', '.join(missing)}")
        if not inc.finding_ids:
            unc.append("Incident lists no finding_ids; nothing supports its scope.")
        sev = Counter(f.severity for f in found)
        return self._envelope(
            "ok", incident_id=inc.incident_id, title=inc.title, incident_status=inc.status,
            declared_severity=inc.severity, finding_count=len(found),
            severity_counts=dict(sev),
            clouds=sorted({f.cloud for f in found}),
            resources=sorted({f.resource_id for f in found if f.resource_id}),
            time_span={"first": found[0].observed_at.isoformat(), "last": found[-1].observed_at.isoformat()} if found else None,
            timeline=[_brief(f) for f in found],
            evidence=[_cite(f) for f in found], uncertainty=unc,
            note="Summary lists recorded facts only; it does not assert compromise.",
        )

    def correlate_evidence(self, finding_ids: list[str]):
        snap = self._snap()
        if snap.error:
            return self._unavailable(snap)
        found, missing = self._resolve(snap, finding_ids)
        unc = _gaps(found)
        if missing:
            unc.append(f"Unknown finding_ids: {', '.join(missing)}")
        links = []
        def group(label, keyfn):
            buckets: dict = {}
            for f in found:
                for k in keyfn(f):
                    buckets.setdefault(k, []).append(f)
            for k, fs in buckets.items():
                if len(fs) > 1:
                    links.append({"shared": label, "value": k,
                                  "finding_ids": [f.finding_id for f in fs],
                                  "native_ids": [f.native_id for f in fs]})
        group("resource_id", lambda f: [f.resource_id] if f.resource_id else [])
        group("account_id", lambda f: [f.account_id] if f.account_id else [])
        group("mitre_technique", lambda f: f.mitre_techniques)
        group("evidence_ref", lambda f: [f"{e.type}:{e.ref}" for e in f.evidence])
        if len(found) >= 2 and not links:
            unc.append("No shared resource, account, technique or evidence reference was found; "
                       "these findings are not shown to be related by the available data.")
        if len(found) >= 2 and links:
            unc.append("Shared attributes indicate possible relation only; they do not establish causation.")
        found.sort(key=lambda f: f.observed_at)
        return self._envelope("ok", links=links, timeline=[_brief(f) for f in found],
                              evidence=[_cite(f) for f in found], uncertainty=unc)

    def explain_risk(self, finding_id: str):
        snap = self._snap()
        if snap.error:
            return self._unavailable(snap)
        f = snap.findings.get(finding_id)
        if f is None:
            return self._envelope("not_found", evidence=[], uncertainty=[f"finding_id {finding_id!r} not loaded."])
        facts = [f"Severity (normalized): {f.severity}" + (f"; provider-native severity: {f.native_severity}" if f.native_severity is not None else ""),
                 f"Title: {f.title}", f"Observed at {f.observed_at.isoformat()} on {f.cloud}/{f.provider}"]
        if f.finding_type:
            facts.append(f"Provider finding type: {f.finding_type}")
        if f.resource_id:
            facts.append(f"Affected resource: {f.resource_id}")
        if f.mitre_techniques:
            facts.append(f"Mapped MITRE techniques (as reported): {', '.join(f.mitre_techniques)}")
        if f.status:
            facts.append(f"Status: {f.status}")
        if f.description:
            facts.append(f"Provider description: {f.description}")
        unc = _gaps([f])
        unc.append("Impact, exploitability and whether activity succeeded are not determinable from this record alone.")
        if f.status == "archived":
            unc.append("Finding is archived; current exposure is unknown.")
        return self._envelope("ok", finding=_brief(f), recorded_facts=facts,
                              assessment=f"Recorded severity is {f.severity}; treat as a lead to verify, not a confirmed compromise.",
                              evidence=[_cite(f)], uncertainty=unc)

    def recommend_actions(self, finding_ids: list[str]):
        """TEXT-ONLY suggestions for a human. Nothing is executed or queued."""
        snap = self._snap()
        if snap.error:
            return self._unavailable(snap)
        found, missing = self._resolve(snap, finding_ids)
        recs = ["Verify the finding in the source cloud console/tool before acting; confirm it is not a false positive.",
                "Preserve relevant logs and the original provider record (native_id) for investigation."]
        if any(f.severity in ("critical", "high") for f in found):
            recs.append("Escalate to the security owner for triage; high/critical severity was recorded.")
        res = sorted({f.resource_id for f in found if f.resource_id})
        if res:
            recs.append("Review access and configuration of: " + ", ".join(res) + ".")
        if any(f.mitre_techniques for f in found):
            recs.append("Review detections and hardening related to the reported MITRE techniques: "
                        + ", ".join(sorted({t for f in found for t in f.mitre_techniques})) + ".")
        recs.append("Any containment or remediation must be decided and performed by a human operator "
                    "through the owning team's approved process; NEXUS does not execute actions.")
        unc = _gaps(found)
        if missing:
            unc.append(f"Unknown finding_ids: {', '.join(missing)}")
        unc.append("Recommendations are generic and not tailored to your environment or change policy.")
        return self._envelope("ok", recommendations=recs, executes_actions=False,
                              evidence=[_cite(f) for f in found], uncertainty=unc)


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
