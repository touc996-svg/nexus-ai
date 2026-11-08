from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from time import perf_counter
import json


DEFAULT_BENCHMARKS = [
    {
        "id": "enterprise-operations",
        "domain": "enterprise",
        "prompt": "Assess a new service rollout. Cover the business goal, stakeholders, operational workflow, measurable KPIs and risks.",
        "expected_signals": ["goal", "stakeholder", "workflow", "kpi"]
    },
    {
        "id": "enterprise-process-improvement",
        "domain": "enterprise",
        "prompt": "Recommend a process improvement plan for a support team. Include baseline, owner, implementation steps and success metrics.",
        "expected_signals": ["baseline", "owner", "steps", "metrics"]
    },
    {
        "id": "personal-learning-plan",
        "domain": "personal",
        "prompt": "Create a practical four-week learning plan for someone learning Python, with milestones, practice and progress checks.",
        "expected_signals": ["week", "practice", "milestone", "progress"]
    },
    {
        "id": "personal-career-plan",
        "domain": "personal",
        "prompt": "Create a career development plan with a target role, skills gap, portfolio evidence and measurable next steps.",
        "expected_signals": ["skills", "portfolio", "role", "steps"]
    },
    {
        "id": "cybersecurity-suspicious-login",
        "domain": "cybersecurity",
        "prompt": "Review a suspicious login alert. Explain evidence to collect, authentication checks, containment options and validation.",
        "expected_signals": ["evidence", "authentication", "logs", "validation"]
    },
    {
        "id": "cybersecurity-vulnerability",
        "domain": "cybersecurity",
        "prompt": "Prioritize a reported web application vulnerability. Discuss severity, exploitability, mitigation and verification.",
        "expected_signals": ["severity", "mitigation", "verification", "risk"]
    }
]


class AgentEvaluator:
    "Repeatable benchmark for response availability, signal coverage and latency."

    def __init__(self, minimum_response_chars: int = 40):
        self.minimum_response_chars = max(1, minimum_response_chars)

    @staticmethod
    def _response_text(result) -> str:
        if isinstance(result, dict):
            return str(
                result.get("response") or result.get("output") or result.get("error") or ""
            ).strip()
        return str(result or "").strip()

    def evaluate(self, runner, cases=None) -> dict:
        selected_cases = DEFAULT_BENCHMARKS if cases is None else cases
        if not selected_cases:
            raise ValueError("At least one evaluation case is required")

        results = []
        durations = []
        for case in selected_cases:
            started = perf_counter()
            error_type = None
            try:
                output = runner(case["domain"], case["prompt"])
                failed = (
                    isinstance(output, dict)
                    and ("error" in output or output.get("status") == "failed")
                )
                response = self._response_text(output)
            except Exception as exc:
                failed = True
                response = ""
                error_type = type(exc).__name__

            duration_ms = round((perf_counter() - started) * 1000, 2)
            durations.append(duration_ms)
            lower_response = response.lower()
            required = [str(term).lower() for term in case.get("expected_signals", [])]
            matched = [term for term in required if term in lower_response]
            coverage = len(matched) / len(required) if required else 1.0
            length_score = min(len(response) / self.minimum_response_chars, 1.0)
            score = round(100 * (
                (0.40 if response and not failed else 0.0)
                + 0.25 * length_score
                + 0.35 * coverage
            ), 2)
            passed = (
                not failed
                and len(response) >= self.minimum_response_chars
                and coverage >= 0.50
            )
            results.append({
                "case_id": case["id"],
                "domain": case["domain"],
                "status": "passed" if passed else "needs_review",
                "score": score,
                "duration_ms": duration_ms,
                "response_characters": len(response),
                "signal_coverage": round(coverage, 3),
                "matched_signals": matched,
                "expected_signals": required,
                "error_type": error_type
            })

        ordered = sorted(durations)
        p95_index = max(0, min(
            len(ordered) - 1,
            int((len(ordered) * 0.95) + 0.999) - 1
        ))
        passed_count = sum(item["status"] == "passed" for item in results)
        return {
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "benchmark_version": "1.0",
            "evaluation_type": "heuristic_response_quality_and_latency",
            "limitations": (
                "Signal coverage and response length are deterministic heuristics, "
                "not a human-quality judgement or ground-truth accuracy measure."
            ),
            "summary": {
                "cases": len(results),
                "passed": passed_count,
                "needs_review": len(results) - passed_count,
                "pass_rate": round(passed_count / len(results), 3),
                "average_latency_ms": round(mean(durations), 2),
                "p95_latency_ms": ordered[p95_index]
            },
            "results": results
        }


def save_evaluation(report: dict, output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


agent_evaluator = AgentEvaluator()
