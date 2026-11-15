"""Local-model access with a committed, content-addressed cache.

Embeddings and reranker judgements are computed once against a local Ollama and
stored in ``evals/cache/``. Tests and CI read the cache, so they stay offline and
deterministic. A cache miss with Ollama unreachable raises ``CacheMiss`` rather
than silently returning something made up.
"""
from __future__ import annotations

import hashlib
import atexit
import json
import os
import re
import tempfile
import urllib.request
from pathlib import Path

CACHE_DIR = Path(__file__).parent / "cache"
BASE_URL = os.getenv("EVALS_OLLAMA_URL", "http://localhost:11434")
EMBED_MODEL = os.getenv("EVALS_EMBED_MODEL", "nomic-embed-text")
RERANK_MODEL = os.getenv("EVALS_RERANK_MODEL", "qwen3:1.7b")
GEN_MODEL = os.getenv("EVALS_GEN_MODEL", "qwen3:4b")


class CacheMiss(RuntimeError):
    pass


def _key(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


class JsonCache:
    def __init__(self, name: str):
        self.path = CACHE_DIR / name
        self.data: dict = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.dirty = False
        atexit.register(self.flush)

    def get(self, key):
        return self.data.get(key)

    def put(self, key, value):
        self.data[key] = value
        self.dirty = True

    def flush(self):
        if not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f, sort_keys=True, separators=(",", ":"))
            f.write("\n")
        os.replace(tmp, self.path)
        self.dirty = False


def _post(path: str, payload: dict, timeout: float | None = None) -> dict:
    timeout = timeout or float(os.getenv("EVALS_TIMEOUT", "400"))
    req = urllib.request.Request(BASE_URL + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # local Ollama only
            return json.loads(resp.read())
    except OSError as exc:
        raise CacheMiss(f"value not cached and Ollama unreachable at {BASE_URL}: {exc}") from exc


class Embedder:
    """nomic-embed-text embeddings; documents and queries use the model's task prefixes."""

    def __init__(self, model: str = EMBED_MODEL):
        self.model, self.cache = model, JsonCache("embeddings.json")

    def embed(self, text: str, kind: str) -> list[float]:
        prefix = {"query": "search_query: ", "document": "search_document: "}[kind]
        key = _key(self.model, prefix + text)
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        vec = _post("/api/embed", {"model": self.model, "input": prefix + text})["embeddings"][0]
        vec = [round(x, 6) for x in vec]
        self.cache.put(key, vec)
        return vec


_SCORE = re.compile(r"\b(10|[0-9])\b")


class LLMJudge:
    """Pointwise relevance scoring with a small local model (temperature 0, fixed seed)."""

    PROMPT = ("You grade search results. Question: {q}\n\nPassage: {p}\n\n"
              "How well does the passage contain the information needed to answer the question? "
              "Reply with a single integer from 0 (irrelevant) to 10 (fully answers it). Reply with the number only.")

    def __init__(self, model: str = RERANK_MODEL):
        self.model, self.cache = model, JsonCache("rerank.json")

    def score(self, question: str, passage: str) -> int:
        key = _key(self.model, question, passage)
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        out = _post("/api/chat", {
            "model": self.model, "stream": False, "think": False,
            "options": {"temperature": 0, "seed": 0, "num_predict": 8},
            "messages": [{"role": "user", "content": self.PROMPT.format(q=question, p=passage)}],
        })["message"]["content"]
        m = _SCORE.search(out)
        value = int(m.group(1)) if m else 0
        self.cache.put(key, value)
        return value


class LLMGenerator:
    """Grounded answer generation. Structured mode constrains output to a JSON schema.

    Structured output separates the answer, its citations and an explicit refusal flag into
    fields, so the result is parsed rather than scraped out of free text, and the model
    cannot leak reasoning into the answer.
    """

    REFUSAL = "INSUFFICIENT_INFORMATION"
    SCHEMA = {
        "type": "object",
        "properties": {
            "refusal": {"type": "boolean"},
            "answer": {"type": "string"},
            "citations": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["refusal", "answer", "citations"],
    }
    STRUCTURED_PROMPT = (
        "Answer the question using ONLY the passages below. Respond as JSON with three fields: "
        "refusal (true if the passages do not contain the answer), answer (one or two concise sentences; "
        "empty if refusal is true), citations (ids of the passages the answer is based on, for example "
        "[\"d03\"]; empty if refusal is true).\n\n{passages}\n\nQuestion: {q}"
    )
    FREE_TEXT_PROMPT = (
        "Answer the question using ONLY the passages below. Be concise (1-2 sentences). "
        "After each claim, cite the passage id in square brackets, like [d03]. "
        "If the passages do not contain the answer, reply with exactly " + REFUSAL + " and nothing else.\n\n"
        "{passages}\n\nQuestion: {q}\nAnswer:"
    )

    def __init__(self, model: str | None = None, structured: bool = True):
        self.model, self.structured = model or GEN_MODEL, structured
        self.cache = JsonCache("generate.json")

    def generate(self, question: str, passages: list[tuple[str, str]]) -> str:
        block = "\n".join(f"[{i}] {t}" for i, t in passages)
        mode = "structured" if self.structured else "generate"
        key = _key(self.model, mode, question, block)
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        prompt = (self.STRUCTURED_PROMPT if self.structured else self.FREE_TEXT_PROMPT)
        payload = {
            "model": self.model, "stream": False, "think": False,
            "options": {"temperature": 0, "seed": 0, "num_predict": 200},
            "messages": [{"role": "user", "content": prompt.format(passages=block, q=question)}],
        }
        if self.structured:
            payload["format"] = self.SCHEMA
        out = _post("/api/chat", payload)["message"]["content"].strip()
        self.cache.put(key, out)
        return out
