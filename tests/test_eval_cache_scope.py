"""The evaluation replay cache is keyed on the embedding/retrieval code, not on the whole rag.py file."""

import pytest

from evals import nexus_adapter
from evals.nexus_adapter import HASHED_MEMBERS, ReplayedEmbedding, members_sha


def test_client_and_plumbing_are_not_part_of_the_hash():
    from app.services.rag import NexusRAG

    base = members_sha(NexusRAG)
    assert base == nexus_adapter.production_rag_sha()
    assert "client" not in HASHED_MEMBERS and "health_check" not in HASHED_MEMBERS and "extract_text" not in HASHED_MEMBERS


@pytest.mark.parametrize("member", HASHED_MEMBERS)
def test_changing_any_hashed_member_changes_the_hash(member, monkeypatch):
    from app.services import rag as rag_module

    real = getattr(rag_module.NexusRAG, member)
    base = members_sha(rag_module.NexusRAG)

    def replacement(*args, **kwargs):  # different source text
        return "changed"

    monkeypatch.setattr(rag_module.NexusRAG, member, replacement)
    assert members_sha(rag_module.NexusRAG) != base
    monkeypatch.setattr(rag_module.NexusRAG, member, real)
    assert members_sha(rag_module.NexusRAG) == base


def test_whole_file_vectors_are_adopted_only_while_the_members_are_unchanged(tmp_path, monkeypatch):
    from evals import ollama

    monkeypatch.setattr(ollama, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(ollama, "INFERENCE_BLOCKED", True)
    live = nexus_adapter.production_rag_sha()
    monkeypatch.setattr(nexus_adapter, "PRE_NARROWING_MEMBERS_SHA", live)  # the pin matches the live code
    old = ReplayedEmbedding(lambda t: [9.0], "nomic-embed-text", 768)
    from evals.ollama import _key, fingerprint

    previous = fingerprint("nexus-embed", "nomic-embed-text", 768, ReplayedEmbedding.CONTRACT,
                           nexus_adapter.PRE_NARROWING_RAG_SHA)
    old.cache.put(_key("nexus-embed", previous, "hello"), {"vector": [0.5], "ms": 3.0})
    adopted = ReplayedEmbedding(lambda t: [9.0], "nomic-embed-text", 768)
    adopted.cache = old.cache
    assert adopted("hello") == [0.5]

    # a pin that does not match the live members adopts nothing: the lookup misses and, with inference blocked, fails closed
    monkeypatch.setattr(nexus_adapter, "PRE_NARROWING_MEMBERS_SHA", "0" * 16)
    strict = ReplayedEmbedding(lambda t: [9.0], "nomic-embed-text", 768)
    strict.cache = old.cache
    assert strict.previous_config is None
    with pytest.raises(ollama.CacheMiss):
        strict("never recorded")
