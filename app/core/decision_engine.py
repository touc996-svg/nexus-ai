"""Explainable heuristic decision support for multi-agent assessments.

This engine identifies text signals; it does not verify incidents or establish
that a risk is true. Human validation remains required.
"""

from __future__ import annotations

import re


class NexusDecisionEngine:
    RISK_RULES = {
        "security": {
            "severity": "critical",
            "keywords": [
                "data breach", "databreach", "unauthorized access",
                "credential theft", "ransomware", "malware",
                "privilege escalation", "vulnerability", "cyberattack",
            ],
        },
        "compliance": {
            "severity": "high",
            "keywords": [
                "regulatory violation", "non-compliance", "privacy violation",
                "compliance failure", "gdpr violation",
            ],
        },
        "operational": {
            "severity": "high",
            "keywords": [
                "system failure", "service disruption", "downtime",
                "data loss", "production failure",
            ],
        },
        "financial": {
            "severity": "medium",
            "keywords": [
                "financial loss", "budget overrun", "revenue loss",
                "unexpected cost", "cash flow problem",
            ],
        },
    }
    NEGATION_PREFIX = re.compile(
        r"\b(?:no|not|never|without|neither|ruled\s+out|absence\s+of|"
        r"false\s+positive|cannot\s+confirm|unable\s+to\s+confirm)"
        r"(?:\s+\w+){0,4}\s+$",
        re.IGNORECASE,
    )

    @staticmethod
    def _contains_signal(text, phrase):
        if not isinstance(text, str):
            return False
        if phrase == "recommended":
            return re.search(r"(?<!not )\brecommended\b", text, re.IGNORECASE) is not None
        return re.search(
            rf"(?<!\w){re.escape(phrase)}(?!\w)", text, re.IGNORECASE
        ) is not None

    @classmethod
    def _positive_keyword_matches(cls, text, keyword):
        matches = list(re.finditer(
            rf"(?<!\w){re.escape(keyword)}(?!\w)", text, re.IGNORECASE
        ))
        positive = []
        for match in matches:
            prefix = text[max(0, match.start() - 70):match.start()]
            if not cls.NEGATION_PREFIX.search(prefix):
                positive.append(match.group(0).lower())
        return positive

    def generate_report(self, agent_results):
        if not isinstance(agent_results, dict):
            agent_results = {}

        sections = {
            name: self._extract(agent_results.get(name, {}))
            for name in ("enterprise", "personal", "cybersecurity")
        }
        failures = self._identify_failures(agent_results)
        combined_text = " ".join(sections.values()).lower()
        risks = self._identify_risks(combined_text, sections)
        contradictions = self._detect_contradictions(
            sections["enterprise"], sections["personal"], sections["cybersecurity"]
        )
        priorities = self._prioritize(risks, failures, contradictions)
        decision = self._make_decision(risks, failures, contradictions)

        return {
            "executive_summary": (
                "NEXUS AI completed a multi-agent assessment and generated "
                "an explainable, rule-based decision report."
            ),
            "business_analysis": sections["enterprise"],
            "recommendations": sections["personal"],
            "security_assessment": sections["cybersecurity"],
            "risks": risks,
            "risk_assessment": {"total_risks": len(risks), "risks": risks},
            "contradictions": contradictions,
            "priorities": priorities,
            "failed_agents": failures,
            "decision": decision,
            "agents_analyzed": list(sections),
            "engine": "explainable_rule_based_v2",
            "limitations": (
                "Keyword signals are heuristic indicators, not verified facts, "
                "probabilities, or a substitute for human review."
            ),
            "status": "completed_with_warnings" if failures or contradictions or risks else "completed",
        }

    @staticmethod
    def _extract(result):
        if not isinstance(result, dict):
            return str(result)[:12000]
        return str(
            result.get("response") or result.get("output") or result.get("error")
            or "No result available."
        )[:12000]

    @staticmethod
    def _identify_failures(results):
        required_agents = ("enterprise", "personal", "cybersecurity")
        failures = [name for name in required_agents if name not in results]
        for name, result in results.items():
            if (
                not isinstance(result, dict)
                or "error" in result
                or result.get("status") in {"failed", "rejected"}
                or not str(result.get("response") or result.get("output") or "").strip()
            ):
                failures.append(name)
        return failures

    def _identify_risks(self, text, sections=None):
        sections = sections or {"combined": text}
        risks = []
        for category, rule in self.RISK_RULES.items():
            matches = []
            sources = []
            for keyword in rule["keywords"]:
                for source, content in sections.items():
                    if self._positive_keyword_matches(content.lower(), keyword):
                        matches.append(keyword)
                        sources.append(source)
            if matches:
                risks.append({
                    "category": category,
                    "severity": rule["severity"],
                    "indicators": sorted(set(matches)),
                    "sources": sorted(set(sources)),
                    "status": "potential",
                    "verification_required": True,
                })
        return risks

    def _detect_contradictions(self, enterprise, personal, cybersecurity):
        sections = {
            "enterprise": enterprise.lower(),
            "personal": personal.lower(),
            "cybersecurity": cybersecurity.lower(),
        }
        opposing_pairs = [
            ("no risk", "high risk"),
            ("secure", "vulnerable"),
            ("recommended", "not recommended"),
            ("low cost", "high cost"),
            ("no threat", "active threat"),
        ]
        contradictions = []
        for first, second in opposing_pairs:
            found_first = [name for name, content in sections.items() if self._contains_signal(content, first)]
            found_second = [name for name, content in sections.items() if self._contains_signal(content, second)]
            pairs = [(left, right) for left in found_first for right in found_second if left != right]
            if pairs:
                contradictions.append({
                    "conflicting_signals": [first, second],
                    "sources": sorted({source for pair in pairs for source in pair}),
                    "status": "manual_review_required",
                })
        return contradictions

    @staticmethod
    def _prioritize(risks, failures, contradictions):
        severity_priority = {"critical": "high", "high": "high", "medium": "medium", "low": "low"}
        priorities = []
        for risk in risks:
            severity = risk.get("severity", "medium")
            priority = "critical" if risk.get("status") == "verified_incident" else severity_priority.get(severity, "medium")
            priorities.append({
                "priority": priority,
                "action": f"Validate the evidence and investigate potential {risk['category']} risk indicators.",
            })
        if failures:
            priorities.append({"priority": "high", "action": "Investigate failed or incomplete agent outputs before relying on the assessment."})
        if contradictions:
            priorities.append({"priority": "medium", "action": "Review conflicting agent findings and verify the underlying evidence."})
        if not priorities:
            priorities.append({"priority": "normal", "action": "Continue standard review; no predefined positive risk indicators were detected."})
        return priorities

    @staticmethod
    def _make_decision(risks, failures, contradictions):
        if failures:
            return {"outcome": "REVIEW_REQUIRED", "reason": "One or more agents failed or returned an incomplete response."}
        if any(item.get("status") == "verified_incident" for item in risks):
            return {"outcome": "HIGH_RISK", "reason": "A verified incident was explicitly provided and requires immediate investigation."}
        if contradictions:
            return {"outcome": "REVIEW_REQUIRED", "reason": "Conflicting findings require manual verification."}
        if any(item.get("severity") == "critical" for item in risks):
            return {"outcome": "REVIEW_REQUIRED", "reason": "Potential critical risk indicators require evidence validation."}
        if risks:
            return {"outcome": "CAUTION", "reason": "Potential risk indicators were detected and require evaluation."}
        return {"outcome": "PROCEED_WITH_REVIEW", "reason": "No predefined positive risk indicators were detected; this is not proof of safety."}


decision_engine = NexusDecisionEngine()
