"""A fake model server for load tests: Ollama's ``/api/tags`` and ``/api/embed`` plus OpenAI-style chat completions, with
latency, jitter, failures and hangs that the load runner changes at run time through ``POST /_control``.

No model runs and nothing leaves the machine; replies are canned. Run: ``uvicorn loadtest.fake_llm:app``.
Control (JSON): ``latency_ms``, ``jitter_ms``, ``failure_rate`` (0-1), ``failure_status`` (default 500),
``hang_rate`` (0-1) and ``hang_seconds`` (the request sleeps that long, to provoke client timeouts), ``embed_latency_ms``.
"""

import asyncio
import hashlib
import json
import random
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

MODEL = "qwen3:1.7b"
EMBEDDING_MODEL = "nomic-embed-text"
VECTOR_SIZE = 768
DEFAULTS = {"latency_ms": 200.0, "jitter_ms": 50.0, "failure_rate": 0.0, "failure_status": 500,
            "hang_rate": 0.0, "hang_seconds": 0.0, "embed_latency_ms": 10.0}

app = FastAPI(title="fake-llm", docs_url=None, redoc_url=None)
state = dict(DEFAULTS)
stats = {"chat": 0, "embed": 0, "failed": 0, "hung": 0, "in_flight": 0, "max_in_flight": 0, "started": time.time()}


def embedding(text: str) -> list[float]:
    """Deterministic bag-of-words vector: similar texts land close together, so search returns plausible hits."""
    vector = [0.0] * VECTOR_SIZE
    for word in text.lower().split():
        digest = hashlib.blake2b(word.encode("utf-8"), digest_size=4).digest()
        vector[int.from_bytes(digest, "big") % VECTOR_SIZE] += 1.0
    norm = sum(v * v for v in vector) ** 0.5 or 1.0
    return [v / norm for v in vector]


async def _delay(base_ms: float, jitter_ms: float) -> None:
    await asyncio.sleep(max(0.0, base_ms + random.uniform(-jitter_ms, jitter_ms)) / 1000.0)


class _Track:
    async def __aenter__(self):
        stats["in_flight"] += 1
        stats["max_in_flight"] = max(stats["max_in_flight"], stats["in_flight"])

    async def __aexit__(self, *exc):
        stats["in_flight"] -= 1


@app.post("/_control")
async def control(request: Request):
    body = await request.json()
    if body.get("reset"):
        state.clear()
        state.update(DEFAULTS)
    for key in DEFAULTS:
        if key in body:
            state[key] = float(body[key]) if key != "failure_status" else int(body[key])
    return {"state": state}


@app.get("/_stats")
def get_stats():
    return {**stats, "state": state, "uptime_seconds": round(time.time() - stats["started"], 1)}


@app.get("/api/tags")
def tags():
    return {"models": [{"name": MODEL}, {"name": f"{EMBEDDING_MODEL}:latest"}]}


@app.post("/api/embed")
async def embed(request: Request):
    body = await request.json()
    inputs = body.get("input")
    texts = inputs if isinstance(inputs, list) else [inputs]
    async with _Track():
        stats["embed"] += 1
        await _delay(state["embed_latency_ms"], state["embed_latency_ms"] / 4)
    return {"model": EMBEDDING_MODEL, "embeddings": [embedding(str(t)) for t in texts]}


def _system_text(body: dict) -> str:
    return " ".join(str(m.get("content", "")) for m in body.get("messages", []) if m.get("role") == "system")


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    async with _Track():
        stats["chat"] += 1
        if random.random() < state["hang_rate"]:
            stats["hung"] += 1
            await asyncio.sleep(state["hang_seconds"])
        await _delay(state["latency_ms"], state["jitter_ms"])
        if random.random() < state["failure_rate"]:
            stats["failed"] += 1
            return JSONResponse(status_code=int(state["failure_status"]),
                                content={"error": {"message": "injected failure", "type": "server_error"}})
    system = _system_text(body)
    if "intent_type" in system:
        content = json.dumps({"intent_type": "conversation", "agent": None, "response": "Here is a short canned answer.",
                              "clarifying_question": None, "understood_request": "canned request"})
    elif '"updates"' in system:
        content = json.dumps({"updates": {}})
    else:
        content = "This is a canned load-test answer. " * 20
    return {
        "id": "chatcmpl-fake", "object": "chat.completion", "created": int(time.time()), "model": body.get("model", MODEL),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 160, "total_tokens": 280},
    }
