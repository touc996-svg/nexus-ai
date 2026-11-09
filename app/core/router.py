"""Deterministic first-pass routing with transparent scores and ambiguity hints."""

from __future__ import annotations

import re


class NexusRouter:
    ROUTING_ORDER = ("cybersecurity", "personal", "enterprise")

    def __init__(self):
        self.agent_keywords = {
            "enterprise": [
                "business", "company", "enterprise", "strategy",
                "project", "workflow", "productivity", "management",
                "revenue", "customer", "operations", "planning",
                "process improvement", "stakeholder", "business plan",
            ],
            "personal": [
                "personal", "daily", "schedule", "reminder",
                "organize", "learning", "study", "career",
                "resume", "goal", "recommendation", "advice",
                "certification", "job search", "skill development",
            ],
            "cybersecurity": [
                "security", "cybersecurity", "hack", "attack",
                "threat", "malware", "phishing", "vulnerability",
                "firewall", "encryption", "breach", "incident",
                "network", "password", "unauthorized", "ransomware",
                "incident response", "security audit",
            ],
        }

    @staticmethod
    def _term_pattern(keyword: str):
        return re.compile(rf"(?<!\w){re.escape(keyword)}(?!\w)", re.IGNORECASE)

    def route(self, message):
        if not isinstance(message, str) or not message.strip():
            return {
                "selected_agent": "enterprise",
                "scores": {name: 0 for name in self.agent_keywords},
                "confidence_score": 0.0,
                "confidence_level": "none",
                "requires_clarification": True,
                "routing_reason": "An empty or invalid message cannot be routed reliably.",
                "routing_mode": "keyword_based",
            }

        scores = {
            agent: sum(len(self._term_pattern(term).findall(message)) for term in terms)
            for agent, terms in self.agent_keywords.items()
        }
        highest = max(scores.values(), default=0)
        if highest == 0:
            selected = "enterprise"
            tied = []
            confidence = 0.0
            reason = "Default agent selected; no domain-specific keywords matched."
        else:
            tied = [name for name, score in scores.items() if score == highest]
            selected = next(name for name in self.ROUTING_ORDER if name in tied)
            total = sum(scores.values()) or 1
            confidence = round(highest / total, 3)
            reason = (
                "Multiple domains matched; deterministic priority selected the agent."
                if len(tied) > 1 else
                f"Selected from {highest} domain keyword match(es)."
            )

        level = (
            "high" if highest >= 3 and len(tied) <= 1 and confidence >= 0.75 else
            "medium" if highest > 0 and len(tied) <= 1 else
            "low" if highest > 0 else "none"
        )
        return {
            "selected_agent": selected,
            "scores": scores,
            "confidence_score": confidence,
            "confidence_level": level,
            "requires_clarification": len(tied) > 1 or highest == 0,
            "matched_domains": tied,
            "routing_reason": reason,
            "routing_mode": "keyword_based",
        }


router = NexusRouter()
