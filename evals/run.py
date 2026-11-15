"""CLI: python -m evals.run [--system hybrid] [--check] [--update-baseline]."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .harness import check_against_baseline, evaluate, flatten

BASELINE = Path(__file__).parent / "baseline.json"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Offline RAG evaluation with a regression gate.")
    p.add_argument("--system", default="hybrid", choices=["bm25", "tfidf", "hybrid", "broken"])
    p.add_argument("--check", action="store_true", help="fail (exit 1) if metrics regress vs baseline.json")
    p.add_argument("--update-baseline", action="store_true", help="write current hybrid metrics as the baseline")
    p.add_argument("--tolerance", type=float, default=0.02)
    p.add_argument("--output", type=Path, help="write the full JSON report here")
    p.add_argument("--compare", action="store_true", help="print a side-by-side of every system")
    args = p.parse_args(argv)

    if args.compare:
        names = ["bm25", "tfidf", "hybrid"]
        reports = {n: flatten(evaluate(n)["summary"]) for n in names}
        keys = list(reports["bm25"])
        print(f"{'metric':32}" + "".join(f"{n:>10}" for n in names))
        for k in keys:
            print(f"{k:32}" + "".join(f"{reports[n][k]:>10.3f}" for n in names))
        return 0

    report = evaluate(args.system)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"system": report["system"], "summary": report["summary"],
                      "failures": report["failures"]}, indent=2))

    if args.update_baseline:
        BASELINE.write_text(json.dumps({"system": report["system"], "metrics": flatten(report["summary"])},
                                       indent=2, sort_keys=True) + "\n")
        print(f"baseline written to {BASELINE}")
    if args.check:
        problems = check_against_baseline(report, json.loads(BASELINE.read_text()), args.tolerance)
        if problems:
            print("QUALITY GATE FAILED:\n  " + "\n  ".join(problems), file=sys.stderr)
            return 1
        print("quality gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
