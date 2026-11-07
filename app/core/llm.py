import json
import os
from types import SimpleNamespace

DEMO_MODE = os.getenv("NEXUS_DEMO_MODE", "false").lower() in {
    "1", "true", "yes", "on"
}

MODEL = os.getenv("NEXUS_MODEL", "qwen3:1.7b")


class DemoCompletions:
    """Deterministic responses for the public NEXUS portfolio demo."""

    def create(self, model=None, messages=None, **kwargs):
        messages = messages or []

        system = next(
            (m.get("content", "") for m in messages if m.get("role") == "system"),
            ""
        )
        user = next(
            (m.get("content", "") for m in reversed(messages)
             if m.get("role") == "user"),
            ""
        )

        system_lower = system.lower()

        if "conversation intelligence engine" in system_lower:
            request = user.split("Current message:")[-1].strip()
            content = json.dumps({
                "intent_type": "conversation",
                "agent": None,
                "response": (
                    "NEXUS AI public demo mode is active. "
                    "This demonstration verifies the conversation and "
                    "API workflow; live model reasoning is disabled."
                ),
                "clarifying_question": None,
                "understood_request": request
            })

        elif "return only valid json in this exact structure" in system_lower and "career_direction" in system_lower:
            content = "{}"
        elif "cybersecurity" in system_lower:
            content = (
                "[DEMO SIMULATION] Cybersecurity assessment: "
                "identify the affected resource, preserve relevant logs, "
                "review authentication and network activity, and validate "
                "evidence before declaring an incident. "
                "This is not a live security scan or verified incident."
            )

        elif "personal" in system_lower:
            content = (
                "[DEMO SIMULATION] Personal assistant: "
                "break the requested goal into a clear next action, "
                "a measurable short-term milestone, and a progress review. "
                "Full personalized model reasoning is available in local mode."
            )

        elif "enterprise" in system_lower:
            content = (
                "[DEMO SIMULATION] Enterprise analysis: "
                "define the business objective, identify stakeholders, "
                "map the current workflow, assess constraints, and establish "
                "measurable success criteria."
            )

        else:
            content = (
                "[DEMO SIMULATION] NEXUS AI received your request. "
                "The public demo validates application behavior without "
                "calling an external language model."
            )

        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=content)
                )
            ]
        )


if DEMO_MODE:
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=DemoCompletions()
        )
    )
else:
    from openai import OpenAI

    client = OpenAI(
        base_url=os.getenv(
            "OLLAMA_BASE_URL",
            "http://localhost:11434/v1"
        ),
        api_key="ollama",
        timeout=120.0,
    )
