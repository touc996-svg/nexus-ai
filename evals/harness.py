"""Runs a system over the golden dataset and gates the result against a baseline."""
from __future__ import annotations

import json
from pathlib import Path
from statistics import mean
from time import perf_counter

from . import metrics as m
from .answerer import ExtractiveAnswerer, HallucinatingAnswerer, LLMAnswerer
from .retrievers import build_system

DATA = Path(__file__).parent / "datasets"
K = 3

# metric -> direction ("higher" is better / "lower" is better)
GATED = {
    "retrieval.hit_at_1": "higher",
    "retrieval.recall_at_3": "higher",
    "retrieval.mrr": "higher",
    "retrieval.ndcg_at_3": "higher",
    "answer.correct_rate": "higher",
    "answer.faithfulness": "higher",
    "answer.citation_precision": "higher",
    "abstention.correct_rate": "higher",
    "abstention.false_rate": "lower",
}


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


SPLITS = {"dev": "questions.jsonl", "heldout": "heldout.jsonl"}


def load_dataset(data_dir: Path = DATA, split: str = "dev") -> tuple[dict[str, str], list[dict]]:
    if split not in SPLITS:
        raise ValueError(f"unknown split {split!r}; choose from {sorted(SPLITS)}")
    corpus = {d["id"]: d["text"] for d in load_jsonl(data_dir / "corpus.jsonl")}
    return corpus, load_jsonl(data_dir / SPLITS[split])


def validate_dataset(corpus: dict[str, str], questions: list[dict]) -> None:
    ids = [q["id"] for q in questions]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate question ids")
    for q in questions:
        unknown = set(q["relevant"]) - set(corpus)
        if unknown:
            raise ValueError(f"{q['id']} references unknown documents {sorted(unknown)}")
        if q["answerable"] and not q["relevant"]:
            raise ValueError(f"{q['id']} is answerable but has no relevant documents")
        if not q["answerable"] and q["relevant"]:
            raise ValueError(f"{q['id']} is unanswerable but lists relevant documents")


def _avg(values: list[float]) -> float:
    return round(mean(values), 4) if values else 0.0


def evaluate(system: str = "hybrid", hallucinate: bool = False, data_dir: Path = DATA,
             answerer: str = "extractive", split: str = "dev") -> dict:
    corpus, questions = load_dataset(data_dir, split)
    validate_dataset(corpus, questions)
    retriever = build_system(system, corpus)
    if answerer not in ("extractive", "llm"):
        raise ValueError("answerer must be 'extractive' or 'llm'")
    gen = (LLMAnswerer(retriever, corpus, k=K) if answerer == "llm"
           else ExtractiveAnswerer(retriever, corpus, k=K))
    if hallucinate:
        gen = HallucinatingAnswerer(gen)

    rows, latencies = [], []
    for q in questions:
        relevant = set(q["relevant"])
        started = perf_counter()
        ranked = [d for d, _ in retriever.search(q["question"], 10)]
        ans = gen.answer(q["question"])
        latencies.append((perf_counter() - started) * 1000)
        row = {"id": q["id"], "difficulty": q["difficulty"], "answerable": q["answerable"],
               "retrieved": ranked[:K], "abstained": ans.abstained, "citations": ans.citations, "answer": ans.text}
        if q["answerable"]:
            row.update(
                hit_at_1=m.hit_at_k(ranked, relevant, 1),
                recall_at_3=m.recall_at_k(ranked, relevant, K),
                rr=m.reciprocal_rank(ranked, relevant),
                ndcg_at_3=m.ndcg_at_k(ranked, relevant, K),
            )
            if not ans.abstained:
                row.update(
                    correct=m.keywords_present(ans.text, q["answer_keywords"]),
                    faithfulness=m.support_ratio(ans.text, [corpus[c] for c in (ans.citations or ranked[:K])]),
                    citation_precision=m.citation_precision(ans.citations, relevant),
                )
            else:
                row.update(correct=False)
        rows.append(row)

    ans_rows = [r for r in rows if r["answerable"]]
    unans_rows = [r for r in rows if not r["answerable"]]
    given = [r for r in ans_rows if not r["abstained"]]
    summary = {
        "retrieval": {
            "hit_at_1": _avg([r["hit_at_1"] for r in ans_rows]),
            "recall_at_3": _avg([r["recall_at_3"] for r in ans_rows]),
            "mrr": _avg([r["rr"] for r in ans_rows]),
            "ndcg_at_3": _avg([r["ndcg_at_3"] for r in ans_rows]),
        },
        "answer": {
            "correct_rate": _avg([1.0 if r["correct"] else 0.0 for r in ans_rows]),
            "faithfulness": _avg([r["faithfulness"] for r in given]),
            "citation_precision": _avg([r["citation_precision"] for r in given]),
        },
        "abstention": {
            "correct_rate": _avg([1.0 if r["abstained"] else 0.0 for r in unans_rows]),
            "false_rate": _avg([1.0 if r["abstained"] else 0.0 for r in ans_rows]),
        },
    }
    ordered = sorted(latencies)
    return {
        "system": system,
        "answerer": answerer,
        "split": split,
        "hallucinating_answerer": hallucinate,
        "counts": {"answerable": len(ans_rows), "unanswerable": len(unans_rows), "documents": len(corpus)},
        "summary": summary,
        "failures": [r["id"] for r in ans_rows if not r["correct"]],
        "per_question": rows,
        # wall-clock timing is informational only: never gated, excluded from determinism checks
        "timing_ms": {"p50": round(ordered[len(ordered) // 2], 3), "max": round(ordered[-1], 3)},
    }


def flatten(summary: dict) -> dict[str, float]:
    return {f"{g}.{k}": v for g, vals in summary.items() for k, v in vals.items()}


def check_against_baseline(report: dict, baseline: dict, tolerance: float = 0.02) -> list[str]:
    """Return human-readable regressions (empty list means the gate passes)."""
    current, reference = flatten(report["summary"]), baseline["metrics"]
    problems = []
    for name, direction in GATED.items():
        if name not in reference:
            problems.append(f"{name}: missing from baseline")
            continue
        old, new = reference[name], current[name]
        worse = new < old - tolerance if direction == "higher" else new > old + tolerance
        if worse:
            problems.append(f"{name}: {old} -> {new} (regression beyond {tolerance})")
    return problems
