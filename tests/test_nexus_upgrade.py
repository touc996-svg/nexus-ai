from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agents.enterprise_agent import EnterpriseAgent
from app.agents.personal_agent import PersonalAgent
from app.core.decision_engine import NexusDecisionEngine
from app.core.memory import NexusMemory
from app.core.orchestrator import NexusOrchestrator
from app.core.router import NexusRouter
from app.security.hardening import SlidingWindowRateLimiter, install_security
from app.services.evaluation import AgentEvaluator


def _fake_client(content="A useful response with practical next steps."):
    client = MagicMock()
    client.chat.completions.create.return_value.choices[0].message.content = content
    return client


def test_agents_share_normalized_response_contract():
    enterprise = EnterpriseAgent(llm_client=_fake_client()).run("Review the rollout")
    personal = PersonalAgent(llm_client=_fake_client()).run("Plan my learning")
    assert enterprise["domain"] == "enterprise"
    assert personal["domain"] == "personal"
    for result in (enterprise, personal):
        assert result["status"] == "success"
        assert isinstance(result["response"], str) and result["response"]


def test_agent_empty_model_output_is_failure():
    result = EnterpriseAgent(llm_client=_fake_client("  ")).run("Review the rollout")
    assert result["status"] == "failed"
    assert result["error_type"] == "EmptyResponse"


def test_agent_rejects_non_text_input():
    result = EnterpriseAgent(llm_client=_fake_client()).run(None)
    assert result["status"] == "failed"
    assert result["error_type"] == "InvalidInput"


def test_router_uses_word_boundaries_and_marks_ambiguous_intent():
    router = NexusRouter()
    substring = router.route("I like networking tools")
    assert substring["scores"]["cybersecurity"] == 0
    ambiguous = router.route("business security")
    assert ambiguous["requires_clarification"] is True
    assert ambiguous["confidence_level"] == "low"


def test_orchestrator_continues_after_failed_agent_and_marks_metrics():
    class FakeAgent:
        def __init__(self, result):
            self.result = result
            self.messages = []

        def run(self, message, **kwargs):
            self.messages.append(message)
            return self.result

    class Sink:
        def record(self, *args):
            return None

    agents = {
        "enterprise": FakeAgent({"error": "failed", "status": "failed"}),
        "personal": FakeAgent({"response": "Plan next steps.", "status": "success"}),
        "cybersecurity": FakeAgent({"response": "Review evidence.", "status": "success"}),
    }
    orchestrator = NexusOrchestrator(agents=agents, activity_sink=Sink())
    report = orchestrator.run_team("Assess this platform")
    assert report["status"] == "completed_with_errors"
    assert report["failed_agents"] == ["enterprise"]
    assert report["execution_metrics"]["agents"]["enterprise"]["quality_flag"] == "agent_failed"
    assert "untrusted_agent_output" in agents["personal"].messages[0]
    assert "untrusted_agent_output" in agents["cybersecurity"].messages[0]


def test_activity_storage_failure_does_not_break_successful_agent():
    class FakeAgent:
        def run(self, message):
            return {"response": "Completed safely.", "status": "success"}

    class BrokenSink:
        def record(self, *args):
            raise RuntimeError("database unavailable")

    orchestrator = NexusOrchestrator(
        agents={"enterprise": FakeAgent()}, activity_sink=BrokenSink()
    )
    result = orchestrator.run_agent("enterprise", "Do a review")
    assert result["status"] == "success"


def test_decision_engine_does_not_flag_explicitly_negated_risk():
    engine = NexusDecisionEngine()
    report = engine.generate_report({
        "enterprise": {"response": "No data breach was identified."},
        "personal": {"response": "Continue monitoring."},
        "cybersecurity": {"response": "No unauthorized access observed."},
    })
    assert report["risks"] == []
    assert report["decision"]["outcome"] == "PROCEED_WITH_REVIEW"


def test_decision_engine_escalates_potential_critical_risk_for_review():
    report = NexusDecisionEngine().generate_report({
        "enterprise": {"response": "Potential ransomware activity requires investigation."},
        "personal": {"response": "Preserve evidence."},
        "cybersecurity": {"response": "Validate endpoint telemetry."},
    })
    assert report["decision"]["outcome"] == "REVIEW_REQUIRED"
    assert report["risks"][0]["verification_required"] is True


