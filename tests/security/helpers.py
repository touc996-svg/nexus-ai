"""Shared helpers for the HTTP-level security suite (fixtures live in conftest.py).

Every test goes through the production routes and the production middleware chain (security, then observability),
assembled into a fresh FastAPI app per test with: an isolated in-memory SQL database, an in-memory vector store, a
deterministic bag-of-words embedder and a scripted fake LLM. Nothing touches the network, the real nexus.db, a real
model or any real credential. Keys and secrets below are throwaway test values.
"""

import base64
import hashlib
import hmac
import json
import re
import time
import zlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

OPERATOR_KEY = "operator-key-0123456789-abcdef"
ALICE_KEY = "alice-key-0123456789-abcdefgh"
BOB_KEY = "bob-key-0123456789-abcdefghij"
JWT_SECRET = "jwt-secret-0123456789-abcdefghijklmnopqrstuv"
JWT_ISSUER = "https://tokens.example.test"
JWT_AUDIENCE = "nexus-api"
PROXY_PEER = "10.0.0.5"

ENV_NAMES = (
    "NEXUS_DEMO_MODE", "APP_ENV", "NEXUS_API_KEY", "NEXUS_API_KEYS", "NEXUS_REQUIRE_API_KEY", "NEXUS_KEYSTORE_ENABLED",
    "NEXUS_JWT_SECRETS", "NEXUS_JWT_ISSUER", "NEXUS_JWT_AUDIENCE", "NEXUS_JWT_LEEWAY_SECONDS",
    "NEXUS_JWT_MAX_LIFETIME_SECONDS", "NEXUS_RATE_LIMIT_PER_MINUTE", "NEXUS_RATE_LIMIT_PER_IDENTITY_PER_MINUTE",
    "NEXUS_TRUSTED_PROXY_HOPS", "NEXUS_MAX_REQUEST_BYTES", "NEXUS_CORS_ORIGINS",
)


def auth(key):
    return {"Authorization": f"Bearer {key}"}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_jwt(claims=None, *, header=None, secret=JWT_SECRET, drop=(), signature=None, raw_parts=None):
    now = int(time.time())
    body = {"iss": JWT_ISSUER, "aud": JWT_AUDIENCE, "sub": "alice", "iat": now - 5, "exp": now + 600}
    body.update(claims or {})
    for name in drop:
        body.pop(name, None)
    head = _b64(json.dumps(header or {"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps(body).encode())
    if raw_parts is not None:
        return ".".join(raw_parts)
    if signature is None:
        signature = _b64(hmac.new(secret.encode(), f"{head}.{payload}".encode(), hashlib.sha256).digest())
    return f"{head}.{payload}.{signature}"


def bow_embed(text: str) -> list[float]:
    """Deterministic bag-of-words vector: texts sharing words are similar, so retrieval is meaningful and repeatable."""
    vector = [0.0] * 768
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        vector[zlib.crc32(word.encode()) % 768] += 1.0
    norm = sum(v * v for v in vector) ** 0.5
    if norm == 0:
        vector[0], norm = 1.0, 1.0
    return [v / norm for v in vector]


class FakeLLM:
    """Scripted stand-in for the model client. Records every call so tests can assert what reached the model."""

    def __init__(self):
        self.calls: list[list[dict]] = []
        self.agent = "enterprise"
        self.reply = lambda messages: "FAKE-AGENT-REPLY"
        self.chat = SimpleNamespace(completions=self)

    def create(self, *args, messages=None, **kwargs):
        messages = messages or []
        self.calls.append(messages)
        system = next((m.get("content", "") for m in messages if m.get("role") == "system"), "").lower()
        if "conversation intelligence engine" in system:
            content = json.dumps({"intent_type": "agent_task", "agent": self.agent, "response": "",
                                  "clarifying_question": None, "understood_request": "task"})
        elif "return only valid json in this exact structure" in system and "career_direction" in system:
            content = "{}"
        else:
            content = self.reply(messages)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=None)

    def prompts_text(self) -> str:
        return "\n".join(str(m.get("content", "")) for call in self.calls for m in call)


class _NullActivity:
    def record(self, *args, **kwargs):
        return None

    def get_recent(self, *args, **kwargs):
        return []

    def get_summary(self, *args, **kwargs):
        return {}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


