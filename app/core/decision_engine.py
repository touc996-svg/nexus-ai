
class NexusDecisionEngine:

    RISK_RULES = {
        "security": {
            "severity": "critical",
            "keywords": [
                "data breach",
                "databreach",
                "unauthorized access",
                "credential theft",
                "ransomware",
                "malware",
                "privilege escalation",
                "vulnerability",
                "cyberattack"
            ]
        },
        "compliance": {
            "severity": "high",
            "keywords": [
                "regulatory violation",
                "non-compliance",
                "privacy violation",
                "compliance failure",
                "gdpr violation"
            ]
        },
        "operational": {
            "severity": "high",
            "keywords": [
                "system failure",
                "service disruption",
                "downtime",
                "data loss",
                "production failure"
            ]
        },
        "financial": {
            "severity": "medium",
            "keywords": [
                "financial loss",
                "budget overrun",
                "revenue loss",
                "unexpected cost",
                "cash flow problem"
            ]
        }
    }

    def generate_report(self, agent_results):

        enterprise = self._extract(
            agent_results.get("enterprise", {})
        )
        personal = self._extract(
            agent_results.get("personal", {})
        )
        cybersecurity = self._extract(
            agent_results.get("cybersecurity", {})
        )

        failures = self._identify_failures(agent_results)

        combined_text = " ".join([
            enterprise,
            personal,
            cybersecurity
        ]).lower()

        risks = self._identify_risks(combined_text)

        contradictions = self._detect_contradictions(
            enterprise,
            personal,
            cybersecurity
        )

        priorities = self._prioritize(
            risks, failures, contradictions
        )

        decision = self._make_decision(
            risks, failures, contradictions
        )

        return {
            "executive_summary": (
                "NEXUS AI completed a multi-agent assessment "
                "and generated a structured decision report."
            ),
            "business_analysis": enterprise,
            "recommendations": personal,
            "security_assessment": cybersecurity,
            "risks": risks,
            "risk_assessment": {
                "total_risks": len(risks),
                "risks": risks
            },
            "contradictions": contradictions,
            "priorities": priorities,
            "failed_agents": failures,
            "decision": decision,
            "agents_analyzed": list(agent_results.keys()),
            "engine": "enhanced_rule_based",
            "status": (
                "completed_with_warnings"
                if failures or contradictions or risks
                else "completed"
            )
        }

    def _extract(self, result):

        if not isinstance(result, dict):
            return str(result)

        return str(
            result.get("response")
            or result.get("output")
            or result.get("error")
            or "No result available."
        )

    def _identify_failures(self, results):

        failures = []

        for name, result in results.items():

            if (
                not isinstance(result, dict)
                or "error" in result
                or result.get("status") == "failed"
            ):
                failures.append(name)

        return failures

    def _identify_risks(self, text):

        risks = []

        for category, rule in self.RISK_RULES.items():

            matches = [
                keyword
                for keyword in rule["keywords"]
                if keyword in text
            ]

            if matches:
                risks.append({
                    "category": category,
                    "severity": rule["severity"],
                    "indicators": matches,
                    "status": "potential"
                })

        return risks

    def _detect_contradictions(
        self, enterprise, personal, cybersecurity
    ):

        contradictions = []

        sections = {
            "enterprise": enterprise.lower(),
            "personal": personal.lower(),
            "cybersecurity": cybersecurity.lower()
        }

        opposing_pairs = [
            ("no risk", "high risk"),
            ("secure", "vulnerable"),
            ("recommended", "not recommended"),
            ("low cost", "high cost"),
            ("no threat", "active threat")
        ]

        for first, second in opposing_pairs:

            found_first = [
                name for name, content in sections.items()
                if first in content
            ]

            found_second = [
                name for name, content in sections.items()
                if second in content
            ]

            if found_first and found_second:
                contradictions.append({
                    "conflicting_signals": [first, second],
                    "sources": list(set(
                        found_first + found_second
                    )),
                    "status": "manual_review_required"
                })

        return contradictions

    def _prioritize(self, risks, failures, contradictions):

        status_priority = {
            "potential": "medium",
            "requires_review": "high",
            "verified_incident": "critical"
        }

        priorities = []

        for risk in risks:
            status = risk.get("status", "potential")
            priority = status_priority.get(
                status,
                risk.get("severity", "medium")
            )

            if status == "verified_incident":
                action = (
                    f"Respond immediately to the verified "
                    f"{risk['category']} incident."
                )
            elif status == "requires_review":
                action = (
                    f"Investigate {risk['category']} risk indicators."
                )
            else:
                action = (
                    f"Monitor and evaluate potential "
                    f"{risk['category']} risks."
                )

            priorities.append({
                "priority": priority,
                "action": action
            })

        if failures:
            priorities.append({
                "priority": "high",
                "action": "Investigate failed agents."
            })

        if contradictions:
            priorities.append({
                "priority": "medium",
                "action": "Review conflicting agent findings."
            })

        if not priorities:
            priorities.append({
                "priority": "normal",
                "action": "Continue standard review."
            })

        return priorities

    def _make_decision(self, risks, failures, contradictions):

        if failures:
            return {
                "outcome": "REVIEW_REQUIRED",
                "reason": "One or more agents failed."
            }

        if any(
            risk["severity"] == "critical"
            and risk["status"] == "verified_incident"
            for risk in risks
        ):
            return {
                "outcome": "HIGH_RISK",
                "reason": "A verified critical incident requires immediate investigation."
            }

        if contradictions:
            return {
                "outcome": "REVIEW_REQUIRED",
                "reason": "Conflicting findings require manual verification."
            }

        if risks:
            return {
                "outcome": "CAUTION",
                "reason": "Potential risk indicators were detected and require evaluation."
            }

        return {
            "outcome": "PROCEED_WITH_REVIEW",
            "reason": "No predefined risk indicators were detected."
        }


decision_engine = NexusDecisionEngine()
