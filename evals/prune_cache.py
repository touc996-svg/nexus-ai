"""Remove cache entries that no supported evaluation reads.

    python -m evals.prune_cache            # dry run: report what would be removed
    python -m evals.prune_cache --apply    # rewrite the cache files

Safety rules (each one exists because a defect once lost data):
  * Model inference is blocked. If any evaluation needs a value that is not cached, the tool stops and
    changes nothing; it never prunes from an incomplete workload.
  * An entry counts as used when it is read OR written during the run, so an entry created moments ago is
    never removed just because nothing has read it back yet.
  * Pending writes are flushed first, and removal happens under the same file lock and the same
    read-merge-write path as ordinary flushes, so a later exit-time flush can only add entries, never
    bring back removed ones or drop new ones.
"""
from __future__ import annotations

import argparse
import itertools
import os
import sys

from . import ollama
from .ollama import JsonCache, locked, read_json, write_json_atomic

SPLITS = ("dev", "heldout")
ANSWERERS = ("extractive", "llm")


def collect_usage(workload) -> dict[str, set[str]]:
    """Run ``workload`` while recording every cache key read or written; re-raises if it fails."""
    JsonCache.usage = {}
    try:
        workload()
        return {name: set(keys) for name, keys in JsonCache.usage.items()}
    finally:
        JsonCache.usage = None


def prune_files(keep: dict[str, set[str]], apply: bool = False) -> dict[str, tuple[int, int]]:
    """Keep only ``keep[name]`` in each named cache file. Returns {name: (entries_before, entries_after)}."""
    JsonCache.flush_all()  # make sure entries written during the run are on disk before deciding anything
    report = {}
    for name, keys in keep.items():
        path = ollama.CACHE_DIR / name
        with locked(path):
            data = read_json(path)
            kept = {k: v for k, v in data.items() if k in keys}
            report[name] = (len(data), len(kept))
            if apply and len(kept) != len(data):
                write_json_atomic(path, kept)
    return report


def full_workload():
    from .harness import evaluate
    from .retrievers import SYSTEMS

    for split, system, answerer in itertools.product(SPLITS, SYSTEMS, ANSWERERS):
        evaluate(system, answerer=answerer, split=split)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="rewrite the cache files (default: dry run)")
    args = parser.parse_args(argv)

    ollama.INFERENCE_BLOCKED = True
    os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9/v1"  # unreachable on purpose; read by NexusRAG
    try:
        usage = collect_usage(full_workload)
    except Exception as exc:
        print(f"refusing to prune: the workload could not be replayed from the cache "
              f"({type(exc).__name__}: {str(exc)[:120]}). Nothing was changed.", file=sys.stderr)
        return 1
    report = prune_files(usage, apply=args.apply)
    for name, (before, after) in sorted(report.items()):
        print(f"{name:22}{before:6} -> {after:6} entries")
    print("applied" if args.apply else "dry run: nothing was changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
