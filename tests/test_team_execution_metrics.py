from app.core.orchestrator import NexusOrchestrator


def test_team_workflow_returns_metrics_and_passes_prior_summaries(monkeypatch):
    orchestrator = NexusOrchestrator()
    calls = []

    def fake_run(domain, message, internal=False, user_profile=None):
        calls.append((domain, len(message), internal, user_profile))
        return {
            "agent": domain,
            "response": (
                f"{domain} analysis complete with actionable recommendations "
                "and verified next steps."
            ),
            "status": "success"
        }

    monkeypatch.setattr(orchestrator, "run_agent", fake_run)
    report = orchestrator.run_team(
        "Assess our platform",
        user_profile={"career_direction": "AI engineering"}
    )
    assert [item[0] for item in calls] == [
        "enterprise", "personal", "cybersecurity"
    ]
    assert calls[1][3] == {"career_direction": "AI engineering"}
    assert report["status"] == "completed"
    assert report["execution_metrics"]["total_duration_ms"] >= 0
    metrics = report["execution_metrics"]["agents"]
    assert set(metrics) == {"enterprise", "personal", "cybersecurity"}
    assert all(item["response_present"] for item in metrics.values())
