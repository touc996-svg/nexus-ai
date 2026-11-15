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


def _baseline():
    return json.loads(BASELINE.read_text())


def test_evaluation_is_deterministic():
    assert _stable(evaluate("hybrid")) == _stable(evaluate("hybrid"))
    assert _stable(evaluate("rerank")) == _stable(evaluate("rerank"))


def test_current_system_passes_committed_baseline():
    baseline = _baseline()
    assert check_against_baseline(evaluate(baseline["system"]), baseline) == []
    assert main(["--check"]) == 0


def test_gate_fails_when_retrieval_is_broken():
    """Mutation check: a retriever returning results in reverse order must trip the gate."""
    baseline = json.loads(BASELINE.read_text())
    problems = check_against_baseline(evaluate("broken"), baseline)
    assert any(p.startswith("retrieval.") for p in problems)


def test_gate_fails_when_answers_are_not_grounded():
    """Mutation check: an answerer that invents a claim must trip the faithfulness gate."""
    baseline = json.loads(BASELINE.read_text())
    problems = check_against_baseline(evaluate(baseline["system"], hallucinate=True), baseline)
    assert any(p.startswith("answer.faithfulness") for p in problems)


def test_gate_flags_a_metric_missing_from_the_baseline():
    report = evaluate(_baseline()["system"])
    baseline = {"metrics": {k: v for k, v in flatten(report["summary"]).items() if k != "retrieval.mrr"}}
    assert any("retrieval.mrr" in p for p in check_against_baseline(report, baseline))


def test_unanswerable_questions_are_abstained_on():
    report = evaluate(_baseline()["system"])
    assert report["summary"]["abstention"]["correct_rate"] == 1.0


# ---- dense + reranker run offline from the committed cache ---------------------------------

def test_model_backed_systems_never_touch_the_network(monkeypatch):
    """The cache must cover the whole dataset; any live call would raise here."""
    import urllib.request

    def forbidden(*a, **k):
        raise AssertionError("network call attempted; embeddings/rerank cache is incomplete")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    for system in ("dense", "hybrid_dense", "rerank"):
        assert evaluate(system)["counts"]["answerable"] == 25


def test_reranker_improves_top_rank_over_lexical_hybrid():
    lexical, reranked = evaluate("hybrid")["summary"]["retrieval"], evaluate("rerank")["summary"]["retrieval"]
    assert reranked["mrr"] > lexical["mrr"]
    assert reranked["recall_at_3"] >= lexical["recall_at_3"]


def test_cache_miss_is_an_error_not_a_guess(monkeypatch, tmp_path):
    from evals import ollama

    monkeypatch.setattr(ollama, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(ollama, "BASE_URL", "http://127.0.0.1:9")  # nothing listens here
    with pytest.raises(ollama.CacheMiss):
        ollama.Embedder().embed("never seen before", "query")


# ---- LLM answerer: parsing, refusals, offline replay ---------------------------------------

class _Stub:
    def __init__(self, text):
        self.text = text

    def generate(self, question, passages):
        return self.text


def _llm(text):
    from evals.answerer import LLMAnswerer
    from evals.retrievers import build_system

    corpus, _ = load_dataset()
    return LLMAnswerer(build_system("bm25", corpus), corpus, generator=_Stub(text))


def test_llm_answerer_parses_and_strips_citations():
    ans = _llm("Findings are linked at a score of 2 [d03] and grouped. [d03]").answer("When are two findings linked?")
    assert ans.citations == ["d03"] and "[" not in ans.text and not ans.abstained


def test_llm_answerer_ignores_citations_it_was_not_shown():
    ans = _llm("Some claim [d99].").answer("When are two findings linked?")
    assert ans.citations == []


def test_llm_answerer_honours_refusal():
    ans = _llm("INSUFFICIENT_INFORMATION").answer("When are two findings linked?")
    assert ans.abstained and ans.citations == []


def test_wrong_source_citation_is_penalised():
    """Right content cited to the wrong document (seen in a real run) must score zero precision."""
    assert m.citation_precision(["d10"], {"d08"}) == 0.0


def test_llm_generation_replays_offline_from_cache(monkeypatch):
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("network call attempted")))
    report = evaluate("rerank", answerer="llm")
    assert report["answerer"] == "llm" and report["counts"]["answerable"] == 25
    assert report["summary"]["abstention"]["correct_rate"] == 1.0


def test_hallucination_mutation_is_caught_for_the_llm_path_too():
    clean = evaluate("rerank", answerer="llm")["summary"]["answer"]["faithfulness"]
    dirty = evaluate("rerank", answerer="llm", hallucinate=True)["summary"]["answer"]["faithfulness"]
    assert dirty < clean


def test_llm_answer_without_a_valid_citation_fails_closed():
    """Leaked reasoning or an uncited claim is not a grounded answer; it must be a refusal."""
    ans = _llm("Hmm, the user is asking about billing. Let me look at the passages.").answer("When are two findings linked?")
    assert ans.abstained and ans.citations == []


def test_think_blocks_are_stripped_before_citation_parsing():
    ans = _llm("<think>maybe [d09]</think>Linked at score 2 [d03].").answer("When are two findings linked?")
    assert ans.citations == ["d03"] and "think" not in ans.text
