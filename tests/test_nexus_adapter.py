"""Tests for the adapter that evaluates the real NexusRAG.

What is REAL here: the production ``NexusRAG`` code (ingestion, chunking, search, filters, threshold) and
qdrant-client running in its in-process local mode.
What is REPLAYED: the Ollama embedding response, from the committed cache ``evals/cache/nexus_embeddings.json``.
What is NOT covered: a Qdrant *server* (Docker was not running when these were written). Tests that need
the cache are skipped nowhere: an incomplete cache fails them, on purpose.
"""
import httpx
import pytest

from app.services.rag import NexusRAG
from evals import nexus_adapter
from evals.harness import evaluate, load_dataset
from evals.nexus_adapter import EVAL_COLLECTION_PREFIX, PRODUCTION_COLLECTION, NexusRagRetriever, ReplayedEmbedding
from evals.ollama import CacheMiss


@pytest.fixture
def corpus():
    return load_dataset()[0]


@pytest.fixture
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network call attempted; the embedding cache is incomplete")

    monkeypatch.setattr(httpx, "post", forbidden)


# ---- isolation from production data ----------------------------------------------------------

@pytest.mark.integration_local_qdrant
def test_uses_an_isolated_collection_never_the_production_one(corpus, no_network):
    retriever = NexusRagRetriever(corpus)
    names = {c.name for c in retriever.client.get_collections().collections}
    assert retriever.collection.startswith(EVAL_COLLECTION_PREFIX)
    assert PRODUCTION_COLLECTION not in names
    assert names == {retriever.collection}


@pytest.mark.integration_local_qdrant
def test_ignores_QDRANT_URL_so_it_cannot_reach_a_production_server(corpus, no_network, monkeypatch):
    monkeypatch.setenv("QDRANT_URL", "http://127.0.0.1:9")  # nothing listens here; a connection would fail
    assert NexusRagRetriever(corpus).search("When are two findings linked?", 3)


@pytest.mark.integration_local_qdrant
def test_two_adapters_do_not_share_data(corpus, no_network):
    a, b = NexusRagRetriever(corpus), NexusRagRetriever(corpus)
    assert a.collection != b.collection and a.user_id != b.user_id


@pytest.mark.integration_local_qdrant
def test_close_removes_the_evaluation_collection(corpus, no_network):
    retriever = NexusRagRetriever(corpus)
    retriever.close()
    assert not retriever.client.collection_exists(retriever.collection)


# ---- faithful to production behaviour --------------------------------------------------------

@pytest.mark.integration_local_qdrant
def test_ranking_matches_calling_nexusrag_search_directly(corpus, no_network):
    retriever = NexusRagRetriever(corpus)
    query = "What time window is used to correlate findings?"
    direct = retriever.rag.search(query, user_id=retriever.user_id, limit=10)
    adapted = retriever.search(query, 10)
    assert [d for d, _ in adapted] == list(dict.fromkeys(h["source"] for h in direct))
    assert adapted[0][0] == "d03"


@pytest.mark.integration_local_qdrant
def test_production_defaults_are_untouched(corpus, no_network, monkeypatch):
    monkeypatch.delenv("NEXUS_RAG_MIN_SCORE", raising=False)
    retriever = NexusRagRetriever(corpus)
    assert retriever.rag.score_threshold == 0.15
    assert retriever.rag.COLLECTION != NexusRAG.COLLECTION  # instance override only; the class is unchanged
    assert NexusRAG.COLLECTION == PRODUCTION_COLLECTION


@pytest.mark.integration_local_qdrant
def test_multi_chunk_documents_collapse_to_one_ranked_entry(no_network, monkeypatch):
    words = " ".join(f"alpha{i}" for i in range(2500))  # about 3 chunks at the production chunk size
    monkeypatch.setattr(ReplayedEmbedding, "__call__", lambda self, text: [1.0] + [0.0] * 767)
    retriever = NexusRagRetriever({"big": words})
    points = retriever.client.count(retriever.collection).count
    ranked = retriever.search("alpha5", 5)
    assert points >= 3 and [d for d, _ in ranked] == ["big"]


# ---- offline replay and failure behaviour ----------------------------------------------------

