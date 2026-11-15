"""Grounded answerers: retrieve, then answer only from evidence or abstain."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .metrics import content_tokens
from .retrievers import Retriever, analyze


@dataclass
class Answer:
    text: str
    citations: list[str] = field(default_factory=list)
    abstained: bool = False


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


class ExtractiveAnswerer:
    """Answers with the best-matching source sentences and cites their documents.

    Abstains when the top document covers too little of the question, which is
    the cheap, auditable alternative to answering from nothing.
    """

    def __init__(self, retriever: Retriever, corpus: dict[str, str], k: int = 3,
                 min_coverage: float = 0.3, max_sentences: int = 2):
        self.retriever, self.corpus = retriever, corpus
        self.k, self.min_coverage, self.max_sentences = k, min_coverage, max_sentences

    def answer(self, question: str) -> Answer:
        hits = self.retriever.search(question, self.k)
        query_terms = set(analyze(question))
        if not hits or not query_terms:
            return Answer("I don't have enough information to answer that.", [], True)
        top_terms = set(analyze(self.corpus[hits[0][0]]))
        if len(query_terms & top_terms) / len(query_terms) < self.min_coverage:
            return Answer("I don't have enough information to answer that.", [], True)

        scored = []
        for doc_id, _ in hits:
            for sentence in _sentences(self.corpus[doc_id]):
                overlap = len(query_terms & set(analyze(sentence)))
                if overlap:
                    scored.append((overlap, doc_id, sentence))
        scored.sort(key=lambda x: (-x[0], x[1], x[2]))
        chosen = scored[: self.max_sentences]
        if not chosen:
            return Answer("I don't have enough information to answer that.", [], True)
        citations = list(dict.fromkeys(d for _, d, _ in chosen))
        return Answer(" ".join(s for _, _, s in chosen), citations, False)


class HallucinatingAnswerer:
    """Deliberately unfaithful: appends an unsupported claim. Used to prove the gate catches it."""

    def __init__(self, inner: ExtractiveAnswerer):
        self.inner = inner

    def answer(self, question: str) -> Answer:
        base = self.inner.answer(question)
        if base.abstained:
            return base
        return Answer(base.text + " It processes forty billion events per second on quantum hardware.",
                      base.citations, False)


__all__ = ["Answer", "ExtractiveAnswerer", "HallucinatingAnswerer", "content_tokens"]
