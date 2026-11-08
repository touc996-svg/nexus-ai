from app.services.evaluation import AgentEvaluator


def test_agent_evaluator_scores_complete_responses_and_latency():
    def fake_runner(domain, prompt):
        return {
            "response": (
                f"{domain} goal stakeholders workflow KPI steps "
                "evidence authentication logs validation mitigation "
                "week practice milestone progress portfolio role baseline owner risk severity verification."
            ),
            "status": "success"
        }

    report = AgentEvaluator().evaluate(fake_runner)
    assert report["summary"]["cases"] == 6
    assert report["summary"]["passed"] == 6
    assert report["summary"]["pass_rate"] == 1.0
    assert report["summary"]["average_latency_ms"] >= 0
    assert all(item["score"] > 50 for item in report["results"])


def test_agent_evaluator_marks_failed_responses_for_review():
    def failing_runner(domain, prompt):
        return {"error": "not available", "status": "failed"}

    report = AgentEvaluator().evaluate(
        failing_runner,
        cases=[{
            "id": "one", "domain": "enterprise",
            "prompt": "test prompt", "expected_signals": ["goal"]
        }]
    )
    assert report["summary"]["needs_review"] == 1
    assert report["results"][0]["status"] == "needs_review"


def test_agent_evaluator_rejects_empty_case_list():
    try:
        AgentEvaluator().evaluate(lambda *_: {}, cases=[])
    except ValueError:
        pass
    else:
        raise AssertionError("Empty case list should be rejected")
