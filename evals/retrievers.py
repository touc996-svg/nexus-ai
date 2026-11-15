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


def build_system(name: str, corpus: dict[str, str]) -> Retriever:
    bm25, tfidf = BM25(corpus), TfidfCosine(corpus)
    systems = {
        "bm25": bm25,
        "tfidf": tfidf,
        "hybrid": ReciprocalRankFusion([bm25, tfidf]),
        "broken": ReversedRetriever(bm25),
    }
    if name not in systems:
        raise ValueError(f"unknown system {name!r}; choose from {sorted(systems)}")
    return systems[name]
