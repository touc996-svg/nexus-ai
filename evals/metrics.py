"""Pure metric functions. No I/O, no randomness, no network."""
from __future__ import annotations

import math
import re

_WORD = re.compile(r"[a-z0-9_.]+")
STOPWORDS = frozenset(
    "a an and are as at be by can does do for from has have how if in into is it its of on or "
    "so that the their there this to was were what when where which who why with without not no "
    "next used use uses than then them they these those both each".split()
)


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens with sentence punctuation stripped from the edges."""
    return [t.strip(".") for t in _WORD.findall(text.lower()) if t.strip(".")]


def content_tokens(text: str) -> list[str]:
    return [t for t in tokenize(text) if t not in STOPWORDS]


def hit_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    return 1.0 if relevant & set(ranked[:k]) else 0.0


def recall_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        raise ValueError("recall is undefined without relevant documents")
    return len(relevant & set(ranked[:k])) / len(relevant)


def reciprocal_rank(ranked: list[str], relevant: set[str]) -> float:
    for position, doc_id in enumerate(ranked, start=1):
        if doc_id in relevant:
            return 1.0 / position
    return 0.0


def ndcg_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    """Binary-relevance nDCG."""
    if not relevant:
        raise ValueError("nDCG is undefined without relevant documents")
    dcg = sum(1.0 / math.log2(i + 2) for i, d in enumerate(ranked[:k]) if d in relevant)
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(relevant), k)))
    return dcg / ideal


def support_ratio(answer: str, contexts: list[str]) -> float:
    """Share of the answer's content tokens that appear in the supplied contexts.

    A lexical groundedness proxy: 1.0 means every content word of the answer is
    present in the evidence. It catches invented words and numbers; it cannot
    catch a fluent misreading of correct words (a model-based judge is needed
    for that, and is deliberately a later layer).
    """
    answer_tokens = content_tokens(answer)
    if not answer_tokens:
        return 1.0
    evidence = set()
    for context in contexts:
        evidence.update(content_tokens(context))
    return sum(t in evidence for t in answer_tokens) / len(answer_tokens)


def keywords_present(answer: str, keywords: list[str]) -> bool:
    lowered = answer.lower()
    return all(k.lower() in lowered for k in keywords)


def citation_precision(cited: list[str], relevant: set[str]) -> float:
    """Fraction of cited documents that are actually relevant (1.0 if nothing cited is wrong)."""
    if not cited:
        return 0.0
    return len([c for c in cited if c in relevant]) / len(cited)
