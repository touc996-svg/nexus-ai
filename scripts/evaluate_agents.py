#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path

from app.core.orchestrator import nexus
from app.services.evaluation import AgentEvaluator, save_evaluation


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the NEXUS three-agent response-quality and latency benchmark."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/agent_evaluation_latest.json")
    )
    parser.add_argument("--minimum-response-chars", type=int, default=40)
    args = parser.parse_args()

    if os.getenv("NEXUS_DEMO_MODE", "false").lower() in {"1", "true", "yes", "on"}:
        print("WARNING: NEXUS_DEMO_MODE is enabled; results measure deterministic demo responses, not live model quality.")

    report = AgentEvaluator(args.minimum_response_chars).evaluate(nexus.run_agent)
    path = save_evaluation(report, args.output)
    print(json.dumps({
        "report": str(path),
        "summary": report["summary"],
        "evaluation_type": report["evaluation_type"]
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
