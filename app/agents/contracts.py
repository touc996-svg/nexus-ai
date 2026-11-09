"""Shared input validation and response contracts for NEXUS agents."""

from __future__ import annotations

import json
from typing import Any

MAX_AGENT_MESSAGE_CHARS = 15_000
MAX_PROFILE_CONTEXT_CHARS = 10_000


def validate_agent_message(message: Any) -> tuple[bool, str]:
    if not isinstance(message, str):
        return False, "Message must be text."
    if not message.strip():
        return False, "Message cannot be empty."
    if len(message) > MAX_AGENT_MESSAGE_CHARS:
        return False, f"Message exceeds the {MAX_AGENT_MESSAGE_CHARS}-character limit."
    return True, "ok"


def safe_profile_context(profile: Any) -> str:
    """Serialize only bounded JSON profile context; never treat it as instructions."""
    if not isinstance(profile, dict):
        return "No saved user profile available."

    allowed = {
        "goals", "skills", "preferences",
        "professional_background", "career_direction",
    }
    safe = {key: value for key, value in profile.items() if key in allowed}
    try:
        encoded = json.dumps(
            safe, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError):
        return "Saved user profile unavailable because its data is invalid."

    if len(encoded) > MAX_PROFILE_CONTEXT_CHARS:
        encoded = encoded[:MAX_PROFILE_CONTEXT_CHARS]
        return encoded + " [profile context truncated]"
    return encoded


def success_result(agent: str, domain: str, message: str, response: Any) -> dict:
    text = response.strip() if isinstance(response, str) else ""
    if not text:
        return error_result(agent, domain, "The model returned an empty response.", "EmptyResponse")
    return {
        "agent": agent,
        "domain": domain,
        "message": message,
        "response": text,
        "status": "success",
    }


def error_result(agent: str, domain: str, message: str, error_type: str) -> dict:
    return {
        "agent": agent,
        "domain": domain,
        "error": message,
        "error_type": error_type,
        "status": "failed",
    }
