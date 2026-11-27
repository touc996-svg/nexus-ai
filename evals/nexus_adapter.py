"""Adapter that lets the evaluation harness drive the real production ``NexusRAG``.

What is real: ``NexusRAG`` ingestion (validation, chunking, hashing, metadata), its Qdrant
client calls (collection creation, upsert, filtered cosine search, score threshold) and its
embedding code path (``NexusRAG.embed``: Ollama request, dimension and finite checks).

What is replayed: the HTTP response of ``NexusRAG.embed``. On a cache miss the real ``embed``
runs against a live Ollama and the vector is stored in ``evals/cache/nexus_embeddings.json``;
afterwards the stored vector is returned, so CI needs no model. The Qdrant client is
qdrant-client's in-process local mode unless a server URL is passed explicitly.

Safety: every adapter instance uses a unique collection named ``nexus_eval_<id>`` and a unique
user id, creates its own Qdrant client (it never reads ``QDRANT_URL``), refuses the production
collection name, and removes its collection on ``close()``.
"""
from __future__ import annotations

import uuid
from time import perf_counter

import hashlib

from . import ollama
from .ollama import CacheMiss, JsonCache, _key, fingerprint

# Entries written during the first (aborted) run used a key without the configuration; they were computed by the
# unchanged production embed, so they are adopted once, for the exact configuration pinned here.
LEGACY_EMBED_CONFIGS: set[str] = {"dee11704d19d13ce"}

EVAL_COLLECTION_PREFIX = "nexus_eval_"
PRODUCTION_COLLECTION = "nexus_knowledge"


# The members of ``NexusRAG`` that determine a vector or a retrieval result. Their source text is what the replay cache is
# keyed on. Client construction, health checks, file extraction and other plumbing are deliberately not in this list, so
# changing how Qdrant is reached cannot make a recorded vector stale, while any change to embedding, chunking, ingestion,
# search, the tenant filter or collection/distance set-up still invalidates every replay (and the gate then fails closed).
HASHED_MEMBERS = ("embed", "chunk_text", "ingest_text", "search", "_source_id", "_user_filter", "_ensure_collection")


