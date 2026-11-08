from unittest.mock import patch

from fastapi.testclient import TestClient

from app import main


def test_demo_mode_blocks_persistent_memory_and_profile_routes(monkeypatch):
    monkeypatch.setattr(main, "DEMO_MODE", True)
    client = TestClient(main.app)

    assert client.get("/memory/demo-session").status_code == 403
    assert client.post(
        "/memory/demo-session",
        json={"role": "user", "content": "private"}
    ).status_code == 403
    assert client.delete("/memory/demo-session").status_code == 403

    assert client.get("/profile/demo-user").status_code == 403
    assert client.put(
        "/profile/demo-user",
        json={"career_direction": "private"}
    ).status_code == 403
    assert client.delete("/profile/demo-user").status_code == 403


def test_demo_chat_does_not_read_or_write_persistent_user_data(monkeypatch):
    monkeypatch.setattr(main, "DEMO_MODE", True)
    monkeypatch.setattr(main.rag, "search", lambda **kwargs: [])
    understanding = {
        "intent_type": "conversation",
        "agent": None,
        "response": "Demo response",
        "clarifying_question": None,
        "understood_request": "hello",
    }

    with (
        patch.object(main.memory, "get_history", side_effect=AssertionError("history read")),
        patch.object(main.memory, "get_profile", side_effect=AssertionError("profile read")),
        patch.object(main.memory, "add_message", side_effect=AssertionError("memory write")),
        patch.object(main.conversation_engine, "understand", return_value=understanding),
    ):
        response = main.chat(main.ChatRequest(message="hello", user_id="public-user"))

    assert response["response"] == "Demo response"
    assert response["status"] == "completed"


def test_retrieval_does_not_override_conversation_clarification(monkeypatch):
    monkeypatch.setattr(main, "DEMO_MODE", False)
    monkeypatch.setattr(main.memory, "get_history", lambda _session: [])
    monkeypatch.setattr(main.memory, "get_profile", lambda _user: None)
    monkeypatch.setattr(main.memory, "add_message", lambda *args: None)
    monkeypatch.setattr(
        main.rag,
        "search",
        lambda **kwargs: [{"source": "guide.txt", "text": "Cybersecurity reference text"}],
    )
    clarification = {
        "intent_type": "clarification",
        "agent": None,
        "response": "",
        "clarifying_question": "Which system are you referring to?",
        "understood_request": "Needs clarification",
    }

    with (
        patch.object(main.conversation_engine, "understand", return_value=clarification),
        patch.object(main.nexus, "run_agent") as run_agent,
    ):
        response = main.chat(main.ChatRequest(message="Can you help?"))

    assert response["intent"]["intent_type"] == "clarification"
    assert response["agent"] is None
    assert response["response"] == "Which system are you referring to?"
    run_agent.assert_not_called()


def test_retrieved_knowledge_is_bounded_and_marked_as_reference_material():
    content = main.format_knowledge_context(
        [{"source": "handbook.txt", "text": "x" * 10000}],
        max_chars=400,
    )
    assert "handbook.txt" in content
    assert len(content) <= 400


def test_dashboard_uses_a_unique_session_not_a_shared_constant():
    html = (main.Path(main.__file__).parent / "dashboard" / "index.html").read_text()
    assert 'session_id: "nexus-dashboard"' not in html
    assert "crypto.randomUUID()" in html
