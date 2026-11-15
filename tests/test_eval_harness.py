"""Tests for the evaluation harness itself, including proof that the gate can fail."""
import json
import math

import pytest

from evals import metrics as m
from evals.harness import check_against_baseline, evaluate, flatten, load_dataset, validate_dataset
from evals.run import BASELINE, main


# ---- metrics against hand-computed values -------------------------------------------------

def test_recall_hit_and_rr():
    ranked = ["a", "b", "c", "d"]
    assert m.hit_at_k(ranked, {"c"}, 2) == 0.0
    assert m.hit_at_k(ranked, {"c"}, 3) == 1.0
    assert m.recall_at_k(ranked, {"a", "d"}, 3) == 0.5
    assert m.reciprocal_rank(ranked, {"c", "d"}) == pytest.approx(1 / 3)
    assert m.reciprocal_rank(ranked, {"z"}) == 0.0


def test_ndcg_perfect_and_degraded():
    assert m.ndcg_at_k(["a", "b"], {"a", "b"}, 2) == pytest.approx(1.0)
    expected = (1 / math.log2(3)) / 1.0  # single relevant doc at rank 2, ideal DCG = 1
    assert m.ndcg_at_k(["x", "a"], {"a"}, 2) == pytest.approx(expected)


def test_undefined_metrics_raise_instead_of_returning_misleading_numbers():
    with pytest.raises(ValueError):
        m.recall_at_k(["a"], set(), 1)
    with pytest.raises(ValueError):
        m.ndcg_at_k(["a"], set(), 1)


def test_support_ratio_detects_invented_content():
    ctx = ["The window is 120 minutes."]
    assert m.support_ratio("The window is 120 minutes", ctx) == 1.0
    assert m.support_ratio("The window is 500 minutes on quantum hardware", ctx) < 0.7


def test_citation_precision():
    assert m.citation_precision(["a", "b"], {"a"}) == 0.5
    assert m.citation_precision([], {"a"}) == 0.0


# ---- dataset integrity ---------------------------------------------------------------------

def test_dataset_is_valid_and_has_unanswerable_questions():
    corpus, questions = load_dataset()
    validate_dataset(corpus, questions)
    assert sum(not q["answerable"] for q in questions) >= 3
    assert any(len(q["relevant"]) > 1 for q in questions), "needs a multi-document question"


def test_validation_rejects_dangling_references():
    corpus, questions = load_dataset()
    bad = questions + [{"id": "qx", "question": "?", "relevant": ["nope"], "answer_keywords": [],
                        "answerable": True, "difficulty": "easy"}]
    with pytest.raises(ValueError):
        validate_dataset(corpus, bad)


# ---- determinism and the regression gate ---------------------------------------------------

def _stable(report):
    return {k: v for k, v in report.items() if k != "timing_ms"}


def test_evaluation_is_deterministic():
    assert _stable(evaluate("hybrid")) == _stable(evaluate("hybrid"))


def test_current_system_passes_committed_baseline():
    baseline = json.loads(BASELINE.read_text())
    assert check_against_baseline(evaluate("hybrid"), baseline) == []
    assert main(["--check"]) == 0


def test_gate_fails_when_retrieval_is_broken():
    """Mutation check: a retriever returning results in reverse order must trip the gate."""
    baseline = json.loads(BASELINE.read_text())
    problems = check_against_baseline(evaluate("broken"), baseline)
    assert any(p.startswith("retrieval.") for p in problems)


def test_gate_fails_when_answers_are_not_grounded():
    """Mutation check: an answerer that invents a claim must trip the faithfulness gate."""
    baseline = json.loads(BASELINE.read_text())
    problems = check_against_baseline(evaluate("hybrid", hallucinate=True), baseline)
    assert any(p.startswith("answer.faithfulness") for p in problems)


def test_gate_flags_a_metric_missing_from_the_baseline():
    report = evaluate("hybrid")
    baseline = {"metrics": {k: v for k, v in flatten(report["summary"]).items() if k != "retrieval.mrr"}}
    assert any("retrieval.mrr" in p for p in check_against_baseline(report, baseline))


def test_unanswerable_questions_are_abstained_on():
    report = evaluate("hybrid")
    assert report["summary"]["abstention"]["correct_rate"] == 1.0
