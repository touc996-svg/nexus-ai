import json
import os
from types import SimpleNamespace

import httpx


DEMO_MODE = os.getenv("NEXUS_DEMO_MODE", "false").lower() in {
    "1", "true", "yes", "on"
}
MODEL = os.getenv("NEXUS_MODEL", "qwen3:1.7b")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")
OLLAMA_BASE_URL = os.getenv(
    "OLLAMA_BASE_URL",
    "http://localhost:11434/v1"
).rstrip("/")
OLLAMA_NATIVE_URL = (
    OLLAMA_BASE_URL[:-3]
    if OLLAMA_BASE_URL.endswith("/v1")
    else OLLAMA_BASE_URL
)
OLLAMA_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "120"))


class DemoCompletions:
    "Deterministic responses for the public NEXUS portfolio demo."

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
                    "NEXUS AI public demo mode is active. This demonstration "
                    "verifies the conversation and API workflow; live model "
                    "reasoning is disabled."
                ),
                "clarifying_question": None,
                "understood_request": request
            })
        elif "return only valid json in this exact structure" in system_lower and "career_direction" in system_lower:
            content = "{}"
        elif "cybersecurity" in system_lower:
            content = (
                "[DEMO SIMULATION] Cybersecurity assessment: identify the "
                "affected resource, preserve relevant logs, review "
                "authentication and network activity, and validate evidence "
                "before declaring an incident. This is not a live security scan."
            )
        elif "personal" in system_lower:
            content = (
                "[DEMO SIMULATION] Personal assistant: break the requested "
                "goal into a clear next action, measurable short-term "
                "milestone, and progress review."
            )
        elif "enterprise" in system_lower:
            content = (
                "[DEMO SIMULATION] Enterprise analysis: define the business "
                "objective, identify stakeholders, map the workflow, assess "
                "constraints, and establish measurable success criteria."
            )
        else:
            content = (
                "[DEMO SIMULATION] NEXUS AI received your request. The public "
                "demo validates application behavior without calling an LLM."
            )

        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


def get_ollama_health(timeout: float = 3.0) -> dict:
    "Report Ollama connectivity and configured chat/embedding model availability."
    if DEMO_MODE:
        return {
            "status": "demo",
            "provider": "deterministic-demo",
            "reachable": False,
            "chat_model": MODEL,
            "embedding_model": EMBEDDING_MODEL,
            "reason": "Live Ollama calls are disabled in demo mode."
        }

    try:
        response = httpx.get(f"{OLLAMA_NATIVE_URL}/api/tags", timeout=timeout)
        response.raise_for_status()
        data = response.json()
        available = [
            str(item.get("name", ""))
            for item in data.get("models", [])
            if isinstance(item, dict) and item.get("name")
        ]
        names = set(available)

        def is_available(configured: str) -> bool:
            return any(
                name == configured
                or name.split(":")[0] == configured.split(":")[0]
                for name in names
            )

        chat_available = is_available(MODEL)
        embedding_available = is_available(EMBEDDING_MODEL)
        return {
            "status": "online" if chat_available and embedding_available else "degraded",
            "provider": "ollama",
            "reachable": True,
            "chat_model": MODEL,
            "chat_model_available": chat_available,
            "embedding_model": EMBEDDING_MODEL,
            "embedding_model_available": embedding_available,
            "available_models": available
        }
    except Exception as exc:
        return {
            "status": "offline",
            "provider": "ollama",
            "reachable": False,
            "chat_model": MODEL,
            "embedding_model": EMBEDDING_MODEL,
            "error_type": type(exc).__name__
        }


if DEMO_MODE:
    client = SimpleNamespace(chat=SimpleNamespace(completions=DemoCompletions()))
else:
    from openai import OpenAI

    client = OpenAI(
        base_url=OLLAMA_BASE_URL,
        api_key="ollama",
        timeout=OLLAMA_TIMEOUT_SECONDS,
        max_retries=int(os.getenv("OLLAMA_MAX_RETRIES", "1"))
    )
