"""Dependency-free retrievers used as measurable baselines.

Each exposes ``search(query, k) -> list[(doc_id, score)]``. A production
retriever (for example NexusRAG over Qdrant) can be adapted to the same
interface and measured with the identical harness.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Protocol

from .metrics import content_tokens


class Retriever(Protocol):
    def search(self, query: str, k: int) -> list[tuple[str, float]]: ...


def _stem(token: str) -> str:
    for suffix in ("ies", "es", "s"):
        if token.endswith(suffix) and len(token) > len(suffix) + 2:
            return token[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return token


def analyze(text: str) -> list[str]:
    return [_stem(t) for t in content_tokens(text)]


class BM25:
    name = "bm25"

    def __init__(self, corpus: dict[str, str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = {i: Counter(analyze(t)) for i, t in corpus.items()}
        self.lengths = {i: sum(c.values()) for i, c in self.docs.items()}
        self.avg = sum(self.lengths.values()) / max(len(self.docs), 1)
        df: Counter = Counter()
        for counts in self.docs.values():
            df.update(counts.keys())
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query: str, k: int) -> list[tuple[str, float]]:
        terms = analyze(query)
        scored = []
        for doc_id, counts in self.docs.items():
            score = 0.0
            for term in terms:
                tf = counts.get(term, 0)
                if tf:
                    norm = 1 - self.b + self.b * self.lengths[doc_id] / self.avg
                    score += self.idf[term] * tf * (self.k1 + 1) / (tf + self.k1 * norm)
            if score > 0:
                scored.append((doc_id, score))
        scored.sort(key=lambda x: (-x[1], x[0]))  # deterministic tie-break
        return scored[:k]


class TfidfCosine:
    name = "tfidf"

    def __init__(self, corpus: dict[str, str]):
        counts = {i: Counter(analyze(t)) for i, t in corpus.items()}
        df: Counter = Counter()
        for c in counts.values():
            df.update(c.keys())
        n = len(counts)
        self.idf = {t: math.log((1 + n) / (1 + f)) + 1 for t, f in df.items()}
        self.vectors = {i: self._vector(c) for i, c in counts.items()}

    def _vector(self, counts: Counter) -> dict[str, float]:
        vec = {t: (1 + math.log(c)) * self.idf.get(t, 0.0) for t, c in counts.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {t: v / norm for t, v in vec.items()}

    def search(self, query: str, k: int) -> list[tuple[str, float]]:
        q = self._vector(Counter(t for t in analyze(query) if t in self.idf))
        scored = []
        for doc_id, vec in self.vectors.items():
            score = sum(w * vec.get(t, 0.0) for t, w in q.items())
            if score > 0:
                scored.append((doc_id, score))
        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored[:k]


class ReciprocalRankFusion:
    """Hybrid retrieval: fuse several rankings with RRF (Cormack et al., k=60)."""

    name = "hybrid_rrf"

    def __init__(self, retrievers: list[Retriever], rrf_k: int = 60, depth: int = 20):
        self.retrievers, self.rrf_k, self.depth = retrievers, rrf_k, depth

    def search(self, query: str, k: int) -> list[tuple[str, float]]:
        fused: dict[str, float] = {}
        for retriever in self.retrievers:
            for rank, (doc_id, _) in enumerate(retriever.search(query, self.depth), start=1):
                fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (self.rrf_k + rank)
        return sorted(fused.items(), key=lambda x: (-x[1], x[0]))[:k]


class ReversedRetriever:
    """Deliberately broken system used only to prove the quality gate can fail."""

    name = "broken_reversed"

    def __init__(self, inner: Retriever):
        self.inner = inner

    def search(self, query: str, k: int) -> list[tuple[str, float]]:
        return list(reversed(self.inner.search(query, 50)))[:k]


class DenseRetriever:
    """Cosine similarity over local nomic-embed-text embeddings (cached; see ollama.py)."""

    name = "dense"

    def __init__(self, corpus: dict[str, str], embedder=None):
        from .ollama import Embedder  # imported lazily so lexical systems need no model access

        self.embedder = embedder or Embedder()
        self.vectors = {i: self._unit(self.embedder.embed(t, "document")) for i, t in corpus.items()}

    @staticmethod
    def _unit(vec: list[float]) -> list[float]:
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    def search(self, query: str, k: int) -> list[tuple[str, float]]:
        q = self._unit(self.embedder.embed(query, "query"))
        scored = [(i, sum(a * b for a, b in zip(q, v))) for i, v in self.vectors.items()]
        scored.sort(key=lambda x: (-x[1], x[0]))
        return scored[:k]


class LLMReranker:
    """Re-scores a first-stage candidate list with a local LLM judge, keeping stage-1 order on ties."""

    name = "rerank"

    def __init__(self, first_stage: Retriever, corpus: dict[str, str], judge=None, depth: int = 5):
        from .ollama import LLMJudge

        self.first, self.corpus, self.depth = first_stage, corpus, depth
        self.judge = judge or LLMJudge()

    def search(self, query: str, k: int) -> list[tuple[str, float]]:
        candidates = self.first.search(query, self.depth)
        rescored = [(doc_id, float(self.judge.score(query, self.corpus[doc_id])), -rank)
                    for rank, (doc_id, _) in enumerate(candidates)]
        rescored.sort(key=lambda x: (-x[1], -x[2]))
        return [(d, s) for d, s, _ in rescored][:k]


SYSTEMS = ["bm25", "tfidf", "hybrid", "dense", "hybrid_dense", "rerank", "broken"]


def build_system(name: str, corpus: dict[str, str]) -> Retriever:
    if name not in SYSTEMS:
        raise ValueError(f"unknown system {name!r}; choose from {SYSTEMS}")
    bm25, tfidf = BM25(corpus), TfidfCosine(corpus)
    if name == "bm25":
        return bm25
    if name == "tfidf":
        return tfidf
    if name == "hybrid":
        return ReciprocalRankFusion([bm25, tfidf])
    if name == "broken":
        return ReversedRetriever(bm25)
    dense = DenseRetriever(corpus)
    if name == "dense":
        return dense
    fused = ReciprocalRankFusion([bm25, tfidf, dense])
    fused.name = "hybrid_dense"
    return fused if name == "hybrid_dense" else LLMReranker(fused, corpus)
