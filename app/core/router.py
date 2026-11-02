
class NexusRouter:

    def __init__(self):
        self.agent_keywords = {
            "enterprise": [
                "business", "company", "enterprise", "strategy",
                "project", "workflow", "productivity", "management",
                "revenue", "customer", "operations", "planning"
            ],
            "personal": [
                "personal", "daily", "schedule", "reminder",
                "organize", "learning", "study", "career",
                "resume", "goal", "recommendation", "advice"
            ],
            "cybersecurity": [
                "security", "cybersecurity", "hack", "attack",
                "threat", "malware", "phishing", "vulnerability",
                "firewall", "encryption", "breach", "incident",
                "network", "password", "unauthorized"
            ]
        }

    def route(self, message):
        text = message.lower()
        scores = {}

        for agent, keywords in self.agent_keywords.items():
            scores[agent] = sum(
                1 for keyword in keywords
                if keyword in text
            )

        selected_agent = max(scores, key=scores.get)
        confidence = scores[selected_agent]

        if confidence == 0:
            selected_agent = "enterprise"
            routing_reason = "Default agent: no specific keywords detected."
        else:
            routing_reason = (
                f"Selected based on {confidence} matching keyword(s)."
            )

        return {
            "selected_agent": selected_agent,
            "scores": scores,
            "confidence_score": confidence,
            "routing_reason": routing_reason,
            "routing_mode": "keyword_based"
        }


router = NexusRouter()
