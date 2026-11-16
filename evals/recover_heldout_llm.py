"""Rebuild the held-out structured-generation cache entries that were lost, and compare each with the original report.

Scope is deliberately narrow:
  * only generations for the three real-NexusRAG systems on the held-out split, with the frozen generator
    configuration, and only entries that are missing from the cache;
  * retrieval, embeddings and rerank judgements are replayed from the cache with inference blocked, so the
    only model call this tool can make is the generator call for a missing entry;
  * the original reports are never written. A rebuilt answer is stored in the cache ONLY if it equals what the
    original report recorded (answer text, citations, abstention). A different answer is quarantined in the
    recovery report and never enters the cache, so replay can never silently show a result that differs from the
    recorded one;
  * it aborts before any model call if the frozen configuration changed or if the missing set is not exactly
    the approved size.

    python -m evals.recover_heldout_llm --plan      # list the missing entries; no model call
    python -m evals.recover_heldout_llm --execute   # generate, compare, store matches
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import ollama
from .answerer import LLMAnswerer
from .freeze_nexus_config import FROZEN, current_config
from .harness import K, load_dataset
from .ollama import LLMGenerator
from .retrievers import NEXUS_SYSTEMS, build_system, close_all

REPORTS = Path(__file__).parent / "reports"
RECOVERY = Path(__file__).parent / "recovery"
APPROVED_FROZEN_SHA = "e5bad09980fb8f5f"
APPROVED_MISSING = {"nexus_dense": 22, "nexus_hybrid": 18, "nexus_rerank": 10}
APPROVED_DISTINCT = 44
MODEL = "qwen3:4b"


def frozen_sha() -> str:
    return hashlib.sha256(FROZEN.read_bytes()).hexdigest()[:16]


def answer_fields(answer) -> dict:
    return {"answer": answer.text, "citations": answer.citations, "abstained": answer.abstained}


def build_plan(generator: LLMGenerator, corpus: dict, questions: list[dict]):
    """Return (plan, per_system_missing). plan maps cache key -> question, passages and every original row using it."""
    plan: dict[str, dict] = {}
    per_system: dict[str, int] = {}
    for system in NEXUS_SYSTEMS:
        recorded = json.loads((REPORTS / f"heldout_{system}_llm.json").read_text())
        rows = {row["id"]: row for row in recorded["per_question"]}
        retriever = build_system(system, corpus)  # replays embeddings and judgements; inference is blocked
        try:
            missing = 0
            for q in questions:
                hits = retriever.search(q["question"], K)
                retrieved = [doc_id for doc_id, _ in hits]
                if retrieved != rows[q["id"]]["retrieved"]:
                    raise RuntimeError(f"retrieval for {system}/{q['id']} no longer matches the original report")
                passages = [(doc_id, corpus[doc_id]) for doc_id in retrieved]
                key = generator.key(q["question"], passages)
                if generator.cache.get(key) is not None:
                    continue
                missing += 1
                entry = plan.setdefault(key, {"question": q["question"], "passages": passages, "uses": []})
                entry["uses"].append({"system": system, "id": q["id"],
                                      "original": {f: rows[q["id"]][f] for f in ("answer", "citations", "abstained")}})
            per_system[system] = missing
        finally:
            close_all(retriever)
    return plan, per_system


def sample_ollama_rss(stop: threading.Event, peaks: list[int]) -> None:
    while not stop.is_set():
        out = subprocess.run(["ps", "-axo", "rss=,command="], capture_output=True, text=True).stdout
        total = sum(int(line.split()[0]) for line in out.splitlines() if "llama" in line.lower() and "recover" not in line)
        peaks.append(total)
        stop.wait(2)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)

    ollama.INFERENCE_BLOCKED = True  # planning and retrieval replay can never call a model
    if frozen_sha() != APPROVED_FROZEN_SHA or json.loads(FROZEN.read_text()) != current_config():
        print("frozen configuration changed; refusing to run", file=sys.stderr)
        return 1
    generator = LLMGenerator(MODEL, structured=True)
    if generator.config != json.loads(FROZEN.read_text())["harness"]["generator_fingerprint"]:
        print("generator fingerprint differs from the frozen one; refusing to run", file=sys.stderr)
        return 1

    corpus, questions = load_dataset(split="heldout")
    plan, per_system = build_plan(generator, corpus, questions)
    print(f"missing per system: {per_system}; distinct entries: {len(plan)}")
    if per_system != APPROVED_MISSING or len(plan) != APPROVED_DISTINCT:
        print(f"the missing set differs from the approved scope {APPROVED_MISSING} / {APPROVED_DISTINCT}; "
              "refusing to generate anything", file=sys.stderr)
        return 1
    if args.plan:
        print("plan only: no model call was made")
        return 0

    parser_only = LLMAnswerer(None, corpus, generator=generator)
    peaks: list[int] = []
    stop = threading.Event()
    sampler = threading.Thread(target=sample_ollama_rss, args=(stop, peaks), daemon=True)
    sampler.start()
    results = []
    started = time.time()
    try:
        for index, (key, item) in enumerate(plan.items(), start=1):
            ollama.INFERENCE_BLOCKED = False  # opened only around the single approved generator call
            try:
                t0 = time.time()
                raw = generator.request(item["question"], item["passages"])
                seconds = round(time.time() - t0, 1)
            finally:
                ollama.INFERENCE_BLOCKED = True
            rebuilt = answer_fields(parser_only.interpret(raw, item["passages"]))
            uses = [{"system": u["system"], "id": u["id"], "matches_original": rebuilt == u["original"],
                     "original": u["original"]} for u in item["uses"]]
            entry = {"key": key[:16], "question": item["question"], "raw_output": raw, "rebuilt": rebuilt,
                     "seconds": seconds, "uses": uses, "all_uses_match": all(u["matches_original"] for u in uses)}
            if entry["all_uses_match"]:
                generator.cache.put(key, raw)
                entry["stored_in_cache"] = True
            else:
                ollama.INFERENCE_BLOCKED = False
                try:  # determinism probe: same call again, only for entries that differ from the original
                    raw2 = generator.request(item["question"], item["passages"])
                finally:
                    ollama.INFERENCE_BLOCKED = True
                entry.update(stored_in_cache=False, second_raw_output=raw2, second_run_identical_to_first=(raw2 == raw))
            results.append(entry)
            print(f"[{index:2}/{len(plan)}] {'MATCH' if entry['all_uses_match'] else 'DIFFERS'}  "
                  f"{item['uses'][0]['system']}/{item['uses'][0]['id']}  {seconds}s", flush=True)
        generator.cache.flush()
    finally:
        stop.set()
        sampler.join(timeout=3)

    summary = {
        "frozen_config_sha": frozen_sha(), "generator_fingerprint": generator.config, "model": MODEL,
        "options": generator.OPTIONS, "missing_per_system": per_system, "distinct_entries": len(plan),
        "matched_and_stored": sum(r["all_uses_match"] for r in results),
        "differing_not_stored": sum(not r["all_uses_match"] for r in results),
        "peak_ollama_rss_mb": (max(peaks) // 1024) if peaks else None,
        "wall_seconds": round(time.time() - started, 1),
        "entries": results,
    }
    RECOVERY.mkdir(exist_ok=True)
    out = RECOVERY / "heldout_llm_rebuild.json"
    out.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "entries"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
