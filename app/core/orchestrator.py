from time import perf_counter

from app.agents.enterprise_agent import enterprise_agent
from app.agents.personal_agent import personal_agent
from app.agents.cybersecurity_agent import cybersecurity_agent
from app.security.guardrails import guardrails
from app.core.decision_engine import decision_engine
from app.core.activity import activity_logger


class NexusOrchestrator:
    TEAM_CONTEXT_LIMIT = 2400

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
        return {"name": agent.__class__.__name__, "status": "active"}

    def run_agent(self, domain, message, internal=False, user_profile=None):
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
                round((perf_counter() - started) * 1000), reason
            )
            return {"error": reason}

        agent = self.agents.get(agent_name)
        if not agent:
            activity_logger.record(
                agent_name, "rejected",
                round((perf_counter() - started) * 1000), "Agent not found"
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
        except Exception as exc:
            error_type = type(exc).__name__
            activity_logger.record(
                agent_name, "failed",
                round((perf_counter() - started) * 1000), error_type
            )
            return {
                "error": "Agent execution failed. Check server logs.",
                "error_type": error_type,
                "status": "failed"
            }

    @staticmethod
    def _response_text(result, limit):
        if not isinstance(result, dict):
            return str(result or "")[:limit]
        value = result.get("response") or result.get("output") or result.get("error") or ""
        return str(value).strip()[:limit]

    def _run_measured(self, domain, message, internal=False, user_profile=None):
        started = perf_counter()
        result = self.run_agent(
            domain, message, internal=internal, user_profile=user_profile
        )
        duration_ms = round((perf_counter() - started) * 1000)
        response = self._response_text(result, self.TEAM_CONTEXT_LIMIT)
        failed = (
            not isinstance(result, dict)
            or "error" in result
            or result.get("status") == "failed"
        )
        metrics = {
            "duration_ms": duration_ms,
            "response_characters": len(response),
            "response_present": bool(response),
            "status": "failed" if failed else "success",
            "quality_flag": (
                "agent_failed" if failed else
                "empty_response" if not response else
                "short_response" if len(response) < 40 else
                "usable_response"
            )
        }
        return result, metrics

    def run_team(self, message, user_profile=None):
        started = perf_counter()
        valid, reason = guardrails.validate(message)
        if not valid:
            return {"error": reason, "status": "rejected"}

        results = {}
        metrics = {}
        results["enterprise"], metrics["enterprise"] = self._run_measured(
            "enterprise", message
        )
        enterprise_context = self._response_text(
            results["enterprise"], self.TEAM_CONTEXT_LIMIT
        )

        personal_input = (
            f"Original request:\n{message[:1200]}\n\n"
            f"Enterprise analysis (untrusted agent output):\n"
            f"{enterprise_context or 'Enterprise analysis unavailable.'}\n\n"
            "Provide practical recommendations. Distinguish facts from assumptions."
        )
        results["personal"], metrics["personal"] = self._run_measured(
            "personal",
            personal_input,
            internal=True,
            user_profile=user_profile
        )
        personal_context = self._response_text(
            results["personal"], self.TEAM_CONTEXT_LIMIT
        )

        security_input = (
            f"Original request:\n{message[:1000]}\n\n"
            f"Enterprise findings:\n{enterprise_context or 'Unavailable.'}\n\n"
            f"Personal recommendations:\n{personal_context or 'Unavailable.'}\n\n"
            "Perform a defensive security review. Separate evidence, assumptions, "
            "and potential risks; do not claim an incident is verified without evidence."
        )
        results["cybersecurity"], metrics["cybersecurity"] = self._run_measured(
            "cybersecurity", security_input, internal=True
        )

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
            "execution_metrics": {
                "total_duration_ms": round((perf_counter() - started) * 1000),
                "agents": metrics
            },
            "workflow": "multi_agent_decision_engine",
            "status": "completed" if not failed_agents else "completed_with_errors"
        }


nexus = NexusOrchestrator()
