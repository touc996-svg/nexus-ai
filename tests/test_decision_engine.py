
from app.core.decision_engine import NexusDecisionEngine


def test_security_risk_detection():
    engine = NexusDecisionEngine()

    results = {
        "enterprise": {"response": "Potential data breach detected."},
        "personal": {"response": "Review access controls."},
        "cybersecurity": {"response": "Check for unauthorized access."}
    }

    report = engine.generate_report(results)

    assert any(
        risk["category"] == "security"
        for risk in report["risks"]
    )


def test_agent_failure_detection():
    engine = NexusDecisionEngine()

    results = {
        "enterprise": {"response": "Analysis complete."},
        "personal": {"error": "Agent failed"},
        "cybersecurity": {"response": "No issues found."}
    }

    report = engine.generate_report(results)

    assert "personal" in report["failed_agents"]
    assert report["decision"]["outcome"] == "REVIEW_REQUIRED"


def test_normal_assessment():
    engine = NexusDecisionEngine()

    results = {
        "enterprise": {"response": "Business overview complete."},
        "personal": {"response": "Improve workflow efficiency."},
        "cybersecurity": {"response": "Standard review complete."}
    }

    report = engine.generate_report(results)

    assert report["status"] == "completed"
    assert report["decision"]["outcome"] == "PROCEED_WITH_REVIEW"



def test_not_recommended_in_one_agent_is_not_a_contradiction():
    engine = NexusDecisionEngine()
    results = {
        "enterprise": {"response": "This option is not recommended."},
        "personal": {"response": "Review the costs."},
        "cybersecurity": {"response": "No security concerns identified."},
    }
    report = engine.generate_report(results)
    assert not any(
        item["conflicting_signals"] == ["recommended", "not recommended"]
        for item in report["contradictions"]
    )


def test_recommendation_conflict_across_agents_requires_review():
    engine = NexusDecisionEngine()
    results = {
        "enterprise": {"response": "This option is recommended."},
        "personal": {"response": "This option is not recommended."},
        "cybersecurity": {"response": "No security concerns identified."},
    }
    report = engine.generate_report(results)
    assert any(
        item["conflicting_signals"] == ["recommended", "not recommended"]
        for item in report["contradictions"]
    )
