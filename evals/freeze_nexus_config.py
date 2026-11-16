"""Write (or verify) the frozen configuration used for the single held-out run of the real NexusRAG systems.

No parameter was tuned: the production defaults are used as they are. The file records them, together with
hashes of the production RAG code and of every dataset file, so any later change is detectable.

    python -m evals.freeze_nexus_config           # write evals/frozen_nexus_config.json
    python -m evals.freeze_nexus_config --check   # exit 1 if the live configuration differs
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import sys
from pathlib import Path

from .harness import DATA, K
from .nexus_adapter import production_rag_sha
from .ollama import EMBED_MODEL, GEN_MODEL, RERANK_MODEL, LLMGenerator, LLMJudge

FROZEN = Path(__file__).parent / "frozen_nexus_config.json"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def current_config() -> dict:
    from app.services.rag import NexusRAG

    chunk = inspect.signature(NexusRAG.chunk_text).parameters
    rag = NexusRAG(demo_mode=True)  # reads defaults only; creates no collection and makes no network call
    return {
        "note": "No parameter was tuned. Production defaults are recorded as they are.",
        "systems": ["nexus_dense", "nexus_hybrid", "nexus_rerank"],
        "production": {
            "rag_py_sha": production_rag_sha(),
            "embedding_model": rag.embedding_model,
            "embedding_dimension": rag.vector_size,
            "score_threshold": rag.score_threshold,
            "chunk_size": chunk["chunk_size"].default,
            "chunk_overlap": chunk["overlap"].default,
            "distance": "cosine",
            "tenant_filter": "user_id (unique per evaluation run)",
            "collection": "nexus_eval_<random> (isolated; production collection never used)",
        },
        "harness": {
            "search_depth": 10, "answer_context_k": K, "rrf_k": 60, "fusion_depth": 20, "rerank_depth": 5,
            "embed_model_env_default": EMBED_MODEL, "rerank_model": RERANK_MODEL, "generator_model": GEN_MODEL,
            "judge_fingerprint": LLMJudge(RERANK_MODEL).config,
            "generator_fingerprint": LLMGenerator(GEN_MODEL, structured=True).config,
            "answerers": ["extractive", "llm (structured output, fails closed)"],
        },
        "datasets": {name: _sha(DATA / name) for name in ("corpus.jsonl", "questions.jsonl", "heldout.jsonl")},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    live = current_config()
    if args.check:
        frozen = json.loads(FROZEN.read_text())
        if frozen != live:
            changed = [k for k in live if live[k] != frozen.get(k)]
            print(f"frozen configuration differs in: {changed}", file=sys.stderr)
            return 1
        print("frozen configuration matches")
        return 0
    FROZEN.write_text(json.dumps(live, indent=2, sort_keys=True) + "\n")
    print(f"written {FROZEN}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
