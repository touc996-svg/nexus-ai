"""Repeatable heuristic quality and latency benchmark for all NEXUS agents."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from time import perf_counter


DEFAULT_BENCHMARKS = [
    {
        "id": "enterprise-operations",
        "domain": "enterprise",
        "prompt": "Assess a new service rollout. Cover the business goal, stakeholders, operational workflow, measurable KPIs and risks.",
        "expected_signals": ["goal", "stakeholder", "workflow", "kpi"],
    },
    {
        "id": "enterprise-process-improvement",
        "domain": "enterprise",
        "prompt": "Recommend a process improvement plan for a support team. Include baseline, owner, implementation steps and success metrics.",
        "expected_signals": ["baseline", "owner", "steps", "metrics"],
    },
    {
        "id": "personal-learning-plan",
        "domain": "personal",
        "prompt": "Create a practical four-week learning plan for someone learning Python, with milestones, practice and progress checks.",
        "expected_signals": ["week", "practice", "milestone", "progress"],
    },
    {
        "id": "personal-career-plan",
        "domain": "personal",
        "prompt": "Create a career development plan with a target role, skills gap, portfolio evidence and measurable next steps.",
        "expected_signals": ["skills", "portfolio", "role", "steps"],
    },
    {
        "id": "cybersecurity-suspicious-login",
        "domain": "cybersecurity",
        "prompt": "Review a suspicious login alert. Explain evidence to collect, authentication checks, containment options and validation.",
        "expected_signals": ["evidence", "authentication", "logs", "validation"],
    },
    {
        "id": "cybersecurity-vulnerability",
        "domain": "cybersecurity",
        "prompt": "Prioritize a reported web application vulnerability. Discuss severity, exploitability, mitigation and verification.",
        "expected_signals": ["severity", "mitigation", "verification", "risk"],
    },
]


class AgentEvaluator:
    """Measures availability, expected-signal coverage, and latency heuristically."""

    def __init__(self, minimum_response_chars: int = 40):
        if not isinstance(minimum_response_chars, int) or minimum_response_chars < 1:
            raise ValueError("minimum_response_chars must be a positive integer")
        self.minimum_response_chars = minimum_response_chars

    @staticmethod
    def _response_text(result) -> str:
        if isinstance(result, dict):
            if "error" in result or result.get("status") in {"failed", "rejected"}:
                return ""
            value = result.get("response") or result.get("output") or ""
            return str(value).strip()
        return str(result or "").strip()

    @staticmethod
    def _signal_matches(response: str, signal: str) -> bool:
        normalized = str(signal).strip().lower()
        if not normalized:
            return False
        # Treat CamelCase transitions as token boundaries (e.g. workflowKPI).
        text = re.sub(r"([a-z])([A-Z])", r"\1 \2", response)
        pattern = rf"(?<!\w){re.escape(normalized)}(?!\w)"
        return re.search(pattern, text.lower()) is not None

    @staticmethod
    def _percentile(values, percentile):
        ordered = sorted(values)
        if not ordered:
            return 0.0
        index = max(0, min(len(ordered) - 1, int((len(ordered) * percentile + 0.999999)) - 1))
        return round(float(ordered[index]), 2)

    def evaluate(self, runner, cases=None) -> dict:
        selected_cases = DEFAULT_BENCHMARKS if cases is None else cases
        if not isinstance(selected_cases, (list, tuple)) or not selected_cases:
            raise ValueError("At least one evaluation case is required")

        ids = set()
        for case in selected_cases:
            if not isinstance(case, dict) or not {"id", "domain", "prompt"}.issubset(case):
                raise ValueError("Each evaluation case requires id, domain, and prompt")
            if not isinstance(case["id"], str) or not case["id"].strip() or case["id"] in ids:
                raise ValueError("Evaluation case IDs must be unique non-empty strings")
            if case["domain"] not in {"enterprise", "personal", "cybersecurity"}:
                raise ValueError(f"Unsupported evaluation domain: {case['domain']}")
            if not isinstance(case["prompt"], str) or not case["prompt"].strip():
                raise ValueError("Evaluation prompts must be non-empty text")
            signals = case.get("expected_signals", [])
            if not isinstance(signals, (list, tuple)) or any(not isinstance(x, str) for x in signals):
                raise ValueError("expected_signals must be a list of strings")
            ids.add(case["id"])

        results = []
        durations = []
        domain_results = defaultdict(list)
        for case in selected_cases:
            started = perf_counter()
            error_type = None
            try:
                output = runner(case["domain"], case["prompt"])
                failed = (
                    not isinstance(output, (dict, str))
                    or (isinstance(output, dict) and (
                        "error" in output or output.get("status") in {"failed", "rejected"}
                    ))
                )
                response = self._response_text(output)
            except Exception as exc:
                failed = True
                response = ""
                error_type = type(exc).__name__

            duration_ms = round((perf_counter() - started) * 1000, 2)
            durations.append(duration_ms)
            required = [term.strip().lower() for term in case.get("expected_signals", []) if term.strip()]
            matched = [term for term in required if self._signal_matches(response, term)]
            coverage = len(matched) / len(required) if required else 1.0
            length_score = min(len(response) / self.minimum_response_chars, 1.0)
            score = (
                0.0 if failed or not response else round(100 * (
                    0.40 + 0.25 * length_score + 0.35 * coverage
                ), 2)
            )
            passed = (
                not failed
                and len(response) >= self.minimum_response_chars
                and coverage >= 0.50
            )
            item = {
                "case_id": case["id"],
                "domain": case["domain"],
                "status": "passed" if passed else "needs_review",
                "score": score,
                "duration_ms": duration_ms,
                "response_characters": len(response),
                "signal_coverage": round(coverage, 3),
                "matched_signals": matched,
                "expected_signals": required,
                "error_type": error_type,
            }
            results.append(item)
            domain_results[case["domain"]].append(item)

        passed_count = sum(item["status"] == "passed" for item in results)
        per_domain = {}
        for domain, items in sorted(domain_results.items()):
            per_domain[domain] = {
                "cases": len(items),
                "passed": sum(item["status"] == "passed" for item in items),
                "pass_rate": round(sum(item["status"] == "passed" for item in items) / len(items), 3),
                "average_latency_ms": round(mean(item["duration_ms"] for item in items), 2),
            }

        return {
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "benchmark_version": "2.0",
            "evaluation_type": "heuristic_response_quality_and_latency",
            "limitations": (
                "Signal coverage and response length are deterministic heuristics, "
                "not a human-quality judgement, factuality check, or ground-truth accuracy measure."
            ),
            "summary": {
                "cases": len(results),
                "passed": passed_count,
                "needs_review": len(results) - passed_count,
                "pass_rate": round(passed_count / len(results), 3),
                "average_latency_ms": round(mean(durations), 2),
                "median_latency_ms": round(median(durations), 2),
                "p95_latency_ms": self._percentile(durations, 0.95),
                "max_latency_ms": round(max(durations), 2),
            },
            "per_domain": per_domain,
            "results": results,
        }


def save_evaluation(report: dict, output_path: str | Path) -> Path:
    """Atomically write the latest evaluation report without partial JSON files."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
    return path


agent_evaluator = AgentEvaluator()