def test_memory_rejects_oversized_and_non_json_profile_data():
    with pytest.raises(ValueError, match="4000 characters"):
        NexusMemory._validate_profile_updates({"career_direction": "x" * 4001})
    with pytest.raises(ValueError, match="valid JSON"):
        NexusMemory._validate_profile_updates({"skills": {"python": float("nan")}})


def test_evaluation_uses_word_boundaries_and_domain_breakdown():
    report = AgentEvaluator().evaluate(
        lambda domain, _prompt: {
            "response": "A weekly practice plan includes a milestone and progress review.",
            "status": "success",
        },
        cases=[{
            "id": "personal-one",
            "domain": "personal",
            "prompt": "Create a study plan",
            "expected_signals": ["week", "practice", "milestone"],
        }],
    )
    assert report["summary"]["cases"] == 1
    assert report["per_domain"]["personal"]["cases"] == 1
    assert "week" not in report["results"][0]["matched_signals"]


def test_evaluation_rejects_duplicate_case_ids():
    cases = [
        {"id": "duplicate", "domain": "enterprise", "prompt": "one"},
        {"id": "duplicate", "domain": "personal", "prompt": "two"},
    ]
    with pytest.raises(ValueError, match="unique"):
        AgentEvaluator().evaluate(lambda *_: "response", cases=cases)


def test_demo_mode_blocks_activity_summary(monkeypatch):
    monkeypatch.setenv("NEXUS_DEMO_MODE", "true")
    monkeypatch.delenv("NEXUS_API_KEY", raising=False)
    monkeypatch.delenv("NEXUS_REQUIRE_API_KEY", raising=False)
    app = FastAPI()

    @app.get("/activity/summary")
    def summary():
        return {"total": 1}

    install_security(app)
    response = TestClient(app).get("/activity/summary")
    assert response.status_code == 403


def test_rate_limiter_bounds_client_key_storage():
    limiter = SlidingWindowRateLimiter(limit=2, max_keys=100)
    for index in range(300):
        limiter.check(f"client-{index}", now=1000 + index)
    assert len(limiter._events) <= 100


def test_memory_profile_has_total_serialized_size_limit():
    with pytest.raises(ValueError, match="20000-character"):
        NexusMemory._validate_profile_updates({"skills": {"python": "x" * 20_100}})


def test_rag_validates_threshold_and_document_limits():
    from app.services.rag import NexusRAG

    with pytest.raises(ValueError, match="between -1 and 1"):
        NexusRAG(demo_mode=True, score_threshold=1.5)

    rag = NexusRAG(demo_mode=True)
    rag.max_document_chars = 10
    with pytest.raises(ValueError, match="character limit"):
        rag.ingest_text("x" * 11, "oversized.txt")


def test_chat_requests_get_independent_default_sessions():
    from app.main import ChatRequest

    first = ChatRequest(message="hello")
    second = ChatRequest(message="hello")
    assert first.session_id != second.session_id


def test_evaluation_marks_empty_success_payload_for_review():
    report = AgentEvaluator().evaluate(
        lambda *_: {"response": "", "status": "success"},
        cases=[{"id": "empty", "domain": "enterprise", "prompt": "test"}],
    )
    assert report["results"][0]["status"] == "needs_review"
    assert report["results"][0]["score"] == 0.0


def test_auto_route_requests_clarification_instead_of_defaulting(monkeypatch):
    from app import main

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(main.nexus, "run_agent", lambda *args, **kwargs: pytest.fail("agent should not run"))
        response = main.auto_route(main.AgentRequest(message="Can you help me?"))
    assert response["status"] == "needs_clarification"
    assert response["result"] is None


def test_decision_engine_reports_missing_required_agents_as_failures():
    report = NexusDecisionEngine().generate_report({
        "enterprise": {"response": "Business review complete.", "status": "success"},
    })
    assert report["failed_agents"] == ["personal", "cybersecurity"]
    assert report["decision"]["outcome"] == "REVIEW_REQUIRED"
