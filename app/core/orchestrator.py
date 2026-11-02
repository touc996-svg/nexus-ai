
from app.agents.enterprise_agent import enterprise_agent
from app.agents.personal_agent import personal_agent
from app.agents.cybersecurity_agent import cybersecurity_agent
from app.security.guardrails import guardrails
from app.core.decision_engine import decision_engine


class NexusOrchestrator:

    def __init__(self):
        self.agents = {
            "enterprise": enterprise_agent,
            "personal": personal_agent,
            "cybersecurity": cybersecurity_agent
        }

    def get_agent(self, domain):
        agent = self.agents.get(domain.lower())

        if not agent:
            return {
                "error": "Agent not found",
                "available_agents": list(self.agents.keys())
            }

        return {
            "name": agent.__class__.__name__,
            "status": "active"
        }

    def run_agent(self, domain, message):
        valid, reason = guardrails.validate(message)

        if not valid:
            return {"error": reason}

        agent = self.agents.get(domain.lower())

        if not agent:
            return {
                "error": "Agent not found",
                "available_agents": list(self.agents.keys())
            }

        try:
            return agent.run(message)
        except Exception as e:
            return {
                "error": str(e),
                "status": "failed"
            }

    def run_team(self, message):
        valid, reason = guardrails.validate(message)

        if not valid:
            return {"error": reason}

        results = {}

        # Step 1: Enterprise analysis
        results["enterprise"] = self.run_agent(
            "enterprise", message
        )

        # Step 2: Personal recommendations
        personal_input = (
            f"Original request: {message}\n"
            f"Enterprise analysis: {results['enterprise']}\n"
            "Review the enterprise analysis and add practical "
            "recommendations."
        )

        results["personal"] = self.run_agent(
            "personal", personal_input
        )

        # Step 3: Cybersecurity assessment
        security_input = (
            f"Original request: {message}\n"
            f"Enterprise analysis: {results['enterprise']}\n"
            f"Personal recommendations: {results['personal']}\n"
            "Identify security risks and defensive recommendations."
        )

        results["cybersecurity"] = self.run_agent(
            "cybersecurity", security_input
        )

        # Step 4: Generate unified decision report
        final_report = decision_engine.generate_report(results)

        failed_agents = [
            name for name, result in results.items()
            if not isinstance(result, dict)
            or "error" in result
            or result.get("status") == "failed"
        ]

        return {
            "message": message,
            "agents": results,
            "final_report": final_report,
            "failed_agents": failed_agents,
            "workflow": "multi_agent_decision_engine",
            "status": (
                "completed"
                if not failed_agents
                else "completed_with_errors"
            )
        }


nexus = NexusOrchestrator()