@pytest.mark.integration_local_qdrant
def test_dense_system_replays_offline_from_the_committed_cache(no_network):
    report = evaluate("nexus_dense")
    assert report["counts"]["answerable"] == 25
    assert 0.0 <= report["summary"]["retrieval"]["mrr"] <= 1.0
    assert "retrieval_p50" in report["timing_ms"]


@pytest.mark.unit_no_qdrant
def test_cache_miss_with_ollama_down_raises_instead_of_guessing(tmp_path, monkeypatch):
    from evals import ollama

    monkeypatch.setattr(ollama, "CACHE_DIR", tmp_path)

    def broken_embed(text):
        raise RuntimeError("Ollama embedding request failed (ConnectError).")

    embed = ReplayedEmbedding(broken_embed, "nomic-embed-text")
    with pytest.raises(CacheMiss):
        embed("a query nobody cached")


@pytest.mark.unit_no_qdrant
def test_recorded_latency_is_stored_with_the_vector(tmp_path, monkeypatch):
    from evals import ollama

    monkeypatch.setattr(ollama, "CACHE_DIR", tmp_path)
    embed = ReplayedEmbedding(lambda text: [0.1, 0.2], "m")
    embed("x")
    assert embed.cache.get(next(iter(embed.cache.data)))["ms"] >= 0
    embed.cache.flush()
    again = ReplayedEmbedding(lambda text: pytest.fail("must replay"), "m")
    assert again("x") == [0.1, 0.2]


# ---- no leakage between splits ---------------------------------------------------------------

@pytest.mark.integration_local_qdrant
def test_development_run_never_embeds_a_held_out_question(no_network, monkeypatch):
    seen = []
    original = ReplayedEmbedding.__call__

    def spy(self, text):
        seen.append(text)
        return original(self, text)

    monkeypatch.setattr(ReplayedEmbedding, "__call__", spy)
    evaluate("nexus_dense", split="dev")
    held_out = {q["question"] for q in load_dataset(split="heldout")[1]}
    assert seen and not held_out & set(seen)


@pytest.mark.unit_no_qdrant
def test_adapter_module_never_references_the_held_out_split():
    source = open(nexus_adapter.__file__).read()
    assert "heldout" not in source and "load_dataset" not in source


# ---- tenant isolation is exercised during evaluation (consistent with the merged identity work) ----------

@pytest.mark.integration_local_qdrant
def test_search_is_confined_to_the_evaluation_user_even_with_other_tenants_in_the_collection(corpus, no_network):
    """REAL NexusRAG filter + local Qdrant: a decoy document stored for another user must never be returned."""
    retriever = NexusRagRetriever(corpus)
    # an identical copy of a real document, stored for a different tenant in the same collection
    retriever.rag.ingest_text(corpus["d03"], source="decoy", user_id="another-tenant")
    query = "What time window is used to correlate findings?"
    ranked = [d for d, _ in retriever.search(query, 10)]
    assert "decoy" not in ranked and ranked[0] == "d03"
    leaked = retriever.rag.search(query, user_id="another-tenant", limit=5)
    assert [h["source"] for h in leaked] == ["decoy"]  # the decoy exists; it is simply invisible to the eval user


# ---- cache keys cannot serve stale or cross-configuration values -----------------------------------------

@pytest.mark.unit_no_qdrant
def test_embedding_key_changes_with_model_dimension_and_production_code(tmp_path, monkeypatch):
    from evals import ollama

    monkeypatch.setattr(ollama, "CACHE_DIR", tmp_path)
    base = ReplayedEmbedding(lambda t: [0.0], "nomic-embed-text", 768).config
    assert ReplayedEmbedding(lambda t: [0.0], "other-model", 768).config != base
    assert ReplayedEmbedding(lambda t: [0.0], "nomic-embed-text", 1024).config != base
    monkeypatch.setattr(nexus_adapter, "production_rag_sha", lambda: "a-different-rag-py")
    assert ReplayedEmbedding(lambda t: [0.0], "nomic-embed-text", 768).config != base


@pytest.mark.unit_no_qdrant
def test_a_vector_cached_under_one_configuration_is_not_served_to_another(tmp_path, monkeypatch):
    from evals import ollama

    monkeypatch.setattr(ollama, "CACHE_DIR", tmp_path)
    ReplayedEmbedding(lambda t: [1.0], "model-a", 768)("same text")
    other = ReplayedEmbedding(lambda t: [2.0], "model-b", 768)
    assert other("same text") == [2.0]  # recomputed, not replayed from model-a


