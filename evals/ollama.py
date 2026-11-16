"""Local-model access with a committed, content-addressed cache.

Embeddings and reranker judgements are computed once against a local Ollama and
stored in ``evals/cache/``. Tests and CI read the cache, so they stay offline and
deterministic. A cache miss with Ollama unreachable raises ``CacheMiss`` rather
than silently returning something made up.
"""
from __future__ import annotations

import hashlib
import atexit
import contextlib
import json
import os
import re
import tempfile
import urllib.request
import weakref
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


def fingerprint(*parts) -> str:
    """Stable short hash of everything that determines a computation (prompt, schema, options, model...)."""
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


# Entries cached before keys carried a configuration fingerprint are adopted under the new key only while the
# current configuration still equals the one in force when they were written. Edit a prompt, schema or option
# and the fingerprint changes, adoption stops, and the computation is redone instead of reusing a stale value.
# The fingerprints below were computed from the prompts, schema and options as they stood when those entries
# were written (unchanged since: see git history of this file).
LEGACY_ADOPTABLE = {
    "judge": {"1202d3e4e027184e"},                              # qwen3:1.7b relevance judge
    "structured": {"293938d9180f4474", "5210eaa191c41aa3"},     # qwen3:4b and qwen3:1.7b structured generation
}


try:  # advisory file locking; absent on Windows, where the merge below is still correct for one process
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

INFERENCE_BLOCKED = False  # set by tools and tests that must never call a model


@contextlib.contextmanager
def locked(path: Path):
    """Exclusive advisory lock on ``<path>.lock`` so concurrent writers (threads, processes) serialize."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path.with_name(path.name + ".lock"), "a")
    try:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def write_json_atomic(path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, sort_keys=True, separators=(",", ":"))
        f.write("\n")
    os.replace(tmp, path)


class JsonCache:
    """Content-addressed cache backed by one JSON file, safe for several cache objects on the same file.

    Writes go to ``pending`` and are merged into whatever is on disk at flush time (under a file lock), so a
    flush can add entries but never remove or overwrite entries written by another cache object or process.
    Entries are only ever removed by an explicit ``evals.prune_cache`` run.
    """

    CHECKPOINT = 5
    instances: "weakref.WeakSet[JsonCache]" = weakref.WeakSet()
    usage: dict[str, set[str]] | None = None  # when a dict, records every key read or written (see prune_cache)

    def __init__(self, name: str):
        self.path = CACHE_DIR / name
        self.data: dict = read_json(self.path)
        self.pending: dict = {}
        JsonCache.instances.add(self)
        atexit.register(self.flush)

    @property
    def dirty(self) -> bool:
        return bool(self.pending)

    def _touch(self, key) -> None:
        if JsonCache.usage is not None:
            JsonCache.usage.setdefault(self.path.name, set()).add(key)

    def get(self, key):
        value = self.data.get(key)
        if value is not None:
            self._touch(key)
        return value

    def get_or_adopt(self, key, legacy_key, legacy_allowed: bool):
        """Return the value under ``key``; otherwise adopt a pre-fingerprint entry when it is provably the same config."""
        hit = self.get(key)
        if hit is None and legacy_allowed:
            hit = self.data.get(legacy_key)
            if hit is not None:
                self.put(key, hit)
        return hit

    def put(self, key, value):
        self.data[key] = value
        self.pending[key] = value
        self._touch(key)  # a freshly written entry counts as used, so cleanup can never drop it
        if len(self.pending) >= self.CHECKPOINT:  # an interrupted run keeps its completed work
            self.flush()

    def flush(self):
        if not self.pending:
            return
        with locked(self.path):
            merged = read_json(self.path)  # entries other cache objects or processes wrote since we loaded
            merged.update(self.pending)
            write_json_atomic(self.path, merged)
        self.data = merged
        self.pending = {}

    @classmethod
    def flush_all(cls) -> None:
        for instance in list(cls.instances):
            instance.flush()


def _post(path: str, payload: dict, timeout: float | None = None) -> dict:
    if INFERENCE_BLOCKED:
        raise CacheMiss("model inference is blocked in this process; the value is not cached")
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

    OPTIONS = {"temperature": 0, "seed": 0, "num_predict": 8}

    def __init__(self, model: str = RERANK_MODEL):
        self.model, self.cache = model, JsonCache("rerank.json")
        self.config = fingerprint("judge", model, self.PROMPT, self.OPTIONS)

    def score(self, question: str, passage: str) -> int:
        key = _key("judge", self.config, question, passage)
        hit = self.cache.get_or_adopt(key, _key(self.model, question, passage),
                                      self.config in LEGACY_ADOPTABLE["judge"])
        if hit is not None:
            return hit
        out = _post("/api/chat", {
            "model": self.model, "stream": False, "think": False,
            "options": self.OPTIONS,
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

    OPTIONS = {"temperature": 0, "seed": 0, "num_predict": 200}

    def __init__(self, model: str | None = None, structured: bool = True):
        self.model, self.structured = model or GEN_MODEL, structured
        self.cache = JsonCache("generate.json")
        prompt = self.STRUCTURED_PROMPT if structured else self.FREE_TEXT_PROMPT
        self.config = fingerprint("generate", self.model, structured, prompt, self.OPTIONS,
                                  self.SCHEMA if structured else None)

    def key(self, question: str, passages: list[tuple[str, str]]) -> str:
        """Cache key: configuration fingerprint + the question + the exact retrieved context, in order."""
        block = "\n".join(f"[{i}] {t}" for i, t in passages)
        return _key("generate", self.config, question, block)

    def request(self, question: str, passages: list[tuple[str, str]]) -> str:
        """One model call, never cached. Used by ``generate`` and by the recovery tool."""
        block = "\n".join(f"[{i}] {t}" for i, t in passages)
        prompt = (self.STRUCTURED_PROMPT if self.structured else self.FREE_TEXT_PROMPT)
        payload = {
            "model": self.model, "stream": False, "think": False,
            "options": self.OPTIONS,
            "messages": [{"role": "user", "content": prompt.format(passages=block, q=question)}],
        }
        if self.structured:
            payload["format"] = self.SCHEMA
        return _post("/api/chat", payload)["message"]["content"].strip()

    def generate(self, question: str, passages: list[tuple[str, str]]) -> str:
        block = "\n".join(f"[{i}] {t}" for i, t in passages)
        mode = "structured" if self.structured else "generate"
        key = self.key(question, passages)
        hit = self.cache.get_or_adopt(key, _key(self.model, mode, question, block),
                                      self.structured and self.config in LEGACY_ADOPTABLE["structured"])
        if hit is not None:
            return hit
        out = self.request(question, passages)
        self.cache.put(key, out)
        return out