def members_sha(rag_class) -> str:
    import inspect

    parts = [f"{name}\n{inspect.getsource(getattr(rag_class, name))}" for name in HASHED_MEMBERS]
    parts.append(f"COLLECTION={rag_class.COLLECTION};DEFAULT_VECTOR_SIZE={rag_class.DEFAULT_VECTOR_SIZE}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def production_rag_sha() -> str:
    """Hash of the production embedding/retrieval code (see HASHED_MEMBERS)."""
    from app.services.rag import NexusRAG

    return members_sha(NexusRAG)


# Before this narrowing the key was the sha256 of the whole ``rag.py``. Vectors recorded under that key were produced by
# members whose combined hash is PRE_NARROWING_MEMBERS_SHA (computed from that exact file). They are adopted, once and
# only while the live members hash to the same value, so a change to any hashed member still forces a re-record.
PRE_NARROWING_RAG_SHA = "b8970ae55fb7990e"
PRE_NARROWING_MEMBERS_SHA = "45744ff550b0eb8d"


class ReplayedEmbedding:
    """Wraps the real ``NexusRAG.embed``; replays stored vectors, records the live latency.

    The cache key covers everything that determines a vector: the embedding model, the configured dimension,
    the text, the embed contract (raw text, no task prefix) and the hash of the production ``rag.py``.
    Retrieval *results* are never cached: Qdrant is searched live in a fresh isolated collection every
    run, so collection, user, score threshold and limit cannot go stale.
    """

    CONTRACT = "NexusRAG.embed: raw text, no task prefix"

    def __init__(self, real_embed, model: str, dimension: int = 768):
        self.real, self.model = real_embed, model
        live_sha = production_rag_sha()
        self.config = fingerprint("nexus-embed", model, dimension, self.CONTRACT, live_sha)
        # Vectors recorded under the whole-file hash are adopted only while the hashed members are provably unchanged.
        self.previous_config = (
            fingerprint("nexus-embed", model, dimension, self.CONTRACT, PRE_NARROWING_RAG_SHA)
            if live_sha == PRE_NARROWING_MEMBERS_SHA else None
        )
        self.cache = JsonCache("nexus_embeddings.json")
        self.recorded_ms = 0.0  # latency recorded when the vector was first computed live
        self.wall_ms = 0.0      # time actually spent in this call now

    def __call__(self, text: str) -> list[float]:
        started = perf_counter()
        key = _key("nexus-embed", self.config, text)
        hit = self.cache.get_or_adopt(key, _key(self.model, "nexus-embed", text), self.config in LEGACY_EMBED_CONFIGS)
        if hit is None and self.previous_config is not None:
            hit = self.cache.data.get(_key("nexus-embed", self.previous_config, text))
            if hit is not None:
                self.cache.put(key, hit)
        if hit is None:
            if ollama.INFERENCE_BLOCKED:
                raise CacheMiss("embedding not cached and model inference is blocked in this process")
            try:
                vector = self.real(text)
            except RuntimeError as exc:  # NexusRAG.embed wraps every transport failure in RuntimeError
                raise CacheMiss(f"embedding not cached and the real embed failed: {exc}") from exc
            hit = {"vector": [round(x, 6) for x in vector],
                   "ms": round((perf_counter() - started) * 1000, 2)}
            self.cache.put(key, hit)
        self.recorded_ms = hit["ms"]
        self.wall_ms = (perf_counter() - started) * 1000
        return hit["vector"]


class NexusRagRetriever:
    """Production dense retrieval exposed through the harness ``search(query, k)`` interface."""

    name = "nexus_dense"

    def __init__(self, corpus: dict[str, str], qdrant_url: str | None = None):
        from qdrant_client import QdrantClient

        from app.services.rag import NexusRAG

        self.client = QdrantClient(url=qdrant_url, timeout=30) if qdrant_url else QdrantClient(location=":memory:")
        self.collection = f"{EVAL_COLLECTION_PREFIX}{uuid.uuid4().hex[:12]}"
        self.user_id = f"eval-{uuid.uuid4().hex[:8]}"
        if self.collection == PRODUCTION_COLLECTION or not self.collection.startswith(EVAL_COLLECTION_PREFIX):
            raise RuntimeError("evaluation must never use the production collection")

        self.rag = NexusRAG(client=self.client, demo_mode=False)  # production defaults, no tuning
        self.rag.COLLECTION = self.collection  # every internal call goes through self.COLLECTION
        self.embedder = ReplayedEmbedding(self.rag.embed, self.rag.embedding_model, self.rag.vector_size)
        self.rag.embed = self.embedder

        for doc_id, text in corpus.items():
            report = self.rag.ingest_text(text, source=doc_id, user_id=self.user_id, metadata={"evaluation": True})
            if report.get("status") != "indexed":
                raise RuntimeError(f"ingestion of {doc_id} did not complete: {report.get('status')}")
        self.last_timing: dict[str, float] = {}

    def search(self, query: str, k: int) -> list[tuple[str, float]]:
        started = perf_counter()
        hits = self.rag.search(query, user_id=self.user_id, limit=max(k, 10))
        total_ms = (perf_counter() - started) * 1000
        best: dict[str, float] = {}
        for hit in hits:  # several chunks of one document collapse to its best score
            if hit["source"] not in best or hit["score"] > best[hit["source"]]:
                best[hit["source"]] = hit["score"]
        self.last_timing = {
            "embed_ms": self.embedder.recorded_ms,
            "qdrant_ms": max(0.0, total_ms - self.embedder.wall_ms),
        }
        ranked = sorted(best.items(), key=lambda x: (-x[1], x[0]))
        return ranked[:k]

    def close(self) -> None:
        """Remove the evaluation collection (matters for a real server; harmless in-process)."""
        self.embedder.cache.flush()
        if self.collection.startswith(EVAL_COLLECTION_PREFIX):
            try:
                self.client.delete_collection(self.collection)
            except Exception:  # best-effort cleanup must not mask the evaluation result
                pass