@pytest.mark.unit_no_qdrant
def test_generation_and_judge_keys_change_with_prompt_schema_options_and_model(monkeypatch):
    from evals.ollama import LLMGenerator, LLMJudge

    base = LLMGenerator("m", structured=True).config
    assert LLMGenerator("m2", structured=True).config != base
    assert LLMGenerator("m", structured=False).config != base
    monkeypatch.setattr(LLMGenerator, "OPTIONS", {"temperature": 0.7, "seed": 0, "num_predict": 200})
    assert LLMGenerator("m", structured=True).config != base
    monkeypatch.undo()
    monkeypatch.setattr(LLMGenerator, "STRUCTURED_PROMPT", "different prompt {passages} {q}")
    assert LLMGenerator("m", structured=True).config != base
    judge = LLMJudge("m").config
    monkeypatch.setattr(LLMJudge, "PROMPT", "changed {q} {p}")
    assert LLMJudge("m").config != judge


@pytest.mark.unit_no_qdrant
def test_changing_the_retrieved_context_changes_the_generation_key(tmp_path, monkeypatch):
    """The key embeds the exact retrieved passages in order, so a different retrieval can't reuse an answer."""
    from evals import ollama
    from evals.ollama import LLMGenerator

    monkeypatch.setattr(ollama, "CACHE_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(ollama, "_post", lambda path, payload, timeout=None: calls.append(1) or
                        {"message": {"content": "{}"}})
    generator = LLMGenerator("m", structured=True)
    generator.generate("q", [("d1", "alpha"), ("d2", "beta")])
    generator.generate("q", [("d1", "alpha"), ("d2", "beta")])   # identical context: replayed
    generator.generate("q", [("d2", "beta"), ("d1", "alpha")])   # same passages, different order: recomputed
    assert len(calls) == 2


@pytest.mark.unit_no_qdrant
def test_legacy_entries_are_adopted_only_for_the_pinned_configuration(tmp_path, monkeypatch):
    from evals import ollama
    from evals.ollama import JsonCache, LLMJudge, _key

    monkeypatch.setattr(ollama, "CACHE_DIR", tmp_path)
    legacy = JsonCache("rerank.json")
    legacy.put(_key("m", "q", "p"), 7)
    legacy.flush()
    monkeypatch.setattr(ollama, "_post", lambda *a, **k: {"message": {"content": "3"}})
    unpinned = LLMJudge("m")  # its fingerprint is not in LEGACY_ADOPTABLE, so the old entry must be ignored
    assert unpinned.score("q", "p") == 3


# ---- retrieval results are never replayed: configuration changes take effect immediately ----------------

@pytest.mark.integration_local_qdrant
def test_score_threshold_changes_results_without_any_cache_invalidation(corpus, no_network):
    strict = NexusRagRetriever(corpus)
    strict.rag.score_threshold = 0.99
    loose = NexusRagRetriever(corpus)
    query = "When are two findings linked?"
    assert len(strict.search(query, 5)) < len(loose.search(query, 5))


@pytest.mark.qdrant_server
def test_against_a_real_qdrant_server(corpus, no_network):
    """Runs only with a live Qdrant server (EVALS_QDRANT_URL=http://localhost:6333); skipped otherwise."""
    import os

    url = os.getenv("EVALS_QDRANT_URL")
    if not url:
        pytest.skip("EVALS_QDRANT_URL not set: no Qdrant server was tested")
    retriever = NexusRagRetriever(corpus, qdrant_url=url)
    try:
        assert retriever.search("What time window is used to correlate findings?", 3)[0][0] == "d03"
        assert retriever.collection != PRODUCTION_COLLECTION
    finally:
        retriever.close()
    assert not retriever.client.collection_exists(retriever.collection)


@pytest.mark.unit_no_qdrant
def test_frozen_configuration_still_matches_the_live_one():
    """If production RAG code, defaults, prompts or any dataset file change, the held-out result is no longer comparable."""
    from evals.freeze_nexus_config import FROZEN, current_config

    import json as _json

    assert _json.loads(FROZEN.read_text()) == current_config()
