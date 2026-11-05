
from app.agents.enterprise_agent import enterprise_agent
from app.agents.personal_agent import personal_agent
from app.agents.cybersecurity_agent import cybersecurity_agent
from app.security.guardrails import guardrails
from app.core.decision_engine import decision_engine
from app.core.activity import activity_logger


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

    def run_agent(self, domain, message, internal=False, user_profile=None):
        from time import perf_counter

        started = perf_counter()
        agent_name = domain.lower()

        limit = (
            guardrails.MAX_INTERNAL_CONTEXT_LENGTH
            if internal else guardrails.MAX_MESSAGE_LENGTH
        )
        valid, reason = guardrails.validate(message, max_length=limit)

        if not valid:
            activity_logger.record(
                agent_name, "rejected",
                round((perf_counter() - started) * 1000),
                reason
            )
            return {"error": reason}

        agent = self.agents.get(agent_name)

        if not agent:
            activity_logger.record(
                agent_name, "rejected",
                round((perf_counter() - started) * 1000),
                "Agent not found"
            )
            return {
                "error": "Agent not found",
                "available_agents": list(self.agents.keys())
            }

        try:
            if agent_name == "personal":
                result = agent.run(message, user_profile=user_profile)
            else:
                result = agent.run(message)

            failed = (
                isinstance(result, dict)
                and ("error" in result or result.get("status") == "failed")
            )

            activity_logger.record(
                agent_name,
                "failed" if failed else "success",
                round((perf_counter() - started) * 1000),
                str(result.get("error")) if failed else None
            )

            return result

        except Exception as e:
            activity_logger.record(
                agent_name, "failed",
                round((perf_counter() - started) * 1000),
                str(e)
            )
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
            "personal", personal_input, internal=True
        )

        # Step 3: Cybersecurity assessment

        enterprise_result = results["enterprise"]
        personal_result = results["personal"]

        enterprise_context = (
            enterprise_result.get("response", "")
            if isinstance(enterprise_result, dict)
            else str(enterprise_result)
        )

        personal_context = (
            personal_result.get("response", "")
            if isinstance(personal_result, dict)
            else str(personal_result)
        )

        security_input = (
            f"Original request: {message}\\n\\n"
            f"Enterprise findings:\\n{enterprise_context[:1500]}\\n\\n"
            f"Personal recommendations:\\n{personal_context[:1500]}\\n\\n"
            "Identify security risks, assess potential threats, "
            "and recommend defensive actions. "
            "Distinguish potential risks from verified incidents."
        )

        results["cybersecurity"] = self.run_agent(
            "cybersecurity", security_input, internal=True
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
